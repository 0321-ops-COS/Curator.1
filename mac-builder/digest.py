"""One message per finished job: what happened, what changed, what needs you.

A finished job used to end as a board plus a result line, so "what changed, what
is blocked, what do I decide" had to be reconstructed from three places. The
digest is that one message: it quotes only what the job already recorded (the
review outcome or the job's own fields) and labels anything it did not check.

Nothing here re-runs a stage, reads a log, starts a process or calls a model, so
it is testable without Discord, a Mac or a network.
"""

from __future__ import annotations

import time

BUILDER_KINDS = ("task", "dan", "audit")
MAX_NOTES = 600
MAX_LINE = 1800
DECIDED = ("approved", "rejected", "done", "failed", "cancelled", "timed_out", "blocked")
NEEDS_YOU = {
    "awaiting_review": "Approve or reject on the review card; /merge only after it is pushed.",
    "blocked": "Nothing: a hard rule refused this change, so there is no card to approve.",
    "approved": "Run /merge once CI is green; the merge itself is never automatic.",
    "rejected": "Nothing. The branch and worktree are kept for inspection.",
    "failed": "Nothing, or re-run it after the log's error is understood.",
    "cancelled": "Nothing; cancelling kept the branch and pushed nothing.",
    "timed_out": "Nothing, or re-run with a narrower task.",
    "merged": "Nothing: the branch is in the base.",
}


def is_builder(job: object) -> bool:
    """A job that goes through the builder and its review, not a fixed script."""
    return str(getattr(job, "kind", "")) in BUILDER_KINDS


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _stage_line(job: object, outcome: object | None) -> str:
    """check/test/build states, from the outcome when there is one."""
    if outcome is None:
        state = str(getattr(job, "check_state", "") or "")
        return f"check: {state}" if state else "stages: see the job log"
    parts = [
        f"check: {getattr(outcome, 'check_state', '') or 'not run'}",
        f"test: {getattr(outcome, 'test_state', '') or 'not run'}",
    ]
    build = str(getattr(outcome, "build_state", "") or "")
    if build and build != "not run":
        parts.append(f"build: {build}")
    return " · ".join(parts)


def _changed(job: object, outcome: object | None) -> str:
    if outcome is not None:
        files = tuple(getattr(outcome, "changed_files", ()) or ())
        lines = int(getattr(outcome, "diff_lines", 0) or 0)
    else:
        files = tuple(getattr(job, "changed_paths", ()) or ())
        lines = int(getattr(job, "diff_lines", 0) or 0)
    if not files and not lines:
        return "changed: nothing recorded"
    shown = ", ".join(files[:4])
    if len(files) > 4:
        shown += f", +{len(files) - 4} more"
    return f"changed: {len(files)} file(s), {lines} line(s)" + (f" — {shown}" if shown else "")


def build(job: object, outcome: object | None = None, *, bot_name: str = "Melisa",
          roadmap_next: str = "", now: float | None = None) -> str:
    """The whole digest for one finished job, under Discord's message limit."""
    clock = now if now is not None else time.time()
    stamp = time.strftime("%H:%M", time.localtime(clock))
    status = str(getattr(job, "status", "") or "")
    verdict = str(getattr(outcome, "verdict", "") or "") if outcome is not None else ""
    head = (f"**{bot_name} digest · `{getattr(job, 'id', '?')}` · "
            f"{getattr(job, 'kind', '?')} · {getattr(job, 'project', '?')}**")
    lines = [head, f"outcome: {verdict or status or 'unknown'} · finished {stamp}"]

    title = _clip(getattr(job, "title", "") or "", 120)
    if title:
        lines.append(f"asked for: {title}")
    lines.append(_changed(job, outcome))
    lines.append(_stage_line(job, outcome))

    branch = str(getattr(job, "branch", "") or "")
    if not branch and outcome is not None:
        env = getattr(outcome, "environment", None)
        branch = str(getattr(env, "branch", "") or "")
    if branch:
        lines.append(f"branch: `{branch}`")

    notes = str(getattr(outcome, "notes", "") or "") if outcome is not None else ""
    if notes:
        lines.append(f"why: {_clip(notes, MAX_NOTES)}")

    # The job's own status is the state after the review; the verdict is why.
    # A blocked change has no card to approve, whatever the job's status says.
    needs = NEEDS_YOU.get(verdict if verdict == "blocked" else status) or NEEDS_YOU.get(
        verdict, "")
    if not needs and status in DECIDED:
        needs = "Nothing; the result is above."
    lines.append(f"needs you: {needs or 'nothing recorded'}")

    if roadmap_next:
        lines.append(f"next: {_clip(roadmap_next, 160)}")
    else:
        lines.append("next: no roadmap queued")

    if str(getattr(job, "kind", "")) == "dan" and (
            status in ("awaiting_review", "approved", "merged", "done")
            or verdict == "approve"):
        lines.append("restart: this edits my own code, so it runs only after a restart.")
    lines.append("_nothing was re-run for this digest; it quotes the job's own record._")
    text = "\n".join(lines)
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1] + "…"
