"""What was running when the bot stopped, so a restart can say so.

The queue lives in memory: a restart used to drop a running job without a word,
and the branch, worktree and log it left behind were only found by digging. The
worker writes one small marker when a job starts and removes it when the job
finishes, so a marker found at startup is exactly an interrupted job.

Nothing here approves, pushes or merges, and the file is only ever a note: a
missing, unreadable or half-written marker costs a line, never the startup.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("macbuilder")

MARKER = "running-job.json"
# Longest title kept in the marker; a job's full text is in its own log.
TITLE_CHARS = 120


def path_for(log_dir: Path | str) -> Path:
    """Where the marker lives: beside roadmap.json, in LOG_DIR."""
    return Path(log_dir) / MARKER


def mark(log_dir: Path | str, job, *, now: float | None = None) -> None:
    """Write the job that is running now; never raise into a job.

    The worker calls this as the job starts, so the marker always names the job
    that would be lost if the process died at this instant.
    """
    payload = {
        "id": str(getattr(job, "id", "") or ""),
        "kind": str(getattr(job, "kind", "") or ""),
        "title": str(getattr(job, "title", "") or "")[:TITLE_CHARS],
        "branch": str(getattr(job, "branch", "") or ""),
        "project": str(getattr(job, "project", "") or ""),
        "channel_id": int(getattr(job, "channel_id", 0) or 0),
        "started": float(getattr(job, "started", None) or (now or time.time())),
    }
    if not payload["id"]:
        return
    path = path_for(log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload), "utf-8")
        temp.replace(path)
    except OSError:
        log.warning("could not record the running job in %s", path)


def clear(log_dir: Path | str) -> None:
    """Forget the marker once the job it named has finished."""
    try:
        path_for(log_dir).unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove the running-job marker in %s", log_dir)


def read(log_dir: Path | str) -> dict | None:
    """The interrupted job's marker, or None when there is nothing to report."""
    path = path_for(log_dir)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not str(data.get("id") or ""):
        return None
    return data


def take(log_dir: Path | str) -> dict | None:
    """Read the marker and remove it: a job is announced once, never twice."""
    data = read(log_dir)
    if data is not None:
        clear(log_dir)
    return data


def note(state: dict, *, bot_name: str = "Melisa", worktree_root: Path | str = "") -> str:
    """One plain sentence naming the job, what it left and where to look."""
    job_id = str(state.get("id") or "?")
    kind = str(state.get("kind") or "job")
    started = state.get("started")
    when = ""
    if isinstance(started, (int, float)) and started > 0:
        when = " (started " + time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) + ")"
    lines = [
        f"{bot_name} was stopped while job `{job_id}` ({kind}) was running{when}.",
        "It was not finished, approved or pushed, and it is not queued again.",
    ]
    title = str(state.get("title") or "").strip()
    if title:
        lines.append(f"What it was doing: {title}")
    branch = str(state.get("branch") or "").strip()
    if branch:
        lines.append(f"Branch: `{branch}` (left in place).")
    if str(worktree_root or "").strip():
        lines.append(f"Worktree: `{Path(worktree_root) / job_id}`.")
    lines.append(f"Log: `logs/{job_id}.log`.")
    return "\n".join(lines)
