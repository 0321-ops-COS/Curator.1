"""Read-only /audit: the Mac stages on a detached worktree per branch, one report.

Nothing is committed, pushed, merged or approved and the Hustler checkout is never
touched. A stage is passed, failed, unavailable or not run: a missing tool is
unavailable and a stage that never ran is never a pass. Failures are parsed from the
logs with plain patterns, never by asking the model, and CI is never queried.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from manager import ManagerUnavailable

log = logging.getLogger(__name__)
KIND = "audit"
STAGES = ("check", "build", "test", "drift")
PASSED, FAILED, UNAVAILABLE, NOT_RUN = "passed", "failed", "unavailable", "not run"
REVIEWED = ("awaiting_review", "approved")  # the reviews not merged yet
TAIL_LINES, MAX_FAILURES = 80, 20
UNAVAILABLE_CODES = {127: "tool not installed", 78: "no test action"}
ERROR_RE = re.compile(r"^(?P<where>\S+):(?P<line>\d+): error: (?P<what>.+?)\s*$")
XCTEST_RE = re.compile(r"^Test Case '-\[(?P<case>[^\]]+)\]' failed")
SWIFT_TEST_RE = re.compile(r"^\s*✘ Test (?P<case>\S+)\(\) recorded an issue")


@dataclass
class StageResult:
    name: str
    state: str = NOT_RUN
    code: int | None = None
    seconds: float = 0.0
    tail: list[str] = field(default_factory=list)
    note: str = ""
    cause: str = ""  # the model's opinion, never instead of the raw errors
    cause_error: str = ""


@dataclass
class TargetResult:
    branch: str
    stages: list[StageResult] = field(default_factory=list)
    error: str = ""
    diff_stat: str = ""


def safe_name(branch: str) -> str:
    """A folder name under the stamp, so a branch cannot escape its own worktree."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.") or "branch"


def reviewed(settings: object) -> list[tuple[str, str]]:
    """``(branch, status)`` of every saved review that is awaiting or approved."""
    found: list[tuple[str, str]] = []
    for path in sorted(Path(getattr(settings, "log_dir")).glob("*.review.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        env, status = (data.get("env"), data.get("status")) if isinstance(data, dict) else ({}, "")
        if isinstance(env, dict) and env.get("branch") and status in REVIEWED:
            found.append((str(env["branch"]), str(status)))
    return found


def targets(base: str, *, requested: str = "", reviewed=(), skip=()) -> list[str]:
    """The branches to build: one requested, else the base plus every unmerged review."""
    if requested:
        return [requested]
    chosen = [base]
    for branch, status in reviewed:
        if (branch and status in REVIEWED and branch not in skip
                and branch not in chosen):
            chosen.append(str(branch))
    return chosen


def classify(code: int | None, *, timed_out: bool = False) -> tuple[str, str]:
    """``(state, note)`` for one exit code; a kill or a timeout is never a pass."""
    if timed_out or code is None:
        return FAILED, "timed out"
    if code == 0:
        return PASSED, ""
    return (UNAVAILABLE, UNAVAILABLE_CODES[code]) if code in UNAVAILABLE_CODES else (FAILED, "")


def _lines(raw: object) -> list[str]:
    return (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")).splitlines()


def run_stage(name: str, argv: list[str], cwd: Path, env: dict[str, str], timeout: float,
              required: Path | None = None) -> StageResult:
    """Run one stage to completion, keeping its exit code, duration and tail."""
    if required is not None and not Path(required).exists():
        return StageResult(name, NOT_RUN, note=f"{Path(required).name} is not in this checkout")
    started = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        tail = _lines(exc.stdout)[-TAIL_LINES:] + _lines(exc.stderr)[-TAIL_LINES:]
        return StageResult(name, FAILED, None, time.monotonic() - started, tail,
                           f"timed out after {int(timeout)}s")
    except OSError as exc:
        return StageResult(name, NOT_RUN, None, time.monotonic() - started, [],
                           f"not runnable: {exc}")
    state, note = classify(done.returncode)
    return StageResult(name, state, done.returncode, time.monotonic() - started,
                       _lines(f"{done.stdout or ''}\n{done.stderr or ''}")[-TAIL_LINES:], note)


def parse_failures(raw: object) -> list[str]:
    """Compiler errors and failing test names from a log, in order, deduplicated."""
    found: list[str] = []
    for line in _lines(raw):
        match = ERROR_RE.match(line.strip())
        if match:
            item = f"`{match['where']}:{match['line']}` error: {match['what']}"
        else:
            match = XCTEST_RE.match(line.strip()) or SWIFT_TEST_RE.match(line.strip())
            if match is None:
                continue
            item = f"failed test {match['case'].strip()}"
        if item not in found:
            found.append(item)
    return found[:MAX_FAILURES]


async def add_causes(manager, results: list[TargetResult]) -> None:
    """One plain-words opinion per failed stage; the raw errors always stay."""
    for result in results:
        for stage in result.stages:
            if stage.state != FAILED:
                continue
            try:
                stage.cause = await manager.cause(
                    stage=stage.name, errors=parse_failures("\n".join(stage.tail)),
                    tail="\n".join(stage.tail), diff_stat=result.diff_stat)
            except ManagerUnavailable:
                stage.cause_error = "manager unavailable"
            except Exception:  # an opinion must never fail an audit
                log.exception("could not get a cause for %s", stage.name)
                stage.cause_error = "manager error"


JOB_LIMIT = 4000
RULES = """Rules for this job
- Do not change project.yml, *.xcconfig, .github/, .env* or signing/provisioning files.
- Keep the change under 300 lines.
- Report the real test results; never weaken, skip or delete a test."""


def _job(stage: StageResult, result: TargetResult, stamp: str) -> str:
    """One sheet job: the stage, its exact errors, the branch and what to change."""
    errors = parse_failures("\n".join(stage.tail)) or stage.tail[-8:]
    body = "\n".join([
        "## Job", "kind: task",
        f"Fix the {stage.name} failure on `{result.branch}`",
        f"Audit `{stamp}` ran {stage.name} on `{result.branch}` and it failed.",
        "Exact error lines:", *[f"- {item}" for item in errors],
        "What to change: fix the errors above and rerun the same stage; if you cannot, "
        "say plainly what is still wrong instead of changing the test."])
    return (body[:JOB_LIMIT - len(RULES) - 2].rstrip() + "\n\n" + RULES)[:JOB_LIMIT]


def fix_sheet(stamp: str, results: list[TargetResult], base: str) -> str:
    """One job per distinct failure, the authorized branch's first; '' if none."""
    jobs: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for result in sorted(results, key=lambda item: item.branch != base):
        broken = [stage for stage in result.stages if stage.state == FAILED]
        if result.error:
            broken.append(StageResult("worktree", FAILED, tail=[result.error]))
        for stage in broken:
            key = (result.branch, stage.name, *parse_failures("\n".join(stage.tail)))
            if key in seen:
                continue
            seen.add(key)
            jobs.append(_job(stage, result, stamp))
    return "\n\n".join(jobs)


def report(stamp: str, results: list[TargetResult], base: str) -> str:
    """The markdown report: a table per branch, then one section per state."""
    lines = [f"# Audit {stamp}", "",
             f"Base `{base}`. Each branch ran in a detached worktree under `audit-{stamp}`; "
             "the checkout was not touched and nothing was committed, pushed or merged.", "",
             "CI results are not visible from here: GitHub Actions was never queried, so a "
             "green stage below is this machine only.", ""]
    buckets: dict[str, list[str]] = {}
    for result in results:
        lines += [f"## `{result.branch}`", ""]
        if result.error:
            lines += [f"Worktree failed: {result.error}", ""]
        by_name = {stage.name: stage for stage in result.stages}
        lines += ["| stage | state | exit | time | note |", "| --- | --- | --- | --- | --- |"]
        for name in STAGES:
            stage = by_name.get(name) or StageResult(name)
            code = "-" if stage.code is None else stage.code
            lines.append(f"| {name} | {stage.state} | {code} | {stage.seconds:.0f}s | {stage.note} |")
            buckets.setdefault(stage.state, []).append(f"`{result.branch}` {name}")
        failures = parse_failures("\n".join("\n".join(stage.tail) for stage in result.stages))
        lines += ["", "Failures:", *[f"- {item}" for item in failures]] if failures else [""]
        for stage in result.stages:
            if stage.cause:
                lines += [f"- likely cause (model opinion) for {stage.name}: {stage.cause}"]
            elif stage.cause_error:
                lines += [f"- likely cause (model opinion) for {stage.name}: "
                          f"{stage.cause_error}; see the raw errors above."]
    for state, title in ((PASSED, "Passed"), (FAILED, "Failed"), (UNAVAILABLE, "Unavailable"),
                         (NOT_RUN, "Not run")):
        lines += ["", f"## {title}", ""]
        lines += [f"- {item}" for item in buckets.get(state, [])] or ["- none"]
    return "\n".join(lines + ["", "Unavailable means a missing tool or no test action; not a pass."]) + "\n"


def summary(stamp: str, results: list[TargetResult], filename: str) -> str:
    """The message that carries the report: counts per state and the failed branches."""
    counts: dict[str, int] = {}
    failed: list[str] = []
    for result in results:
        by_name = {stage.name: stage for stage in result.stages}
        for name in STAGES:
            state = (by_name.get(name) or StageResult(name)).state
            counts[state] = counts.get(state, 0) + 1
        if result.error or any(stage.state == FAILED for stage in result.stages):
            failed.append(f"`{result.branch}`")
    head = (f"**Audit `{stamp}`** — {len(results)} branch(es), {len(STAGES)} stages."
            f"\npassed {counts.get(PASSED, 0)} · failed {counts.get(FAILED, 0)} · "
            f"unavailable {counts.get(UNAVAILABLE, 0)} · not run {counts.get(NOT_RUN, 0)}")
    return (head + (f"\nFailures on: {', '.join(failed)}." if failed else "")
            + f"\nFull report: `{filename}`.\nRead-only: nothing committed, pushed, merged "
            "or approved. CI results are not visible from here.")[:1900]
