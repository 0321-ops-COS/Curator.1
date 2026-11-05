"""The session handoff, written from live state instead of from memory.

The README asks every session to start by reading ``logs/session-handoff.md``
and to write it before the context runs out — and nothing ever wrote it, so
continuity depended on someone remembering. This module assembles that file from
what the bot already knows: the project it builds, its own commit against
``main``, the queue, the roadmap, the draft sheet, the reviews still waiting and
the newest log.

Nothing here is probed over the network: no fetch, no GitHub call, no model. A
fact that cannot be read is written as ``unknown`` rather than guessed, and the
file says what it did not check.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord

import health

log = logging.getLogger("macbuilder")

HANDOFF_FILE = "session-handoff.md"
NORTH_STAR = (
    "Curator is the home base for the building team; Melisa is the owner's "
    "companion and the communication, planning and coordination layer between "
    "the owner and the builders. Read the README's \"Product purpose and "
    "long-term direction\" and its working principles before acting."
)
NOT_VERIFIED = (
    "Written from the bot's own state. Not checked for this handoff: a live job "
    "beyond the queue snapshot, the Mac's toolchain, GitHub, and whether CI is "
    "green."
)


@dataclass
class Handoff:
    """Everything one handoff states, already reduced to plain strings."""

    bot_name: str = "Melisa"
    stamp: str = ""
    project: str = ""
    project_name: str = ""
    repo: str = ""
    branch: str = ""
    head: str = "unknown"
    running_code: str = ""
    main_code: str = ""
    behind: int | None = None
    running_job: str = ""
    waiting: int = 0
    roadmap_left: int = 0
    roadmap_paused: bool = False
    roadmap_current: str = ""
    roadmap_next: str = ""
    draft_jobs: int = 0
    reviews: tuple[str, ...] = ()
    last_log: str = ""
    next_action: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def git_line(root: Path | str, *args: str) -> str:
    """One short read-only git answer, or "" when git cannot say."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _next_action(now: "Handoff", running_id: str) -> str:
    """The single safest highest-value next step, from the state alone."""
    if running_id:
        return (f"Wait for `{running_id}` to reach review, then Approve or reject "
                "on its card.")
    if now.reviews:
        return (f"Decide the waiting review(s): {', '.join(now.reviews)}. "
                "Approve pushes the branch; /merge gates the merge.")
    if now.roadmap_left and now.roadmap_paused:
        return (f"Resume the roadmap (/roadmap action:resume); {now.roadmap_left} "
                "job(s) are left and it is paused.")
    if now.roadmap_left:
        return (f"The roadmap queues its next job after the current one is "
                f"approved and merged; {now.roadmap_left} job(s) are left.")
    if now.draft_jobs:
        return (f"Approve and queue the draft sheet ({now.draft_jobs} job(s)), or "
                "edit it first.")
    return "Nothing is queued: the next step is the owner's to choose."


def collect(bot, *, now: float | None = None) -> Handoff:
    """Read the live state off the bot; never raise, never probe."""
    settings = getattr(bot, "settings", None)
    clock = now if now is not None else time.time()
    state = health.snapshot(bot, now=clock,
                            code_root=getattr(bot, "code_root", None))
    ctx = getattr(settings, "project", None)
    steps = getattr(bot, "_roadmap_steps", []) or []
    pending = [step for step in steps if not getattr(step, "job_id", "")]
    current = getattr(bot, "_roadmap_job", None)
    repo = Path(str(getattr(settings, "repo_dir", "") or ""))
    running = (state.running or {})
    reviews = tuple(
        str(record.get("job", {}).get("id", ""))
        for record in _saved_reviews(settings) if record.get("job", {}).get("id")
    )
    handoff = Handoff(
        bot_name=state.bot_name,
        stamp=time.strftime("%Y-%m-%d %H:%M", time.localtime(clock)),
        project=getattr(ctx, "project_id", "") or "",
        project_name=getattr(ctx, "project_name", "") or "",
        repo=str(getattr(ctx, "target_repo", "") or repo),
        branch=str(getattr(settings, "authorized_branch", "") or ""),
        head=git_line(repo, "rev-parse", "--short", "HEAD") if repo.is_dir() else "unknown",
        running_code=getattr(state.code, "running", "") if state.code else "",
        main_code=getattr(state.code, "main", "") if state.code else "",
        behind=getattr(state.code, "behind", None) if state.code else None,
        running_job=(
            f"`{running.get('id')}` ({running.get('kind')}) · "
            f"{health.format_uptime(running.get('elapsed'))}"
            if running else ""
        ),
        waiting=state.waiting,
        roadmap_left=len(pending),
        roadmap_paused=bool(getattr(bot, "_roadmap_paused", False)),
        roadmap_current=(
            f"`{current.id}` · {current.status}" if current is not None else ""
        ),
        roadmap_next=(" ".join(pending[0].text.split())[:80] if pending else ""),
        draft_jobs=state.draft_jobs,
        reviews=reviews,
        last_log=(f"`{state.last_job[0]}` · {health.format_uptime(state.last_job[1])} ago"
                  if state.last_job else "none yet"),
        notes=(),
    )
    handoff.next_action = _next_action(handoff, str(running.get("id") or ""))
    return handoff


def _saved_reviews(settings) -> list[dict]:
    """The reviews still awaiting a decision, without importing the whole bot."""
    try:
        import review_record

        return review_record.load(settings)
    except Exception:  # a broken record dir must not stop a handoff
        log.exception("handoff: could not read the saved reviews")
        return []


def text(state: Handoff) -> str:
    """The handoff as the markdown the README's protocol asks for."""
    lines = [
        f"# Session handoff — {state.stamp}",
        "",
        "## North star",
        NORTH_STAR,
        "",
        "## Current project",
        f"- project: `{state.project or 'unknown'}`"
        + (f" ({state.project_name})" if state.project_name else ""),
        f"- repository: {state.repo or 'unknown'}",
        f"- authorized branch: `{state.branch or 'unknown'}` · HEAD `{state.head}`",
    ]
    if state.running_code:
        state_line = (f"- my own code: running `{state.running_code}` · "
                      f"main `{state.main_code}`")
        if state.behind:
            state_line += f" ({state.behind} commit(s) behind: restart to pick it up)"
        else:
            state_line += " (up to date)"
        lines.append(state_line)
    lines += [
        "",
        "## Queue",
        f"- running: {state.running_job or 'nothing'}",
        f"- waiting: {state.waiting} job(s)",
        "",
        "## Roadmap and draft",
        f"- roadmap: {state.roadmap_left} job(s) left"
        + (", paused" if state.roadmap_paused else ""),
    ]
    if state.roadmap_current:
        lines.append(f"- current job: {state.roadmap_current}")
    if state.roadmap_next:
        lines.append(f"- next job: {state.roadmap_next}")
    if state.draft_jobs:
        lines.append(f"- draft sheet: {state.draft_jobs} job(s), not queued")
    lines += [
        "",
        "## Waiting on the owner",
        ("- reviews awaiting a decision: " + ", ".join(f"`{item}`" for item in state.reviews))
        if state.reviews else "- no review is waiting",
        "",
        "## Safety state",
        "- nothing was pushed, merged, deployed or approved by writing this file.",
        "",
        "## Evidence",
        f"- newest job log: {state.last_log}",
        "",
        "## Not verified",
        NOT_VERIFIED,
        "",
        "## Next action",
        state.next_action or "Nothing is queued: the next step is the owner's to choose.",
    ]
    return "\n".join(lines) + "\n"


def write(log_dir: Path | str, body: str) -> Path:
    """Save the handoff as LOG_DIR/session-handoff.md and return the path."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / HANDOFF_FILE
    path.write_text(body, encoding="utf-8")
    return path


def command(bot, interaction) -> None:
    """Write /handoff from live state; the only write is the handoff file."""
    return _command(bot, interaction)


async def _command(bot, interaction) -> None:
    if not await bot.require_allowed(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        state = collect(bot)
        path = write(bot.settings.log_dir, text(state))
    except Exception as exc:  # a handoff never crashes the bot
        log.exception("handoff failed")
        await interaction.followup.send(
            f"Could not write the handoff: {exc}", ephemeral=True)
        return
    await interaction.followup.send(
        content=(f"Handoff written to `{path.name}`. Next action: {state.next_action}\n"
                 "```\n" + state.head + "·" + (state.running_job or "idle") + "\n```"),
        file=discord.File(path, filename=path.name),
        ephemeral=True,
    )
