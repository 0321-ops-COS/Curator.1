"""A small index of finished jobs, so /jobs is not git archaeology.

Every other view is about one job at a time: the board for what runs now,
``/logs`` for one id, ``/review`` for cards that are waiting. Nothing answered
"what have I asked for lately, and where did each one end up", which is the
question a phone actually has.

One JSON line per finished job is appended to ``LOG_DIR/jobs.jsonl`` and the last
few are read back. A broken or half-written line is skipped rather than raised,
the file is capped, and the index never carries a diff, a token or job text
beyond a clipped title.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import discord

log = logging.getLogger("macbuilder")

INDEX_FILE = "jobs.jsonl"
MAX_ROWS = 500
DEFAULT_LIMIT = 10
MAX_TITLE = 90
MAX_RENDER = 1900


def path_for(log_dir: Path | str) -> Path:
    return Path(log_dir) / INDEX_FILE


def _row(job: object, outcome: object | None, now: float) -> dict:
    env = getattr(outcome, "environment", None) if outcome is not None else None
    return {
        "id": str(getattr(job, "id", "")),
        "kind": str(getattr(job, "kind", "")),
        "project": str(getattr(job, "project", "")),
        "status": str(getattr(outcome, "verdict", "") if outcome is not None
                      else getattr(job, "status", "")),
        "branch": str(getattr(job, "branch", "") or getattr(env, "branch", "") or ""),
        "title": " ".join(str(getattr(job, "title", "")).split())[:MAX_TITLE],
        "finished": round(float(now), 3),
        "changed": int(getattr(job, "diff_lines", 0) or 0),
    }


def record(settings: object, job: object, *, outcome: object | None = None,
           now: float | None = None) -> Path | None:
    """Append one finished job to the index; never raise into a job."""
    log_dir = getattr(settings, "log_dir", None)
    if log_dir is None or not str(getattr(job, "id", "")):
        return None
    path = path_for(log_dir)
    row = _row(job, outcome, now if now is not None else time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("could not add job %s to the index", row["id"])
        return None
    _trim(path)
    return path


def _trim(path: Path) -> None:
    """Keep the file bounded: an old line is worth less than a small disk."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_ROWS:
        return
    try:
        path.write_text("\n".join(lines[-MAX_ROWS:]) + "\n", encoding="utf-8")
    except OSError:
        log.debug("could not trim the job index")


def recent(settings: object, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """The newest rows, one per job id, newest first."""
    log_dir = getattr(settings, "log_dir", None)
    if log_dir is None:
        return []
    path = path_for(log_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: dict[str, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a half-written line is skipped, never raised
        if not isinstance(row, dict) or not row.get("id"):
            continue
        seen[str(row["id"])] = row
    rows = sorted(seen.values(), key=lambda row: float(row.get("finished") or 0.0),
                  reverse=True)
    return rows[: max(1, int(limit))]


def render(rows: list[dict], *, limit: int = DEFAULT_LIMIT) -> str:
    """The rows as short lines a phone can read; never over Discord's limit."""
    if not rows:
        return ("No finished job is recorded yet. The index is written when a job "
                "reaches its result or its review; /logs reads a log by id.")
    lines = [f"Recent jobs (newest first, {len(rows)}):"]
    for row in rows[:limit]:
        line = (f"`{row.get('id', '?')}` · {row.get('kind', '?')} · "
                f"{row.get('project', '?')} · {row.get('status', '?')}")
        if row.get("branch"):
            line += f" · `{row['branch']}`"
        title = str(row.get("title") or "")
        if title:
            line += f"\n   {title}"
        if len("\n".join([*lines, line])) > MAX_RENDER:
            lines.append("…older jobs are in `jobs.jsonl`.")
            break
        lines.append(line)
    return "\n".join(lines)


async def command(bot, interaction) -> None:
    """Answer /jobs from the index; read-only and ephemeral."""
    if not await bot.require_allowed(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    rows = recent(bot.settings)
    await interaction.followup.send("```\n" + render(rows)[:1900] + "\n```",
                                   ephemeral=True)
