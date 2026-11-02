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

import questions
import rules
import screens
import status_board
from freebuff_runner import FreebuffError, FreebuffRunner, TaskEnvironment
from manager import (
    Manager,
    ManagerRateLimited,
    ManagerUnavailable,
    ReviewBrief,
    escalation_reason,
)

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
    # Whether the real stages passed. test_ok=None means /test was unavailable
    # (no test action), which is reported but cannot fail a diff.
    check_ok: bool = True
    test_ok: bool | None = None
    # HEAD plus the staged-change digest the review saw; Approve re-checks it.
    snapshot: str = ""
    violations: tuple[rules.RuleViolation, ...] = ()


@dataclass
class ExecOutcome:
    code: int
    timed_out: bool
    tail: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


PostJob = Callable[[Job, bool, str], Awaitable[None]]
PostReview = Callable[[ReviewOutcome], Awaitable[None]]
PostQuestion = Callable[[Job, questions.Question], Awaitable[bool]]
PostPane = Callable[[Job, str], Awaitable[bool]]
# A screen a person has to deal with, with its sanitized pane text.
PostScreen = Callable[[Job, screens.Screen, str], Awaitable[bool]]
# A manager line for the owner: an auto-answer record or a suggestion.
PostNote = Callable[[Job, str], Awaitable[None]]
# ONE status hook: (job, stage, detail). The board listens here; a failure may
# never fail, delay or alter a job.
StageHook = Callable[[Job, str, str], None]
# Reasons from the runner that mean a question box stopped the job.
QUESTION_REASONS = ("question-timeout", "question-failed")
# The runner's reason for a screen that never changed and was dismissed.
STALL_REASON = "stalled"
# How long advice may take before it is dropped: the question is already posted,
# so a late line would arrive after the owner had answered.
SUGGESTION_WINDOW_SECONDS = 15.0
DIFF_STAT_TIMEOUT_SECONDS = 30.0
MAX_DIFF_STAT_LINES = 60
# The reviews whose Approve could still push, so a stage the runner reports after
# the job's run can name the job it belongs to. A reviewer who never decides
# leaves an entry behind; only the newest few are kept.
MAX_TRACKED_REVIEWS = 16


def _write_job_log(job: Job, text: str) -> None:
    """One auditable line in the job log."""
    try:
        with job.log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + text.rstrip() + "\n")
    except OSError:  # pragma: no cover - a log write must never stop a job
        log.debug("could not write to the log for job %s", job.id)


def _project_block(settings: object) -> str:
    """The manager's project context, empty when startup built none."""
    ctx = getattr(settings, "project", None)
    return ctx.prompt_block() if ctx is not None else ""


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def format_elapsed(seconds: float | None) -> str:
    """Compact elapsed time for Discord: 9s, 4m12s, 1h02m."""
    if seconds is None:
        return "-"
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


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


def build_followup_text(task: str, notes: str, *, dan: bool = False) -> str:
    protected = (
        "Keep the change focused. mac-builder/ is allowed, but the guardrail files "
        "are not: mac-builder/rules.py, mac-builder/freebuff_runner.py, the "
        "allowlist and Approve code in mac-builder/bot.py, and the tests that pin "
        "them (tests/test_rules.py, tests/test_push_guard.py, tests/test_dan.py, "
        "tests/test_approve_recheck.py). Also off limits: .env*, .github/, signing "
        "or provisioning material. Do not push."
        if dan
        else "Keep the change focused. Do not touch protected files (project.yml, "
        "*.xcconfig, .github/, .env*, mac-builder/, signing or provisioning "
        "material). Do not push."
    )
    return (
        "Your previous attempt was reviewed and needs changes before it can be approved.\n\n"
        "Reviewer notes:\n"
        f"{notes.strip()}\n\n"
        "The original task is still:\n"
        f"{task}\n\n"
        f"{protected}"
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
        post_question: PostQuestion | None = None,
        post_pane: PostPane | None = None,
        post_screen: PostScreen | None = None,
        post_note: PostNote | None = None,
        on_stage: StageHook | None = None,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.freebuff = freebuff
        self.post_job = post_job
        self.post_review = post_review
        self.post_question = post_question
        self.post_pane = post_pane
        self.post_screen = post_screen
        self.post_note = post_note
        self.on_stage = on_stage
        self._last_log: Path | None = None
        self._last_summary = ""
        self._last_diff_excerpt = ""
        self._running_env: TaskEnvironment | None = None
        # Branch → job for every review an Approve could still push; see
        # _on_runner_stage.
        self._reviews: dict[str, Job] = {}
        # Auto-answers used by each job (QUESTION_AUTO_ANSWER_MAX) and the
        # moment the manager may be called again after a 429 or a timeout.
        self._auto_answers: dict[str, int] = {}
        self._manager_cooldown_until = 0.0
        # The last parsed answer, reused for a suggestion instead of paying for
        # a second request for the same question.
        self._last_answer: tuple[questions.Question, object] | None = None
        self._suggestion_task: asyncio.Task[None] | None = None

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
            if job.kind in ("task", "dan"):
                ok, summary = await self._run_task(job)
            else:
                ok, summary = await self._run_script(job)
        except FreebuffError as exc:
            summary = f"{job.kind} failed: {exc}"
            log.warning("job %s: %s", job.id, summary)
        except asyncio.CancelledError:
            # A cancel is an outcome the owner must still see, but a review that
            # already posted owns the status: its embed is the record.
            if job.status == "running":
                job.status = "cancelled"
                job.summary = (
                    f"{job.kind} was cancelled; nothing was pushed and the "
                    "branch was left in place."
                )
                self._last_summary = f"{job.kind} {job.id}: cancelled"
                try:
                    await self.post_job(job, False, job.summary)
                except Exception:  # pragma: no cover - Discord can fail independently
                    log.exception("could not post the cancellation for job %s", job.id)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job %s crashed", job.id)
            summary = f"{job.kind} crashed: {exc}"
        finally:
            job.finished = time.time()
            self._running_env = None

        # A review sets its own status (awaiting_review, blocked); the queue must
        # not overwrite the state the owner still has to act on.
        if job.status not in ("awaiting_review", "blocked"):
            job.status = "done" if ok else "failed"
        # _post_review can leave a note the result post must keep; never fold in
        # the same text twice.
        note = job.summary
        if note and note not in summary:
            summary = f"{summary}\n\n{note}" if summary else note
        job.summary = summary
        self._last_summary = f"{job.kind} {job.id}: {summary}"
        if job.status == "cancelled":
            self._stage(job, status_board.CANCELLED, "cancelled; nothing was pushed")
        elif job.status == "failed":
            self._stage(job, status_board.FAILED, summary)
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
        # A screen that needs a person is reported through the same Discord
        # channel as the job, before and during the wait for the first screen.
        self.freebuff.screen_reporter = self._screen_handler(job)
        # The runner reports real stages through this one hook as well.
        self.freebuff.status_reporter = lambda stage, detail: self._stage(
            job, stage, detail
        )
        try:
            env = await self.freebuff.start_task(
                job_id=job.id,
                task_text=job.text,
                log_path=job.log_path,
                kind=job.kind,
            )
            self._running_env = env
            reason = await self.freebuff.wait_for_changes(
                env,
                job.log_path,
                on_question=self._question_handler(job),
                on_pane=self._pane_handler(job),
            )
            if reason in QUESTION_REASONS:
                return await self._question_stopped(job, env, reason)
            if reason == STALL_REASON:
                return await self._stalled(job, env)
            if reason == "timeout":
                # Stop the session before counting: collect() runs git and can
                # raise, and a timed-out Freebuff must not keep running because
                # the count failed.
                self._stage(job, status_board.TIMED_OUT, "hit FREEBUFF_TIMEOUT_SECONDS")
                await self.freebuff.abort(env)
                try:
                    changes = await self.freebuff.collect(
                        env, int(getattr(self.settings, "max_diff_lines"))
                    )
                except Exception:
                    log.exception("could not count the changes for job %s", job.id)
                    detail = "changes could not be counted"
                else:
                    detail = (
                        f"{changes.diff_lines} changed lines, not reviewed"
                        if not changes.empty
                        else "no changes"
                    )
                return (
                    False,
                    f"Freebuff timed out on {env.branch} ({detail}); the session was "
                    f"stopped. Log: {job.log_path.name}",
                )

            while True:
                changes = await self.freebuff.collect(env, int(getattr(self.settings, "max_diff_lines")))
                self._last_diff_excerpt = changes.diff[:4000]
                if changes.empty:
                    await self.freebuff.abort(env)
                    return (
                        False,
                        f"Freebuff finished on {env.branch} without changes "
                        f"({self._reason_detail(reason)}); nothing to review. "
                        f"Log: {job.log_path.name}",
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
                        dan=job.kind == "dan",
                        diff=changes.diff,
                    )
                )
                if violations:
                    self._stage(
                        job, status_board.FAILED, "blocked by the hard rules"
                    )
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
                    project_context=_project_block(self.settings),
                )
                self._stage(
                    job,
                    status_board.MANAGER_REVIEW,
                    f"reviewing {changes.diff_lines} changed lines",
                )
                review = await self.manager.review(brief)
                verdict = review.verdict
                notes = review.notes
                forced = rules.force_revise_reason(
                    check_ok, test_ok, dan=job.kind == "dan"
                )
                if forced:
                    verdict = "revise"
                    notes = forced + (f"\n\n{notes}" if notes else "")

                if verdict == "revise" and env.round < int(
                    getattr(self.settings, "max_revise_rounds")
                ):
                    _log_separator(job, f"revision round {env.round + 1}")
                    followup_reason = await self.freebuff.followup(
                        env,
                        build_followup_text(job.text, notes, dan=job.kind == "dan"),
                        job.log_path,
                        on_question=self._question_handler(job),
                        on_pane=self._pane_handler(job),
                    )
                    reason = followup_reason
                    if followup_reason in QUESTION_REASONS:
                        return await self._question_stopped(job, env, followup_reason)
                    if followup_reason == "timeout":
                        self._stage(
                            job, status_board.TIMED_OUT, "hit FREEBUFF_TIMEOUT_SECONDS"
                        )
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

                self._stage(
                    job,
                    status_board.AWAITING_APPROVE,
                    f"{changes.diff_lines} changed lines, checks {check_state}, "
                    f"tests {test_state}",
                )
                # Approve pushes long after this run returns, so the branch is
                # kept: it is what names this job when the runner reports PUSHED.
                self._remember_review(job, env)
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
                    check_ok=check_ok,
                    test_ok=test_ok,
                    snapshot=changes.snapshot,
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
        finally:
            # The screen reporter belongs to this job: a later job installs its
            # own. The stage reporter cannot end with the run, because the push
            # after Approve is reported through it once the run is long over.
            self.freebuff.screen_reporter = None
            self.freebuff.status_reporter = self._on_runner_stage

    # ------------------------------------------------------------- questions

    def _question_handler(self, job: Job):
        """The callback the runner uses while a question box is open."""

        async def handle(question: questions.Question) -> str | None:
            return await self._on_question(job, question)

        return handle

    def _pane_handler(self, job: Job):
        """The callback for a box that did not parse: show its pane, never idle."""

        async def handle(tail: str) -> None:
            if self.post_pane is None:
                return
            try:
                await self.post_pane(job, tail)
            except Exception:  # Discord must not kill the job
                log.exception("could not post the pane tail for job %s", job.id)

        return handle

    def _remember_review(self, job: Job, env: TaskEnvironment | None) -> None:
        """Keep the branch an Approve will push, so a late PUSHED finds its job."""
        branch = getattr(env, "branch", "") or ""
        if not branch:
            return
        self._reviews.pop(branch, None)
        self._reviews[branch] = job
        while len(self._reviews) > MAX_TRACKED_REVIEWS:
            self._reviews.pop(next(iter(self._reviews)))

    def _reviewed_job(self, detail: str) -> Job | None:
        """The job whose review a stage detail names, and only that one."""
        for branch, job in list(self._reviews.items()):
            # The push reports '<branch> at <sha>', never a bare branch prefix.
            if detail == branch or detail.startswith(branch + " "):
                self._reviews.pop(branch, None)
                return job
        return None

    def _on_runner_stage(self, stage: str, detail: str = "") -> None:
        """A stage the runner reports after the run: the push after Approve.

        The run is over by then (the job is waiting on the owner), so this hook
        belongs to no run: the branch in the detail names the job it is for.
        """
        job = self._reviewed_job(detail)
        if job is None:
            log.debug("a stage reported after the run named no review: %s", stage)
            return
        self._stage(job, stage, detail)

    def _stage(self, job: Job, stage: str, detail: str = "") -> None:
        """Report a real stage through the one hook. Never raises into the job."""
        hook = getattr(self, "on_stage", None)
        if hook is None:
            return
        try:
            hook(job, stage, detail)
        except Exception:  # a status update must never alter a job
            log.exception("status hook failed for job %s", job.id)

    def _screen_handler(self, job: Job):
        """The callback for a screen that needs a person, with its pane text."""

        async def handle(screen: screens.Screen, tail: str) -> None:
            if self.post_screen is None:
                return
            try:
                await self.post_screen(job, screen, tail)
            except Exception:  # Discord must not kill the job
                log.exception("could not post the screen for job %s", job.id)

        return handle

    def _reason_detail(self, reason: str) -> str:
        """'exited' plus why, when the last screen says what happened."""
        note = getattr(self.freebuff, "exit_note", "")
        if reason == "exited" and note:
            return f"exited; {note}"
        return reason

    async def _stalled(self, job: Job, env: TaskEnvironment) -> tuple[bool, str]:
        """A screen that never changed was dismissed with Esc: nothing is reviewed."""
        wait = int(float(getattr(self.settings, "question_wait_seconds", 1800)))
        kind = getattr(self.freebuff, "last_stall_kind", "") or screens.UNKNOWN
        await self.freebuff.abort(env)
        return (
            False,
            f"Freebuff was stuck on {screens.describe(kind)}: the worktree and the "
            f"pane both stopped changing and it never moved within {wait}s "
            f"(QUESTION_WAIT_SECONDS), so it was dismissed with Esc and the job was "
            f"stopped on {env.branch}; nothing is reviewed. The pane is in the log "
            f"and in the screen message. Log: {job.log_path.name}",
        )

    async def _question_stopped(
        self, job: Job, env: TaskEnvironment, reason: str
    ) -> tuple[bool, str]:
        """Esc was sent and the job is over: nothing of it is reviewed."""
        wait = int(float(getattr(self.settings, "question_wait_seconds", 1800)))
        await self.freebuff.abort(env)
        log_file = f"Log: {job.log_path.name}"
        if reason == "question-failed":
            return (
                False,
                "Freebuff's question box did not show the answer where its own pane "
                "said it should be, so it was dismissed with Esc and the job was "
                f"stopped on {env.branch}; nothing is reviewed. The pane after every "
                f"key is in the log when QUESTION_DEBUG=1. {log_file}",
            )
        return (
            False,
            f"Freebuff asked a question that went unanswered for {wait}s "
            f"(QUESTION_WAIT_SECONDS). It was dismissed with Esc and the job was "
            f"stopped on {env.branch}; nothing is reviewed. {log_file}",
        )

    async def _on_question(self, job: Job, question: questions.Question) -> str | None:
        """Auto-answer when the manager is sure, otherwise post it for the owner.

        None means the owner has to answer with /answer; the runner then waits
        for at most QUESTION_WAIT_SECONDS.
        """
        auto = await self._auto_answer(job, question)
        if auto is not None:
            _log_separator(job, "question auto-answered")
            log.info("job %s: auto-answered %r with %r", job.id, question.text, auto)
            return auto
        if self.post_question is None:
            log.warning("job %s: no Discord channel for its question", job.id)
            return None
        try:
            posted = await self.post_question(job, question)
        except Exception:  # Discord can fail without stopping the job
            log.exception("could not post the question for job %s", job.id)
            posted = False
        if not posted:
            log.warning(
                "job %s: the question did not reach Discord; it will expire", job.id
            )
            return None
        # The question is already posted: advice may follow it, and that advice
        # can never hold the question up, block /answer or reach the pane.
        self._stage(job, status_board.WAITING_ON_YOU, screens.QUESTION_BOX)
        self._schedule_suggestion(job, question)
        return None

    @staticmethod
    def _option_lines(question: questions.Question) -> list[str]:
        return [
            f"{option.label} — {option.description}"
            if option.description
            else option.label
            for option in question.options
        ]

    def _manager_name(self) -> str:
        """The manager's name from BOT_NAME, never a hardcoded one."""
        return str(getattr(self.settings, "bot_name", "") or "the manager")

    def _start_cooldown(self, job: Job, exc: BaseException) -> None:
        seconds = float(
            getattr(self.settings, "question_manager_cooldown_seconds", 30.0)
        )
        self._manager_cooldown_until = time.monotonic() + max(0.0, seconds)
        log.warning(
            "job %s: manager throttled (%s); no manager calls for %.0fs",
            job.id,
            exc,
            seconds,
        )
        _write_job_log(
            job,
            f"[manager throttled for {int(seconds)}s; questions go to the owner]",
        )

    async def _current_diff_stat(self) -> str:
        """A short tail of the running job's diff stat; empty when there is none."""
        env = self._running_env
        if env is None:
            return ""
        outcome = await _exec(
            ["git", "-C", str(env.worktree), "--no-pager", "diff", "--stat"],
            cwd=env.worktree,
            env=build_script_env(self.settings, env.worktree),
            timeout=DIFF_STAT_TIMEOUT_SECONDS,
            max_tail=MAX_DIFF_STAT_LINES,
        )
        return "\n".join(outcome.tail)

    async def _audit_answer(
        self, job: Job, question: questions.Question, answer: str, result: object
    ) -> None:
        """Record which option the manager took and why, in Discord and the log."""
        number = questions.option_number(answer, len(question.options))
        if number is not None:
            what = f"option {number}"
        else:
            what = f'"{questions.redact(answer[:120])}"'
        confidence = getattr(result, "confidence", "low")
        reason = questions.redact(str(getattr(result, "reason", "") or "")[:200])
        line = f"{self._manager_name()} answered {what} (confidence {confidence})"
        if reason:
            line += f": {reason}"
        _write_job_log(job, line)
        if self.post_note is None:
            return
        try:
            await self.post_note(job, line)
        except Exception:  # Discord must not kill the job
            log.exception("could not post the auto-answer note for job %s", job.id)

    async def _auto_answer(self, job: Job, question: questions.Question) -> str | None:
        """The manager's high-confidence answer, or None when a human must answer.

        Off unless QUESTION_AUTO_ANSWER=1. The order is fixed: the code gate in
        ``manager.escalation_reason`` on the question and the options, then the
        cooldown and the per-job cap, then the manager call, then the same gate
        on the chosen answer, then the audit post, and only then is the answer
        returned to be typed. Every answer has to clear the gate whatever the
        confidence, at most QUESTION_AUTO_ANSWER_MAX answers are taken per job,
        and a 429 or a timeout stops manager calls for
        QUESTION_MANAGER_COOLDOWN_SECONDS. A malformed reply or anything below
        high confidence falls back to the owner instead of guessing.
        """
        self._last_answer = None
        if not bool(getattr(self.settings, "question_auto_answer", False)):
            return None
        if not getattr(self.manager, "enabled", False):
            return None
        options = self._option_lines(question)
        # The question side of the gate runs first, before the cooldown and the
        # cap: an escalated question never spends a manager call and is never
        # counted as throttled.
        reason = escalation_reason(question.text, *options)
        if reason:
            log.info(
                "job %s: question %d escalated (%s); asking the owner",
                job.id,
                question.number,
                reason,
            )
            _write_job_log(
                job,
                f"[question {question.number} is the owner's call: it mentions "
                f"{reason!r}]",
            )
            return None
        if time.monotonic() < self._manager_cooldown_until:
            log.info("job %s: manager cooldown is on; asking the owner", job.id)
            return None
        cap = int(getattr(self.settings, "question_auto_answer_max", 3))
        used = self._auto_answers.get(job.id, 0)
        if used >= cap:
            log.info("job %s: auto-answer cap reached; asking the owner", job.id)
            _write_job_log(
                job,
                f"[auto-answer cap of {cap} reached; question {question.number} "
                "goes to the owner]",
            )
            return None
        try:
            result = await self.manager.answer_question(
                task=job.text,
                text=question.text,
                options=options,
                plan=str(getattr(job, "plan", "") or ""),
                diff_stat=await self._current_diff_stat(),
                number=question.number,
                total=question.total,
            )
        except ManagerRateLimited as exc:
            self._start_cooldown(job, exc)
            return None
        except ManagerUnavailable as exc:
            log.warning("job %s: auto-answer unavailable: %s", job.id, exc)
            return None
        except Exception as exc:  # an endpoint failure must never kill the job
            log.warning("job %s: auto-answer failed: %s", job.id, exc)
            return None
        self._last_answer = (question, result)
        if not getattr(result, "ok", True):
            log.info("job %s: auto-answer did not parse; asking the owner", job.id)
            return None
        if not getattr(result, "confident", False):
            log.info(
                "job %s: auto-answer was %s confidence; asking the owner",
                job.id,
                getattr(result, "confidence", "low"),
            )
            return None
        answer = questions.validate_answer(result.answer, question.labels)
        if answer is None:
            log.info("job %s: the auto-answer did not fit the box; asking the owner", job.id)
            return None
        chosen = question.labels[int(answer) - 1] if answer.isdigit() else answer
        reason = escalation_reason(chosen)
        if reason:
            log.info(
                "job %s: answer to question %d escalated (%s); asking the owner",
                job.id,
                question.number,
                reason,
            )
            _write_job_log(
                job,
                f"[the manager's answer to question {question.number} is the "
                f"owner's call: it mentions {reason!r}]",
            )
            return None
        self._auto_answers[job.id] = used + 1
        await self._audit_answer(job, question, answer, result)
        return answer

    def _schedule_suggestion(self, job: Job, question: questions.Question) -> None:
        """Start the advice line in the background: it never delays anything."""
        if not bool(getattr(self.settings, "question_suggest", False)):
            return
        if not getattr(self.manager, "enabled", False):
            return
        self._suggestion_task = asyncio.get_running_loop().create_task(
            self._suggest(job, question)
        )

    async def _suggest(self, job: Job, question: questions.Question) -> None:
        """Post advice for the owner, once, inside a short window.

        Never an answer: the line goes to Discord and the job log only, is
        never returned to the runner and is never typed into the pane.
        """
        if time.monotonic() < self._manager_cooldown_until:
            return
        cached = self._last_answer
        try:
            if cached is not None and cached[0] is question:
                result = cached[1]
            else:
                result = await asyncio.wait_for(
                    self.manager.answer_question(
                        task=job.text,
                        text=question.text,
                        options=self._option_lines(question),
                        plan=str(getattr(job, "plan", "") or ""),
                        diff_stat=await self._current_diff_stat(),
                        number=question.number,
                        total=question.total,
                    ),
                    timeout=SUGGESTION_WINDOW_SECONDS,
                )
        except (asyncio.TimeoutError, ManagerUnavailable) as exc:
            log.info("job %s: no suggestion: %s", job.id, exc)
            return
        except Exception as exc:
            log.warning("job %s: suggestion failed: %s", job.id, exc)
            return
        line = self._suggestion_line(question, result)
        if line is None or self.post_note is None:
            return
        _write_job_log(job, line)
        try:
            await self.post_note(job, line)
        except Exception:  # Discord must not kill the job
            log.exception("could not post a suggestion for job %s", job.id)

    def _suggestion_line(
        self, question: questions.Question, result: object
    ) -> str | None:
        """'<BOT_NAME> suggests option N: ... (suggestion only, you decide)'."""
        if not getattr(result, "ok", True):
            return None
        raw = str(getattr(result, "answer", "") or "")
        answer = questions.validate_answer(raw, question.labels)
        if answer is None:
            return None
        number = questions.option_number(answer, len(question.options))
        if number is not None:
            what = f"option {number}"
        else:
            what = f'"{questions.redact(answer[:120])}"'
        reason = questions.redact(str(getattr(result, "reason", "") or "")[:200])
        line = f"{self._manager_name()} suggests {what}"
        if reason:
            line += f": {reason}"
        return f"{line} (suggestion only, you decide)"

    async def _run_stage(
        self, job: Job, env: TaskEnvironment, kind: str
    ) -> tuple[str, str, bool | None]:
        _log_separator(job, f"/{kind} in {env.worktree}")
        self._stage(
            job,
            status_board.CHECKS if kind == "check" else status_board.TESTS,
            f"/{kind} running",
        )
        if job.kind == "dan":
            # A self-build ships Python and shell, not Swift: the Mac's
            # xcodebuild stages would tell her nothing about her own change.
            return await self._run_dan_stage(job, env, kind)
        script = SCRIPT_DIR / f"{kind}.sh"
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

    async def _run_dan_stage(
        self, job: Job, env: TaskEnvironment, stage: str
    ) -> tuple[str, str, bool | None]:
        """The real stages for a self-build: unittest, then py_compile and bash -n."""
        root = env.worktree / "mac-builder"
        cwd = root if root.is_dir() else env.worktree
        if stage == "check":
            outcome = await _exec(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                cwd=cwd,
                env=build_script_env(self.settings, cwd),
                timeout=_stage_timeout(self.settings, stage),
                log_path=job.log_path,
                max_tail=160,
            )
            if outcome.timed_out:
                return "timed out", "\n".join(outcome.tail[-40:]), False
            state = "passed" if outcome.code == 0 else f"failed (exit {outcome.code})"
            return state, "\n".join(outcome.tail[-40:]), outcome.code == 0

        modules = sorted(str(path) for path in cwd.glob("*.py"))
        scripts = sorted(str(path) for path in (cwd / "scripts").glob("*.sh"))
        if not modules:
            return "failed (no modules in mac-builder/)", "", False
        commands = [("py_compile", [sys.executable, "-m", "py_compile", *modules])]
        if scripts:
            commands.append(("bash -n", ["/bin/bash", "-n", *scripts]))
        states: list[str] = []
        tail: list[str] = []
        ok = True
        for label, argv in commands:
            outcome = await _exec(
                argv,
                cwd=cwd,
                env=build_script_env(self.settings, cwd),
                timeout=_stage_timeout(self.settings, stage),
                log_path=job.log_path,
                max_tail=80,
            )
            if outcome.timed_out:
                states.append(f"{label} timed out")
                ok = False
            elif outcome.code != 0:
                states.append(f"{label} failed (exit {outcome.code})")
                ok = False
            else:
                states.append(f"{label} passed")
            tail.extend(outcome.tail[-20:])
        return "; ".join(states), "\n".join(tail), ok
