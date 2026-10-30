"""Material for /plan, and where the plan file is written.

A plan request is read-only. It creates no job, starts no tmux session, touches
no worktree and never edits ``mac-builder/``; the only output is one markdown
file under ``LOG_DIR/plans/``.

Everything here is filesystem-only, so it can be tested without Discord, a model
or a Mac.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PLAN_DIR_NAME = "plans"
MAX_PLAN_GOAL_CHARS = 500
MAX_RECENT_JOBS = 6
MAX_ERROR_LINES_PER_JOB = 4
MAX_EVIDENCE_CHARS = 300
MAX_FILE_MAP_ENTRIES = 40
GAP_SECTIONS = ("What is not verified here", "Limitations worth knowing")

JOB_LOG_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failure|traceback|timed out|not found|no such|refused)\b"
)


@dataclass(frozen=True)
class PlanInputs:
    """What the manager is allowed to see when it writes a plan."""

    goal: str
    project_block: str
    recent: tuple[str, ...]
    file_map: tuple[str, ...]
    known_gaps: tuple[str, ...]


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_tail(path: Path, limit: int = 40_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    return ANSI_RE.sub("", data.decode("utf-8", "replace"))


def recent_failures(
    log_dir: Path,
    *,
    limit: int = MAX_RECENT_JOBS,
    error_lines: int = MAX_ERROR_LINES_PER_JOB,
) -> tuple[str, ...]:
    """One line per recent job log: its last line plus any error lines.

    Newest first, job logs only, so a stray ``notes.txt`` in LOG_DIR is ignored.
    A log with no error line still contributes its last line, because "the last
    job ended with this" is the summary a plan needs.
    """
    if not log_dir.is_dir():
        return ()
    candidates: list[tuple[float, Path]] = []
    for path in log_dir.glob("*.log"):
        if not JOB_LOG_RE.match(path.stem):
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    entries: list[str] = []
    for _mtime, path in sorted(candidates, reverse=True)[:limit]:
        lines = [line.strip() for line in _read_tail(path).splitlines() if line.strip()]
        if not lines:
            entries.append(f"{path.stem}: (empty log)")
            continue
        entry = f"{path.stem}: last line: {_clip(lines[-1])}"
        errors = [line for line in lines if ERROR_RE.search(line)][-error_lines:]
        if errors:
            entry += " | errors: " + " ; ".join(_clip(line) for line in errors)
        entries.append(entry)
    return tuple(entries)


def file_map(target_dir: Path, *, limit: int = MAX_FILE_MAP_ENTRIES) -> tuple[str, ...]:
    """A shallow map of the checkout, so the plan names real paths only."""
    target = Path(target_dir)
    if not target.is_dir():
        return ()
    entries: list[str] = []
    for child in sorted(target.iterdir()):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        if child.name == "mac-builder":
            entries.append("mac-builder/ (protected: the plan must not touch it)")
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= limit:
            return tuple(entries)
    for folder in ("ios-app", "HustlerCore"):
        base = target / folder
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.name.startswith("."):
                continue
            entries.append(f"{folder}/" + child.name + ("/" if child.is_dir() else ""))
            if len(entries) >= limit:
                return tuple(entries)
    return tuple(entries)


def known_gaps(readme: Path) -> tuple[str, ...]:
    """The bullet list from the README's two gap sections."""
    try:
        text = Path(readme).read_text(encoding="utf-8")
    except OSError:
        return ()
    wanted = {name.lower() for name in GAP_SECTIONS}
    gaps: list[str] = []
    collecting = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip().lower()
            collecting = title in wanted
            continue
        if collecting and stripped.startswith("- "):
            gaps.append(_clip(stripped[2:], 240))
    return tuple(gaps)


def gather(
    *,
    goal: str,
    project_block: str,
    log_dir: Path,
    target_dir: Path,
    readme: Path,
) -> PlanInputs:
    return PlanInputs(
        goal=(goal or "").strip()[:MAX_PLAN_GOAL_CHARS],
        project_block=(project_block or "").strip(),
        recent=recent_failures(Path(log_dir)),
        file_map=file_map(Path(target_dir)),
        known_gaps=known_gaps(Path(readme)),
    )


def plan_document(
    goal: str,
    body: str,
    *,
    bot_name: str = "Melisa",
    created: datetime | None = None,
) -> str:
    """Wrap the manager's markdown so the file always names the goal and its origin."""
    stamp = (created or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = (body or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("# "):
        title = lines[0].lower()
        if "task for dan" in title or title.strip("# ").strip() == goal.strip().lower():
            lines = lines[1:]
    body_text = "\n".join(lines).strip()
    header = (
        f"# Task for Dan — {goal.strip()}\n\n"
        f"_Written by {bot_name} on {stamp}. Every claim below is labelled "
        "`verified` or `assumed`; nothing was built or pushed for this plan._\n"
    )
    return header + ("\n" + body_text if body_text else "") + "\n"


def write_plan(log_dir: Path, text: str, *, created: datetime | None = None) -> Path:
    """Save the plan as LOG_DIR/plans/<timestamp>.md and return the path."""
    directory = Path(log_dir) / PLAN_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (created or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}.md"
    counter = 2
    while path.exists():
        path = directory / f"{stamp}-{counter}.md"
        counter += 1
    path.write_text(text, encoding="utf-8")
    return path
