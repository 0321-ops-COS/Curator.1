"""What /tidy would remove, and the one removal it is allowed to do.

Old logs and leftover worktrees piled up with no way to see them from a phone.
The reports existed in pieces — ``/health`` names stray worktrees, the README
tells you to remove worktrees by hand — but nothing listed what is stale and
nothing ever cleaned it.

The report lists old job logs, worktrees that no job log explains, and leftover
audit folders. Only old logs are deleted, and only after a button press: a branch
is a record of what was built, and a rejected worktree is kept on purpose, so
those are named with the exact command to run instead of being removed here.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord

import health

log = logging.getLogger("macbuilder")

KEEP_DAYS = 14.0
MAX_LISTED = 8
MAX_RENDER = 1800


@dataclass
class Tidy:
    """What one scan found, and the size of what a clean would delete."""

    log_dir: str = ""
    worktree_root: str = ""
    keep_days: float = KEEP_DAYS
    old_logs: list[Path] = field(default_factory=list)
    stray_worktrees: list[str] = field(default_factory=list)
    audit_dirs: list[str] = field(default_factory=list)
    log_bytes: int = 0
    busy_id: str = ""

    @property
    def nothing_to_do(self) -> bool:
        return not (self.old_logs or self.stray_worktrees or self.audit_dirs)


def scan(settings: object, *, now: float | None = None, keep_days: float = KEEP_DAYS,
         busy_id: str = "") -> Tidy:
    """Find what is stale; read-only, and never the running job's own log."""
    clock = now if now is not None else time.time()
    log_dir = Path(str(getattr(settings, "log_dir", ".")))
    root = Path(str(getattr(settings, "worktree_root", ".")))
    state = Tidy(log_dir=str(log_dir), worktree_root=str(root),
                 keep_days=float(keep_days), busy_id=busy_id)
    cutoff = clock - max(0.0, float(keep_days)) * 86400.0
    try:
        logs = [path for path in log_dir.glob("*.log") if path.is_file()]
    except OSError:
        logs = []
    for path in logs:
        if busy_id and path.stem == busy_id:
            continue  # the running job's log is being written right now
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_mtime < cutoff:
            state.old_logs.append(path)
            state.log_bytes += int(info.st_size)
    state.old_logs.sort(key=lambda path: path.stat().st_mtime)
    state.stray_worktrees = health.stray_worktrees(root, log_dir, limit=MAX_LISTED)
    try:
        entries = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        entries = []
    for path in entries:
        if not path.name.startswith("audit-"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                state.audit_dirs.append(path.name)
        except OSError:
            continue
    state.audit_dirs.sort()
    return state


def report(state: Tidy) -> str:
    """The scan as short lines, saying what is deleted and what is only named."""
    lines = [f"tidy · older than {state.keep_days:g} day(s)"]
    if state.old_logs:
        shown = ", ".join(f"`{path.name}`" for path in state.old_logs[:MAX_LISTED])
        more = len(state.old_logs) - MAX_LISTED
        lines.append(f"old logs ({len(state.old_logs)}, "
                     f"{health.format_bytes(state.log_bytes)}): {shown}"
                     + (f", and {more} more" if more > 0 else ""))
    else:
        lines.append("old logs: none")
    if state.stray_worktrees:
        lines.append("stray worktrees (no job log): "
                     + ", ".join(state.stray_worktrees)
                     + f"\n   remove one with `git worktree remove {state.worktree_root}/<name>`")
    else:
        lines.append("stray worktrees: none")
    if state.audit_dirs:
        lines.append("leftover audit folders (named only): " + ", ".join(state.audit_dirs[:MAX_LISTED]))
    lines.append(
        "only old logs are deleted; branches and worktrees are never removed here."
    )
    return "\n".join(lines)


def clean_logs(paths: list[Path], log_dir: Path | str) -> tuple[list[str], list[str]]:
    """Delete the given ``*.log`` files inside LOG_DIR; refuse anything else."""
    root = Path(log_dir).resolve()
    removed: list[str] = []
    failed: list[str] = []
    for path in paths:
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
        except OSError:
            failed.append(str(path))
            continue
        if resolved.suffix != ".log" or root not in resolved.parents:
            failed.append(str(path))  # not ours, or not a log: never deleted
            continue
        try:
            resolved.unlink()
            removed.append(resolved.name)
        except OSError:
            failed.append(resolved.name)
    return removed, failed


class TidyView(discord.ui.View):
    """One button, one action: delete the old logs the report listed."""

    def __init__(self, bot, state: Tidy) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.paths = list(state.old_logs)

    @discord.ui.button(label="Delete old logs", style=discord.ButtonStyle.danger)
    async def clean(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot.require_allowed(interaction):
            return
        removed, failed = clean_logs(self.paths, self.bot.settings.log_dir)
        button.disabled = True
        self.paths = []
        text = f"Deleted {len(removed)} old log(s)."
        if failed:
            text += f" Could not delete {len(failed)}: {', '.join(failed[:5])}."
        text += " No branch and no worktree was touched."
        await interaction.response.edit_message(content=text[:MAX_RENDER], view=self)


async def command(bot, interaction) -> None:
    """/tidy: the report, with the log deletion behind one button press."""
    if not await bot.require_allowed(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    running = getattr(bot.queue, "running", None)
    try:
        state = scan(bot.settings, busy_id=getattr(running, "id", "") or "")
    except Exception as exc:  # a scan never crashes the bot
        log.exception("tidy scan failed")
        await interaction.followup.send(f"Could not scan for stale files: {exc}",
                                       ephemeral=True)
        return
    body = "```\n" + report(state)[:MAX_RENDER] + "\n```"
    if state.old_logs:
        await interaction.followup.send(
            body + f"\nPress the button to delete {len(state.old_logs)} old log(s).",
            view=TidyView(bot, state), ephemeral=True)
        return
    await interaction.followup.send(body, ephemeral=True)
