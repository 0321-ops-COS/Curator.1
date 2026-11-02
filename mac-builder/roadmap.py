"""A build sheet as a queue of jobs: parsed, saved, steered by job status only.

One "## Job" heading starts one job; a "kind: task"/"kind: dan" line picks the
type. Nothing here queues, approves, pushes or merges.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_TASK_CHARS = 6000
HEADING = "## job"
# A job that ended without an approval stops the roadmap.
STOPPED_STATUSES = ("rejected", "failed", "cancelled", "timed_out")


class RoadmapError(ValueError):
    """The sheet cannot become jobs; the message says which rule it broke."""


@dataclass
class Step:
    """One job of the sheet: its task, its kind, and the job it became."""

    text: str
    kind: str = "task"
    job_id: str = ""


def parse(sheet: str) -> list[Step]:
    """The sheet's jobs in order, or RoadmapError naming the rule broken."""
    steps: list[Step] = []
    block, kind, started = [], "task", False

    def flush() -> None:
        nonlocal block, kind
        text = "\n".join(block).strip()
        if text:
            steps.append(Step(text, kind))
        block, kind = [], "task"

    for raw in (sheet or "").splitlines():
        line = raw.rstrip()
        head = line.strip().lower()
        if head.startswith(HEADING) and head[len(HEADING):][:1] in ("", " ", "\t", ":"):
            flush()
            started = True
        elif started and head in ("kind: task", "kind: dan"):
            kind = head.split(":", 1)[1].strip()
        elif started:
            block.append(line)
    flush()

    if not steps:
        raise RoadmapError('no "## Job" heading with a task under it')
    for number, step in enumerate(steps, start=1):
        if len(step.text) > MAX_TASK_CHARS:
            raise RoadmapError(f"job {number} is {len(step.text)} chars; limit {MAX_TASK_CHARS}")
    return steps


# Discord rejects a message over 2000 characters, so a preview stays under this.
PREVIEW_LIMIT = 1900
PREVIEW_SNIPPET = 50


def preview(steps: list[Step]) -> str:
    """The sheet as a numbered plan: kind and the first 50 chars; nothing saved.

    A long sheet cannot fit one message: the header still counts every job, rows
    are dropped from the end once the plan would pass PREVIEW_LIMIT, and the last
    line says how many jobs were left out.
    """
    rows = [f"{number}. `{step.kind}` " + " ".join(step.text.split())[:PREVIEW_SNIPPET]
            for number, step in enumerate(steps, start=1)]
    lines = [f"Preview: {len(rows)} job(s), nothing queued or saved"]
    for index, row in enumerate(rows):
        omitted = len(rows) - index - 1
        more = [f"...and {omitted} more job(s)"] if omitted else []
        if len("\n".join([*lines, row, *more])) > PREVIEW_LIMIT:
            break
        lines.append(row)
    shown = len(lines) - 1
    if shown < len(rows):
        lines.append(f"...and {len(rows) - shown} more job(s)")
    return "\n".join(lines)


def save(path: Path, steps: list[Step], *, channel_id: int = 0,
         requester_id: int = 0) -> None:
    """Write the roadmap and where it started, so a restart can resume it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel_id": int(channel_id),
        "requester_id": int(requester_id),
        "steps": [asdict(step) for step in steps],
    }
    path.write_text(json.dumps(payload, indent=2), "utf-8")


def load(path: Path) -> tuple[list[Step], int, int]:
    """The saved roadmap, its channel and requester: ([], 0, 0) if unusable."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return [], 0, 0
    items = data.get("steps") if isinstance(data, dict) else data  # a bare list is an old file
    if not isinstance(items, list):
        return [], 0, 0
    steps: list[Step] = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else None
        kind = (item.get("kind") if isinstance(item, dict) else None) or "task"
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_TASK_CHARS or kind not in ("task", "dan")):
            return [], 0, 0
        steps.append(Step(text, kind, str(item.get("job_id") or "")))
    try:
        return steps, int(data.get("channel_id") or 0), int(data.get("requester_id") or 0)
    except (AttributeError, TypeError, ValueError):
        return steps, 0, 0
