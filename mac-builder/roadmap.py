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


def preview(steps: list[Step]) -> str:
    """The sheet as a numbered plan: kind and the first 80 chars; nothing saved."""
    rows = [f"{number}. `{step.kind}` " + " ".join(step.text.split())[:80]
            for number, step in enumerate(steps, start=1)]
    return "\n".join([f"Preview: {len(rows)} job(s), nothing queued or saved", *rows])


def save(path: Path, steps: list[Step]) -> None:
    """Write the roadmap, so a restart can say what was queued."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(step) for step in steps], indent=2), "utf-8")
