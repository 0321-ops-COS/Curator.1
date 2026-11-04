"""Turn a goal into a draft build sheet: the logic behind /plansheet.

A /plansheet is read-only like /plan: it creates no Job, starts no tmux session,
touches no worktree and queues nothing. Its outputs are one draft on the existing
draft-sheet card and the sheet saved as ``LOG_DIR/plansheet-<stamp>.md``.

Nothing here approves, pushes or merges, and every Discord call goes through the
bot it is handed, so this module runs without Discord, a model or a Mac.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import discord

import planner
import projects
import roadmap
import rules
import sheet_card
from manager import ManagerUnavailable, SheetLimits

log = logging.getLogger("macbuilder")

DRAFT_TITLE = "Plan sheet draft"
FILE_STEM = "plansheet"
REFUSED_SUFFIX = "-refused"
REPLACED_NOTE = "This replaced the earlier sheet draft."
# Punctuation a path may be quoted or wrapped in inside a job's prose.
TRIM = "`'\"()[]{}<>,;:!?*"
MAX_WARNINGS = 3
# MAX_DIFF_LINES when a settings object (or a test) does not carry one.
DEFAULT_MAX_DIFF_LINES = 400
SHEET_FILE_RE = re.compile(r"^plansheet-\d{8}-\d{6}(?:-refused)?(?:-\d+)?\.md$")


class SheetError(roadmap.RoadmapError):
    """The reply was not a sheet; the message says what the manager must fix."""


@dataclass(frozen=True)
class Attempt:
    """What one /plansheet got: the jobs, or why it refused, plus the raw reply."""

    steps: tuple[roadmap.Step, ...] = ()
    raw: str = ""
    refusal: str = ""
    truncated: bool = False
    retried: bool = False
    warnings: tuple[str, ...] = ()


def parse_sheet(text: str) -> list[roadmap.Step]:
    """The jobs in the manager's reply, or SheetError saying what to fix.

    A reply with anything before its first ``## Job`` heading has prose around
    the sheet and is refused: the card, /roadmap and the builder read only the
    jobs, so prose there would be dropped without anyone noticing.
    """
    body = (text or "").strip()
    if not body:
        raise SheetError("the reply was empty")
    first = next(line for line in body.splitlines() if line.strip()).strip()
    if not first.lower().startswith(roadmap.HEADING):
        raise SheetError(
            'the reply did not start with a "## Job" heading, so it had prose '
            "around the sheet"
        )
    return roadmap.parse(body)


def named_protected_paths(step: roadmap.Step) -> list[str]:
    """The paths one job names that a change to them would be rejected for."""
    found: list[str] = []
    dan = step.kind == "dan"
    for raw in step.text.split():
        token = raw.strip(TRIM)
        if not token or ("/" not in token and "." not in token):
            continue
        if rules.is_protected_path(token, dan=dan) and token not in found:
            found.append(token)
    return found


def protected_warnings(steps: Sequence[roadmap.Step]) -> tuple[str, ...]:
    """One plain warning per job naming a protected path; never a refusal."""
    lines: list[str] = []
    for number, step in enumerate(steps, start=1):
        for path in named_protected_paths(step):
            lines.append(
                f"Heads-up: job {number} names {path}, a protected path, so a job "
                "that changes it is rejected. The sheet is still drafted."
            )
            if len(lines) >= MAX_WARNINGS:
                return tuple(lines)
    return tuple(lines)


def sheet_document(goal: str, steps: Sequence[roadmap.Step], *, bot_name: str,
                   created: str) -> str:
    """The attached file: the goal, one line of provenance, then the whole sheet."""
    header = (
        f"# Build sheet — {goal.strip()}\n\n"
        f"_Drafted by {bot_name} on {created}. Every job below is one `## Job`; "
        "nothing was queued, approved, pushed or merged._\n"
    )
    return header + "\n" + sheet_card.sheet_text(list(steps)) + "\n"


def write_sheet(log_dir: Path, text: str, *, stamp: str, refused: bool = False) -> Path:
    """Save the sheet as LOG_DIR/plansheet-<stamp>.md and return the path."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = REFUSED_SUFFIX if refused else ""
    path = directory / f"{FILE_STEM}-{stamp}{suffix}.md"
    counter = 2
    while path.exists():
        path = directory / f"{FILE_STEM}-{stamp}{suffix}-{counter}.md"
        counter += 1
    path.write_text(text, encoding="utf-8")
    return path


async def draft(manager, inputs: planner.PlanInputs, limits: SheetLimits) -> Attempt:
    """Ask for a sheet, retry a bad one once, and never leave a half draft."""
    first = await manager.plan_sheet(inputs, limits)
    if first.truncated:
        return Attempt(
            refusal=(
                "The manager hit its token limit, so the sheet was incomplete and is "
                "refused as a whole; try a narrower goal."
            ),
            truncated=True,
        )
    try:
        steps = parse_sheet(first.text)
    except roadmap.RoadmapError as exc:
        second = await manager.plan_sheet(inputs, limits, correction=str(exc))
        if second.truncated:
            return Attempt(
                raw=second.text,
                retried=True,
                truncated=True,
                refusal=(
                    "The retried sheet was cut off at the manager's token limit too; "
                    "its reply is attached."
                ),
            )
        try:
            steps = parse_sheet(second.text)
        except roadmap.RoadmapError as again:
            return Attempt(
                raw=second.text,
                retried=True,
                refusal=(
                    f"The manager's sheet was still not usable after one retry ({again}); "
                    "its reply is attached."
                ),
            )
        return Attempt(steps=tuple(steps), raw=second.text, retried=True,
                       warnings=protected_warnings(steps))
    return Attempt(steps=tuple(steps), raw=first.text,
                   warnings=protected_warnings(steps))


def project_ids(settings) -> tuple[str, ...]:
    """The project ids a sheet may name, the default one always included."""
    registry = getattr(settings, "projects", None)
    if isinstance(registry, projects.Registry):
        return registry.ids()
    return (projects.DEFAULT_PROJECT,)


def project_material(settings, project_id: str = "") -> tuple[str, Path]:
    """The prompt block and the checkout whose file map the sheet may name."""
    registry = getattr(settings, "projects", None)
    entry = registry.get(project_id) if isinstance(registry, projects.Registry) else None
    if entry is not None and not entry.is_default:
        # Read-only metadata: another project's checkout is never probed here.
        block = "\n".join([
            f"project: {entry.id}",
            f"name: {entry.name}",
            f"checkout: {entry.path}",
            f"base branch: {entry.authorized_branch}",
            f"kind: {entry.kind}",
        ])
        return block, Path(entry.path)
    ctx = getattr(settings, "project", None)
    block = ctx.prompt_block() if ctx is not None else ""
    return block, Path(getattr(settings, "repo_dir", Path.cwd()))


async def command(bot, interaction, goal: str, project: str = "", *,
                  prompt_goal: str = "", source: str = "/plansheet") -> None:
    """Draft a build sheet for a goal and post it on the draft-sheet card.

    ``prompt_goal`` is what the manager reads when it is not the goal itself
    (/feedback frames an owner's note); the sheet, the attachment and the card
    keep the owner's own words. ``source`` only names the command that asked.
    """
    if not await bot.require_allowed(interaction):
        return
    await interaction.response.defer(thinking=True)
    settings = bot.settings
    if not getattr(bot.manager, "enabled", False):
        await interaction.followup.send(
            "The AI manager is not configured, so there is no sheet to draft. Set "
            "MANAGER_API_BASE, MANAGER_API_KEY and MANAGER_MODEL in `.env`."
        )
        return
    chosen = (project or "").strip().lower()
    known = project_ids(settings)
    if chosen and chosen not in known:
        await interaction.followup.send(
            f"Not drafted: unknown project {chosen!r}. Known: {', '.join(known)}."
        )
        return
    block, target = project_material(settings, chosen)
    inputs = planner.gather(
        goal=(prompt_goal or goal),
        project_block=block,
        log_dir=settings.log_dir,
        target_dir=target,
        readme=Path(__file__).resolve().parent / "README.md",
    )
    limits = SheetLimits(
        max_diff_lines=int(getattr(settings, "max_diff_lines", 0) or DEFAULT_MAX_DIFF_LINES),
        max_task_chars=roadmap.MAX_TASK_CHARS,
    )
    try:
        attempt = await draft(bot.manager, inputs, limits)
    except ManagerUnavailable as exc:
        await interaction.followup.send(f"Could not draft a sheet: {exc}")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if attempt.refusal:
        message = attempt.refusal + " Nothing was drafted; any earlier draft is unchanged."
        if attempt.raw:
            path = write_sheet(settings.log_dir, attempt.raw, stamp=stamp, refused=True)
            message += f" The manager's reply is attached as `{path.name}`."
            await interaction.followup.send(
                message, file=discord.File(path, filename=path.name))
        else:
            await interaction.followup.send(message)
        return
    steps = list(attempt.steps)
    unknown = sorted({step.project for step in steps if step.project not in known})
    if unknown:
        number = next(n for n, step in enumerate(steps, 1) if step.project in unknown)
        await interaction.followup.send(
            f"Not drafted: job {number} names an unknown project {unknown[0]!r}. "
            f"Known: {', '.join(known)}. Nothing was drafted; any earlier draft is "
            "unchanged."
        )
        return
    created = time.strftime("%Y-%m-%d %H:%M")
    document = sheet_document(
        goal, steps,
        bot_name=str(getattr(settings, "bot_name", "Melisa") or "Melisa"),
        created=created,
    )
    try:
        path = write_sheet(settings.log_dir, document, stamp=stamp)
    except OSError as exc:
        await interaction.followup.send(
            f"Could not save the sheet: {exc}; nothing was drafted.")
        return
    previous = bot._sheet_draft
    bot._sheet_draft = sheet_card.Draft(
        steps,
        (interaction.channel_id or 0) or (previous.channel_id if previous else 0),
        interaction.user.id,
        previous.message_id if previous else 0,
    )
    sheet_card.save(bot._sheet_draft_path(), bot._sheet_draft)
    note = f"From {source}: {goal[:80]}."
    if previous is not None:
        note += " " + REPLACED_NOTE
    if attempt.warnings:
        note += "\n" + "\n".join(attempt.warnings)
    await sheet_card.post(bot, note=note, title=DRAFT_TITLE)
    await interaction.followup.send(
        f"Planned `{goal[:80]}` as {len(steps)} job(s) on the draft card; nothing "
        f"is queued, approved, pushed or merged. The whole sheet is attached and saved "
        f"as `{path.name}`.",
        file=discord.File(path, filename=path.name),
    )
