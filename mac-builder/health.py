"""One read-only answer to "is my bot okay?": the logic behind /health.

Everything here comes from state the bot already holds — the queue snapshot, the
loaded settings, LOG_DIR, the worktree root — plus one disk usage call. Nothing
is probed over the network, nothing is started, and nothing is written, so the
report is safe on a phone and testable without Discord, a Mac or a model.

A number that cannot be read is reported as unknown rather than guessed: a
health report that invents "ok" is worse than no report.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("macbuilder")

# Jobs are named ``YYYYMMDD-HHMMSS-<8 hex>``; anything else under the worktree
# root is not one of ours.
JOB_ID_LEN = 24
MAX_STRAYS = 5
MAX_LOG_AGE_HOURS = 24.0


def format_uptime(seconds: float | None) -> str:
    """Compact age for a phone: 42s, 9m12s, 3h04m, 2d03h."""
    if seconds is None:
        return "unknown"
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_bytes(size: float | None) -> str:
    """Bytes as GB/MB, or unknown."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "unknown"


def disk(path: Path | str) -> tuple[int, int] | None:
    """(free, total) bytes on the volume holding ``path``, or None."""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    return int(usage.free), int(usage.total)


def last_log(log_dir: Path | str) -> tuple[str, float] | None:
    """The newest job log: (job id, seconds since it last changed), or None."""
    try:
        files = [path for path in Path(log_dir).glob("*.log") if path.is_file()]
    except OSError:
        return None
    if not files:
        return None
    newest = max(files, key=lambda path: path.stat().st_mtime)
    return newest.stem, max(0.0, time.time() - newest.stat().st_mtime)


def stray_worktrees(root: Path | str, log_dir: Path | str) -> list[str]:
    """Directories in the worktree root that no job log explains.

    A worktree is expected to be named after the job that made it. One with no
    log is either a leftover from a crash or something a person made; either way
    it is worth naming rather than deleting.
    """
    try:
        entries = [path for path in Path(root).iterdir() if path.is_dir()]
    except OSError:
        return []
    logs = set()
    try:
        logs = {path.stem for path in Path(log_dir).glob("*.log")}
    except OSError:
        pass
    strays = sorted(path.name for path in entries if path.name not in logs)
    return strays[:MAX_STRAYS]


@dataclass
class Snapshot:
    """The facts one report is built from; every field is optional."""

    bot_name: str = "Melisa"
    uptime_seconds: float | None = None
    running: dict | None = None
    waiting: int = 0
    last_job: tuple[str, float] | None = None
    manager_configured: bool = False
    roadmap_left: int = 0
    roadmap_paused: bool = False
    draft_jobs: int = 0
    disk: tuple[int, int] | None = None
    log_dir: str = ""
    strays: list[str] = field(default_factory=list)
    worktree_root: str = ""
    queue_depth: int = 0


def snapshot(bot, *, now: float | None = None) -> Snapshot:
    """Read the live state off a bot object; never raise, never probe."""
    settings = getattr(bot, "settings", None)
    log_dir = Path(getattr(settings, "log_dir", Path.cwd()))
    worktree_root = Path(getattr(settings, "worktree_root", Path.cwd()))
    queue = getattr(bot, "queue", None)
    try:
        state = queue.snapshot() if queue is not None else {}
    except Exception:  # pragma: no cover - a broken queue is reported, not fatal
        log.exception("health: the queue snapshot failed")
        state = {}
    started = getattr(bot, "_started_at", None)
    clock = now if now is not None else time.time()
    uptime = (clock - float(started)) if isinstance(started, (int, float)) else None
    manager = getattr(bot, "manager", None)
    draft = getattr(bot, "_sheet_draft", None)
    steps = getattr(bot, "_roadmap_steps", []) or []
    return Snapshot(
        bot_name=str(getattr(settings, "bot_name", "Melisa") or "Melisa"),
        uptime_seconds=uptime,
        running=state.get("running") if isinstance(state, dict) else None,
        waiting=int(state.get("pending") or 0) if isinstance(state, dict) else 0,
        last_job=last_log(log_dir),
        manager_configured=bool(getattr(manager, "enabled", False)),
        roadmap_left=len([step for step in steps if not getattr(step, "job_id", "")]),
        roadmap_paused=bool(getattr(bot, "_roadmap_paused", False)),
        draft_jobs=len(getattr(draft, "steps", []) or []),
        disk=disk(log_dir),
        log_dir=str(log_dir),
        strays=stray_worktrees(worktree_root, log_dir),
        worktree_root=str(worktree_root),
        queue_depth=len(steps) + int(state.get("pending") or 0) if isinstance(state, dict) else 0,
    )


def report(state: Snapshot) -> str:
    """The whole report as short lines, most important first."""
    lines = [f"{state.bot_name} health · up {format_uptime(state.uptime_seconds)}"]
    running = state.running or {}
    if running:
        elapsed = format_uptime(running.get("elapsed"))
        lines.append(
            f"running: {running.get('id')} ({running.get('kind')}) · {elapsed}"
        )
    else:
        lines.append("running: nothing")
    lines.append(f"waiting: {state.waiting} job(s)")
    if state.last_job:
        job_id, age = state.last_job
        lines.append(f"last log: {job_id} · changed {format_uptime(age)} ago")
    else:
        lines.append("last log: none yet")
    if state.manager_configured:
        lines.append("manager: configured (no probe, nothing sent)")
    else:
        lines.append("manager: NOT configured; reviews and sheets wait for you")
    roadmap = f"roadmap: {state.roadmap_left} job(s) left"
    if state.roadmap_paused:
        roadmap += ", paused"
    lines.append(roadmap)
    if state.draft_jobs:
        lines.append(f"draft sheet: {state.draft_jobs} job(s) not queued")
    if state.disk:
        free, total = state.disk
        lines.append(f"disk: {format_bytes(free)} free of {format_bytes(total)}")
    else:
        lines.append("disk: unknown")
    if state.strays:
        lines.append(f"stray worktrees ({state.worktree_root}): " + ", ".join(state.strays))
    else:
        lines.append("stray worktrees: none")
    return "\n".join(lines)


async def command(bot, interaction) -> None:
    """Answer /health from the live state, ephemeral and read-only."""
    if not await bot.require_allowed(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        text = report(snapshot(bot))
    except Exception as exc:  # pragma: no cover - a report never crashes the bot
        log.exception("health report failed")
        await interaction.followup.send(f"Could not read the bot's health: {exc}", ephemeral=True)
        return
    await interaction.followup.send("```\n" + text[:1900] + "\n```", ephemeral=True)
