"""Job execution: fixed scripts, the Freebuff pipeline and the manager review.

Discord never supplies a command line. A job either names one of the shell
scripts in ``scripts/`` or carries plain task text for Freebuff. Everything is
executed with an argv list, never through a shell.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import secrets
import shlex
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import rules
from freebuff_runner import FreebuffError, FreebuffRunner, TaskEnvironment
from manager import Manager, ReviewBrief

# The repository layout requires queue.py, which shadows the standard library
# module of the same name. Load it under a private module name so libraries
# (aiohttp, concurrent.futures, ...) still get the real stdlib `queue`.
def _load_queue_module() -> object:
    name = "macbuilder_queue"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name("queue.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_queue_module = _load_queue_module()
Job = _queue_module.Job
JobQueue = _queue_module.JobQueue
QueueFull = _queue_module.QueueFull

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class ReviewOutcome:
    job: Job
    verdict: str
    notes: str
    environment: TaskEnvironment | None
    check_state: str
    test_state: str
    diff_lines: int
    changed_files: tuple[str, ...]
    violations: tuple[rules.RuleViolation, ...] = ()


@dataclass
class ExecOutcome:
    code: int
    timed_out: bool
    tail: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


PostJob = Callable[[Job, bool, str], Awaitable[None]]
PostReview = Callable[[ReviewOutcome], Awaitable[None]]


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def tail_text(text: str, *, max_lines: int = 60, max_chars: int = 3500) -> str:
    lines = strip_ansi(text).replace("\r", "").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        prefix = [f"[{len(lines)} line tail]"]
    else:
        prefix = []
    value = "\n".join(prefix + lines).strip()
    if len(value) > max_chars:
        value = value[-max_chars:]
        value = "[earlier output omitted]\n" + value
    return value


def make_job(
    settings: object,
    *,
    kind: str,
    title: str,
    channel_id: int,
    requester_id: int,
    text: str = "",
) -> Job:
    job_id = new_job_id()
    log_dir = Path(getattr(settings, "log_dir"))
    log_dir.mkdir(parents=True, exist_ok=True)
    return Job(
        id=job_id,
        kind=kind,
        title=title,
        channel_id=channel_id,
        requester_id=requester_id,
        log_path=log_dir / f"{job_id}.log",
        text=text,
    )


def build_script_env(settings: object, repo_dir: Path) -> dict[str, str]:
    """A deliberately small environment for shell scripts.

    Secrets (Discord token, manager key) are never forwarded.
    """
    env: dict[str, str] = {}
    for key in ("HOME", "LANG", "LC_ALL", "TMPDIR", "USER"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    base_path = os.environ.get("PATH", "/usr/bin:/bin")
    parts = ["/opt/homebrew/bin", "/usr/local/bin", *base_path.split(":")]
    env["PATH"] = os.pathsep.join(dict.fromkeys(part for part in parts if part))
    env["REPO_DIR"] = str(repo_dir)
    env["HUSTLERCORE_DIR"] = str(getattr(settings, "hustlercore_dir", "ios-app/HustlerCore"))
    env["XCODE_PROJECT"] = str(getattr(settings, "xcode_project", "ios-app/Hustler.xcodeproj"))
    env["XCODE_SCHEME"] = str(getattr(settings, "xcode_scheme", "Hustler"))
    env["SIMULATOR_NAME"] = str(getattr(settings, "simulator_name", ""))
    env["SWIFT_JOBS"] = str(getattr(settings, "swift_jobs", 2))
    env["XCODEBUILD_JOBS"] = str(getattr(settings, "xcodebuild_jobs", 2))
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    return env


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()


async def _exec(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    log_path: Path | None = None,
    max_tail: int = 80,
    max_output: int = 400,
) -> ExecOutcome:
    handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8", errors="replace")
        handle.write(f"\n$ {shlex.join(list(argv))}\n")
        handle.flush()

    tail: deque[str] = deque(maxlen=max_tail)
    output: deque[str] = deque(maxlen=max_output)
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            message = f"could not start {argv[0]}: {exc}"
            if handle is not None:
                handle.write(message + "\n")
            return ExecOutcome(127, False, [message], [message])

        async def pump() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                text = raw.decode("utf-8", "replace")
                if handle is not None:
                    handle.write(text)
                    handle.flush()
                for line in text.splitlines():
                    tail.append(line)
                    output.append(line)
            await proc.wait()

        timed_out = False
        try:
            await asyncio.wait_for(pump(), timeout=max(1.0, timeout))
            code = proc.returncode
        except asyncio.CancelledError:
            # Bot shutdown must not leave xcodebuild running on the mini.
            await _stop_process(proc)
            raise
        except asyncio.TimeoutError:
            timed_out = True
            await _stop_process(proc)
            code = 124
            message = f"[timed out after {int(timeout)}s]"
            if handle is not None:
                handle.write("\n" + message + "\n")
                handle.flush()
            tail.append(message)
            output.append(message)
        return ExecOutcome(code, timed_out, list(tail), list(output))
    finally:
        if handle is not None:
            handle.close()


async def probe_status(settings: object) -> tuple[int, str]:
    """Run status.sh for /status without creating a job."""
    repo_dir = Path(getattr(settings, "repo_dir"))
    outcome = await _exec(
        ["/bin/bash", str(SCRIPT_DIR / "status.sh")],
        cwd=repo_dir,
        env=build_script_env(settings, repo_dir),
        timeout=float(getattr(settings, "status_timeout_seconds", 60)),
        max_tail=120,
        max_output=300,
    )
    return outcome.code, "\n".join(outcome.output)


def _stage_timeout(settings: object, kind: str) -> float:
    mapping = {
        "check": "check_timeout_seconds",
        "test": "test_timeout_seconds",
        "build": "build_timeout_seconds",
    }
    return float(getattr(settings, mapping.get(kind, "build_timeout_seconds"), 1800))


def _log_separator(job: Job, title: str) -> None:
    with job.log_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"\n=== {title} ===\n")
        handle.flush()


def build_followup_text(task: str, notes: str) -> str:
    return (
        "Your previous attempt was reviewed and needs changes before it can be approved.\n\n"
        "Reviewer notes:\n"
        f"{notes.strip()}\n\n"
        "The original task is still:\n"
        f"{task}\n\n"
        "Keep the change focused. Do not touch protected files (project.yml, *.xcconfig, "
        ".github/, .env*, mac-builder/, signing or provisioning material). Do not push."
    )


class JobRunner:
    """The one place that knows how to execute a job."""

    def __init__(
        self,
        settings: object,
        manager: Manager,
        freebuff: FreebuffRunner,
        *,
        post_job: PostJob,
        post_review: PostReview,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.freebuff = freebuff
        self.post_job = post_job
        self.post_review = post_review
        self._last_log: Path | None = None
        self._last_summary = ""
        self._last_diff_excerpt = ""
        self._running_env: TaskEnvironment | None = None

    # ------------------------------------------------------------------ run

    async def run(self, job: Job) -> None:
        job.status = "running"
        self._last_log = job.log_path
        _log_separator(
            job,
            f"job {job.id} kind={job.kind} requested_by={job.requester_id}",
        )
        ok = False
        summary = ""
        try:
            if job.kind == "task":
                ok, summary = await self._run_task(job)
            else:
                ok, summary = await self._run_script(job)
        except FreebuffError as exc:
            summary = f"{job.kind} failed: {exc}"
            log.warning("job %s: %s", job.id, summary)
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job %s crashed", job.id)
            summary = f"{job.kind} crashed: {exc}"
        finally:
            job.finished = time.time()
            self._running_env = None

        job.status = "done" if ok else "failed"
        job.summary = summary
        self._last_summary = f"{job.kind} {job.id}: {summary}"
        try:
            await self.post_job(job, ok, summary)
        except Exception:  # pragma: no cover - Discord can fail independently
            log.exception("could not post result for job %s", job.id)

    async def shutdown(self) -> None:
        await self.freebuff.abort(self._running_env)

    def last_job_context(self) -> str:
        parts = []
        if self._last_log is not None:
            parts.append(f"latest log: {self._last_log.name}")
        if self._last_summary:
            parts.append(f"latest result: {self._last_summary}")
        if self._last_diff_excerpt:
            parts.append("latest diff excerpt:\n" + self._last_diff_excerpt)
        return "\n\n".join(parts)

    # --------------------------------------------------------- script jobs

    async def _run_script(self, job: Job) -> tuple[bool, str]:
        script = SCRIPT_DIR / f"{job.kind}.sh"
        if not script.is_file():
            return False, f"missing script: scripts/{script.name}"
        outcome = await _exec(
            ["/bin/bash", str(script)],
            cwd=Path(getattr(self.settings, "repo_dir")),
            env=build_script_env(self.settings, Path(getattr(self.settings, "repo_dir"))),
            timeout=_stage_timeout(self.settings, job.kind),
            log_path=job.log_path,
        )
        state = (
            "timed out"
            if outcome.timed_out
            else ("passed" if outcome.code == 0 else f"failed (exit {outcome.code})")
        )
        summary = f"{job.kind} {state}. Log: {job.log_path.name}"
        if outcome.tail:
            summary += "\n" + "\n".join(outcome.tail[-12:])
        return outcome.code == 0, summary

    # ----------------------------------------------------------- task jobs

    async def _run_task(self, job: Job) -> tuple[bool, str]:
        env: TaskEnvironment | None = None
        try:
            env = await self.freebuff.start_task(
                job_id=job.id, task_text=job.text, log_path=job.log_path
            )
            self._running_env = env
            reason = await self.freebuff.wait_for_changes(env, job.log_path)
            if reason == "timeout":
                await self.freebuff.abort(env)
                return (
                    False,
                    f"Freebuff timed out on {env.branch}; the session was stopped. Log: {job.log_path.name}",
                )

            while True:
                changes = await self.freebuff.collect(env, int(getattr(self.settings, "max_diff_lines")))
                self._last_diff_excerpt = changes.diff[:4000]
                if changes.empty:
                    await self.freebuff.abort(env)
                    return (
                        False,
                        f"Freebuff made no changes on {env.branch} ({reason}). Log: {job.log_path.name}",
                    )

                # Union the status paths with the paths named by the diff so a
                # quoted or renamed path cannot slip past the protected list.
                all_paths = sorted(
                    set(changes.changed_paths) | set(rules.changed_files_from_diff(changes.diff))
                )
                violations = tuple(
                    rules.evaluate_change(
                        diff_lines=changes.diff_lines,
                        changed_paths=all_paths,
                        max_diff_lines=int(getattr(self.settings, "max_diff_lines")),
                    )
                )
                if violations:
                    await self.freebuff.abort(env)
                    outcome = ReviewOutcome(
                        job=job,
                        verdict="blocked",
                        notes=rules.protected_summary(violations),
                        environment=env,
                        check_state="not run",
                        test_state="not run",
                        diff_lines=changes.diff_lines,
                        changed_files=tuple(all_paths),
                        violations=violations,
                    )
                    await self.post_review(outcome)
                    return False, "Blocked by rules; the manager was not called."

                check_state, check_tail, check_ok = await self._run_stage(job, env, "check")
                test_state, test_tail, test_ok = await self._run_stage(job, env, "test")

                brief = ReviewBrief(
                    task=job.text,
                    diff=changes.diff,
                    diff_lines=changes.diff_lines,
                    status=changes.status,
                    changed_files=changes.changed_paths,
                    check_state=check_state,
                    check_tail=check_tail,
                    test_state=test_state,
                    test_tail=test_tail,
                    round_number=env.round,
                )
                review = await self.manager.review(brief)
                verdict = review.verdict
                notes = review.notes
                forced = rules.force_revise_reason(check_ok, test_ok)
                if forced:
                    verdict = "revise"
                    notes = forced + (f"\n\n{notes}" if notes else "")

                if verdict == "revise" and env.round < int(
                    getattr(self.settings, "max_revise_rounds")
                ):
                    _log_separator(job, f"revision round {env.round + 1}")
                    followup_reason = await self.freebuff.followup(
                        env, build_followup_text(job.text, notes), job.log_path
                    )
                    reason = followup_reason
                    if followup_reason == "timeout":
                        await self.freebuff.abort(env)
                        return (
                            False,
                            f"Freebuff timed out during revision on {env.branch}. Log: {job.log_path.name}",
                        )
                    continue

                if verdict == "revise":
                    verdict = "ask_user"
                    notes = (
                        "The automatic revision limit was reached. "
                        "The last review notes were:\n\n" + notes
                    )

                await self.freebuff.abort(env)
                outcome = ReviewOutcome(
                    job=job,
                    verdict=verdict,
                    notes=notes,
                    environment=env,
                    check_state=check_state,
                    test_state=test_state,
                    diff_lines=changes.diff_lines,
                    changed_files=tuple(all_paths),
                )
                await self.post_review(outcome)
                return verdict == "approve", f"review: {verdict}"
        except asyncio.CancelledError:
            if env is not None:
                await self.freebuff.abort(env)
            raise
        except FreebuffError as exc:
            if env is not None:
                await self.freebuff.abort(env)
            return False, f"Freebuff job failed: {exc}"

    async def _run_stage(
        self, job: Job, env: TaskEnvironment, kind: str
    ) -> tuple[str, str, bool | None]:
        script = SCRIPT_DIR / f"{kind}.sh"
        _log_separator(job, f"/{kind} in {env.worktree}")
        if not script.is_file():
            return "missing script", f"scripts/{script.name} not found", False
        outcome = await _exec(
            ["/bin/bash", str(script)],
            cwd=env.worktree,
            env=build_script_env(self.settings, env.worktree),
            timeout=_stage_timeout(self.settings, kind),
            log_path=job.log_path,
            max_tail=120,
        )
        tail = "\n".join(outcome.tail[-40:])
        if kind == "test" and outcome.code == 78:
            state = "unavailable (scheme has no test action)"
            return state, tail, None
        if outcome.timed_out:
            state = "timed out"
            return state, tail, False
        state = "passed" if outcome.code == 0 else f"failed (exit {outcome.code})"
        return state, tail, outcome.code == 0
