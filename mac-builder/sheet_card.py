"""A draft build sheet and its card: edit, remove, replace, discard, start.

Nothing here approves, pushes or merges a job: Approve and queue starts the
draft through /roadmap's own start path, so only the first job is queued and the
rest still wait for the owner's Approve and the merge.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord

import projects
import roadmap

log = logging.getLogger(__name__)
DRAFT_FILE = "sheet-draft.json"
FIELD_LIMIT = 4000  # a Discord modal text field
NO_DRAFT = "There is no draft sheet; it was queued, replaced or discarded."


def sheet_text(steps: list[roadmap.Step]) -> str:
    """The steps as a sheet /roadmap parses back, each job's project kept.

    The project line is written only when a job differs from the default, so a
    single-project sheet reads exactly as it did before projects existed.
    """
    blocks: list[str] = []
    for number, step in enumerate(steps, start=1):
        lines = [f"## Job {number}", f"kind: {step.kind}"]
        if step.project != projects.DEFAULT_PROJECT:
            lines.append(f"project: {step.project}")
        lines.append(step.text.strip())
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@dataclass
class Draft:
    steps: list[roadmap.Step] = field(default_factory=list)
    channel_id: int = 0
    requester_id: int = 0
    message_id: int = 0

    def edit(self, index: int, text: str) -> None:
        if not 0 <= index < len(self.steps):
            raise roadmap.RoadmapError(f"job {index + 1} is no longer in the draft")
        if not text.strip():
            raise roadmap.RoadmapError("the job text is empty; use Remove job to drop it")
        steps = list(self.steps)
        old = steps[index]
        # kind, project and job id belong to the step, not to its text.
        steps[index] = roadmap.Step(text.strip(), old.kind, old.job_id, old.project)
        self.steps = roadmap.parse(sheet_text(steps))


def save(path: Path, draft: Draft) -> None:
    path.write_text(json.dumps({"sheet": sheet_text(draft.steps), "channel_id": draft.channel_id,
                                "requester_id": draft.requester_id, "message_id": draft.message_id}), "utf-8")


def load(path: Path) -> Draft | None:
    try:
        data = json.loads(path.read_text("utf-8"))
        return Draft(roadmap.parse(str(data["sheet"])), int(data.get("channel_id") or 0),
                     int(data.get("requester_id") or 0), int(data.get("message_id") or 0)) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, KeyError, roadmap.RoadmapError):
        return None


async def post(bot, channel=None, *, note: str = "", active: bool = True, file=None,
               title: str = "Fix sheet draft") -> None:
    draft = bot._sheet_draft
    if draft is None:
        return
    body = f"**{title}** — {len(draft.steps)} job(s); nothing is queued."
    if note:
        body += "\n" + note
    if active:
        body += "\nPreview, edit, remove or replace it; Approve and queue starts it as the roadmap."
    body = body[:1900]
    channel = channel or bot._target_channel(draft.channel_id)
    if channel is None:
        return log.warning("no Discord channel for the draft sheet card")
    view = SheetCardView(bot) if active else discord.ui.View()
    if draft.message_id:
        try:
            await (await channel.fetch_message(draft.message_id)).edit(content=body, view=view)
            return
        except Exception:
            log.warning("could not edit the draft sheet card; posting a new one")
    try:
        message = await channel.send(body, file=file, view=view)
        draft.message_id = int(getattr(message, "id", 0) or 0)
        save(bot._sheet_draft_path(), draft)
    except Exception:
        log.exception("could not post the draft sheet card")


async def edit_job(bot, index: int, text: str) -> str:
    draft = bot._sheet_draft
    if draft is None:
        return NO_DRAFT
    try:
        draft.edit(index, text)
    except roadmap.RoadmapError as exc:
        return f"Not saved: {exc}. The draft is unchanged."
    save(bot._sheet_draft_path(), draft)
    await post(bot, note=f"Job {index + 1} saved; {len(draft.steps)} job(s).")
    return f"Job {index + 1} saved. The card is updated."

async def remove_job(bot, index: int) -> str:
    draft = bot._sheet_draft
    if draft is None:
        return NO_DRAFT
    if not 0 <= index < len(draft.steps):
        return f"Not removed: job {index + 1} is no longer in the draft."
    draft.steps.pop(index)
    save(bot._sheet_draft_path(), draft)
    if not draft.steps:
        await discard_draft(bot, "The last job was removed, so the draft was discarded.")
        return "Removed the last job; the draft was discarded. Nothing was queued."
    await post(bot, note=f"Removed job {index + 1}; {len(draft.steps)} job(s) left.")
    return f"Removed job {index + 1}."

async def discard_draft(bot, note: str = "Draft discarded. Nothing was queued.") -> None:
    if bot._sheet_draft is not None:
        await post(bot, note=note, active=False)
    bot._sheet_draft = None
    try:
        bot._sheet_draft_path().unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove the draft sheet file")


async def replace_draft(bot, steps: list[roadmap.Step], channel_id: int, requester_id: int) -> str:
    old = bot._sheet_draft
    bot._sheet_draft = Draft(list(steps), channel_id or (old.channel_id if old else 0),
                             requester_id, old.message_id if old else 0)
    save(bot._sheet_draft_path(), bot._sheet_draft)
    await post(bot, note=f"Sheet replaced: {len(steps)} job(s).")
    return f"Draft sheet replaced: {len(steps)} job(s); nothing is queued."


async def start_draft(bot, channel_id: int, requester_id: int) -> str:
    draft = bot._sheet_draft
    if draft is None or not draft.steps:
        return NO_DRAFT
    if getattr(bot, "_roadmap_steps", []):
        return "A roadmap is already active, so nothing was queued. The draft is unchanged; run /roadmap action:status."
    text = await bot._roadmap_start(draft.steps, draft.channel_id or channel_id, requester_id)
    if not getattr(bot, "_roadmap_steps", []):
        return text  # the queue was full; the draft is kept
    await post(bot, note="Approved and queued as the roadmap.", active=False)
    bot._sheet_draft = None
    try:
        bot._sheet_draft_path().unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove the queued draft sheet file")
    return text


class SheetCardView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _pick(self, i: discord.Interaction, verb: str) -> None:
        draft = self.bot._sheet_draft
        if draft is None or not draft.steps:
            return await i.response.send_message(NO_DRAFT, ephemeral=True)
        await i.response.send_message(f"Pick the job to {verb}:", view=JobPicker(self.bot, verb), ephemeral=True)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary)
    async def preview(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if not await self.bot.require_allowed(i):
            return
        draft = self.bot._sheet_draft
        await i.response.send_message(roadmap.preview(draft.steps) if draft and draft.steps else NO_DRAFT, ephemeral=True)

    @discord.ui.button(label="Edit job", style=discord.ButtonStyle.primary)
    async def edit(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            await self._pick(i, "edit")

    @discord.ui.button(label="Remove job", style=discord.ButtonStyle.secondary)
    async def remove(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            await self._pick(i, "remove")

    @discord.ui.button(label="Replace sheet", style=discord.ButtonStyle.secondary)
    async def replace(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if not await self.bot.require_allowed(i):
            return
        draft = self.bot._sheet_draft
        self.bot._roadmap_waiting[i.user.id] = ((draft.channel_id if draft else 0) or i.channel_id or 0,
                                                "draft", time.time() + getattr(self.bot, "ROADMAP_WAIT_SECONDS", 600.0))
        await i.response.send_message("Send the .md sheet as your next message in this channel within 10 minutes.", ephemeral=True)

    @discord.ui.button(label="Approve and queue", style=discord.ButtonStyle.success)
    async def approve(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if not await self.bot.require_allowed(i):
            return
        if getattr(self.bot, "_roadmap_steps", []):
            await i.response.send_message("A roadmap is already active, so nothing was queued. The draft is unchanged; run /roadmap action:status.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True, thinking=True)
        await i.followup.send(await start_draft(self.bot, i.channel_id or 0, i.user.id), ephemeral=True)

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.danger)
    async def discard(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            await discard_draft(self.bot)
            await i.response.send_message("Draft discarded. Nothing was queued.", ephemeral=True)


class JobPicker(discord.ui.View):
    """Pick one job to edit or remove, a page at a time.

    Discord caps one select at 25 options, and a sheet has no cap on its jobs, so
    every page but the last holds PAGE_SIZE of them and Previous/Next reach the
    rest: job 26 is on page 2, never out of reach.
    """

    PAGE_SIZE = 25

    def __init__(self, bot, verb: str, page: int = 0) -> None:
        super().__init__(timeout=120)
        self.bot, self.verb = bot, verb
        steps = bot._sheet_draft.steps if bot._sheet_draft else []
        self.pages = max(1, (len(steps) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(max(int(page), 0), self.pages - 1)
        start = self.page * self.PAGE_SIZE
        options = [
            discord.SelectOption(label=f"Job {n}: {' '.join(s.text.split())[:70]}", value=str(n - 1))
            for n, s in enumerate(steps[start:start + self.PAGE_SIZE], start + 1)]
        if options:
            select = discord.ui.Select(placeholder=self._placeholder(), options=options)
            select.callback = self._chosen
            self.add_item(select)
        if self.pages > 1:
            earlier = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary,
                                        disabled=self.page == 0)
            earlier.callback = self._earlier
            self.add_item(earlier)
            later = discord.ui.Button(label="Next", style=discord.ButtonStyle.primary,
                                      disabled=self.page >= self.pages - 1)
            later.callback = self._later
            self.add_item(later)

    def _placeholder(self) -> str:
        where = f" (page {self.page + 1}/{self.pages})" if self.pages > 1 else ""
        return f"Pick a job to {self.verb}{where}"

    async def _earlier(self, i: discord.Interaction) -> None:
        await self._turn(i, self.page - 1)

    async def _later(self, i: discord.Interaction) -> None:
        await self._turn(i, self.page + 1)

    async def _turn(self, i: discord.Interaction, page: int) -> None:
        if not await self.bot.require_allowed(i):
            return
        await i.response.edit_message(view=JobPicker(self.bot, self.verb, page))

    async def _chosen(self, i: discord.Interaction) -> None:
        if not await self.bot.require_allowed(i):
            return
        index = int(i.data["values"][0])
        if self.verb == "remove":
            await i.response.edit_message(content=await remove_job(self.bot, index), view=None)
            return
        draft = self.bot._sheet_draft
        if draft is None or not 0 <= index < len(draft.steps):
            await i.response.edit_message(content=NO_DRAFT, view=None)
        elif len(draft.steps[index].text) > FIELD_LIMIT:
            await i.response.edit_message(content=f"Job {index + 1} is {len(draft.steps[index].text)} characters; a modal holds {FIELD_LIMIT}. Replace the sheet or shorten it in the file.", view=None)
        else:
            await i.response.send_modal(JobModal(self.bot, index))


class JobModal(discord.ui.Modal):
    def __init__(self, bot, index: int) -> None:
        super().__init__(title=f"Edit job {index + 1}")
        self.bot, self.index = bot, index
        self.field = discord.ui.TextInput(label=f"Job {index + 1} text", style=discord.TextStyle.paragraph,
                                          default=bot._sheet_draft.steps[index].text[:FIELD_LIMIT],
                                          max_length=FIELD_LIMIT, required=True)
        self.add_item(self.field)

    async def on_submit(self, i: discord.Interaction) -> None:
        if await self.bot.require_allowed(i):
            await i.response.send_message(await edit_job(self.bot, self.index, str(self.field.value)), ephemeral=True)
