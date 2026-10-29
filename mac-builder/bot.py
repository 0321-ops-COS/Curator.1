#!/usr/bin/env python3
"""Discord front end for the private Mac build bot.

All Discord input is treated as untrusted. Commands only select fixed scripts or
plain task text for Freebuff; buttons check the allowlist again on every press.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

# The required filename queue.py shadows the standard library module. Preload
# the real stdlib queue before putting this folder back on sys.path, otherwise
# libraries that import `queue` lazily would receive our job queue module.
for _entry in list(sys.path):
    try:
        if _entry and Path(_entry).resolve() == HERE:
            sys.path.remove(_entry)
    except OSError:
        continue
import queue as _stdlib_queue  # noqa: E402,F401

sys.modules.setdefault("queue", _stdlib_queue)
sys.path.insert(0, str(HERE))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import jobs  # noqa: E402
import rules  # noqa: E402
from freebuff_runner import FreebuffError, FreebuffRunner  # noqa: E402
from jobs import Job, ReviewOutcome, tail_text  # noqa: E402
from manager import Manager  # noqa: E402

log = logging.getLogger("macbuilder")

DEFAULT_AUTHORIZED_BRANCH = "freebuff/ce4b8907c250c30103d8993e"
LOG_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_optional_id(name: str) -> int | None:
    raw = _env(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a Discord ID (integer), got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive Discord ID")
    return value


def _env_ids(name: str) -> frozenset[int]:
    raw = _env(name)
    if not raw:
        return frozenset()
    values: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-numeric ID: {part!r}") from exc
        if value <= 0:
            raise ConfigError(f"{name} contains an invalid ID: {part!r}")
        values.add(value)
    return frozenset(values)


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_ids: frozenset[int]
    authorized_branch: str
    repo_dir: Path
    log_dir: Path
    worktree_root: Path
    notify_channel_id: int | None
    ai_channel_id: int | None
    ai_channel_name: str
    guild_id: int | None
    manager_api_base: str
    manager_api_key: str
    manager_model: str
    manager_timeout_seconds: float
    max_diff_lines: int
    max_revise_rounds: int
    max_pending_jobs: int
    freebuff_bin: str
    freebuff_args: tuple[str, ...]
    freebuff_ready_seconds: float
    freebuff_idle_seconds: float
    freebuff_poll_seconds: float
    freebuff_timeout_seconds: float
    check_timeout_seconds: float
    test_timeout_seconds: float
    build_timeout_seconds: float
    status_timeout_seconds: float
    hustlercore_dir: str
    xcode_project: str
    xcode_scheme: str
    simulator_name: str
    swift_jobs: int
    xcodebuild_jobs: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = _env("DISCORD_TOKEN")
        if not token:
            raise ConfigError("DISCORD_TOKEN is not set; copy .env.example to .env")
        allowed = _env_ids("ALLOWED_USER_IDS")
        if not allowed:
            raise ConfigError(
                "ALLOWED_USER_IDS is empty; refusing to start a bot nobody can use"
            )

        branch = _env("AUTHORIZED_BRANCH", DEFAULT_AUTHORIZED_BRANCH)
        branch = rules.assert_not_main(branch)

        repo_dir = Path(_env("REPO_DIR", str(REPO_ROOT))).expanduser().resolve()
        if not (repo_dir / ".git").exists():
            raise ConfigError(f"REPO_DIR is not a git checkout: {repo_dir}")

        log_dir = Path(_env("LOG_DIR", str(HERE / "logs"))).expanduser().resolve()
        worktree_root = Path(
            _env("WORKTREE_ROOT", str(Path.home() / "mac-builder-worktrees"))
        ).expanduser().resolve()

        max_diff_lines = _env_int("MAX_DIFF_LINES", 400)
        if max_diff_lines < 1:
            raise ConfigError("MAX_DIFF_LINES must be at least 1")
        max_revise = _env_int("MAX_REVISE_ROUNDS", 2)
        if max_revise < 0 or max_revise > 5:
            raise ConfigError("MAX_REVISE_ROUNDS must be between 0 and 5")

        freebuff_args_raw = _env("FREEBUFF_ARGS")
        freebuff_args = tuple(shlex.split(freebuff_args_raw)) if freebuff_args_raw else ()

        settings = cls(
            token=token,
            allowed_user_ids=allowed,
            authorized_branch=branch,
            repo_dir=repo_dir,
            log_dir=log_dir,
            worktree_root=worktree_root,
            notify_channel_id=_env_optional_id("NOTIFY_CHANNEL_ID"),
            ai_channel_id=_env_optional_id("AI_CHANNEL_ID"),
            ai_channel_name=_env("AI_CHANNEL_NAME", "ai").lstrip("#") or "ai",
            guild_id=_env_optional_id("GUILD_ID"),
            manager_api_base=_env("MANAGER_API_BASE"),
            manager_api_key=_env("MANAGER_API_KEY"),
            manager_model=_env("MANAGER_MODEL"),
            manager_timeout_seconds=_env_float("MANAGER_TIMEOUT_SECONDS", 120.0),
            max_diff_lines=max_diff_lines,
            max_revise_rounds=max_revise,
            max_pending_jobs=_env_int("MAX_PENDING_JOBS", 10),
            freebuff_bin=_env("FREEBUFF_BIN", "freebuff"),
            freebuff_args=freebuff_args,
            freebuff_ready_seconds=_env_float("FREEBUFF_READY_SECONDS", 60.0),
            freebuff_idle_seconds=_env_float("FREEBUFF_IDLE_SECONDS", 120.0),
            freebuff_poll_seconds=_env_float("FREEBUFF_POLL_SECONDS", 10.0),
            freebuff_timeout_seconds=_env_float("FREEBUFF_TIMEOUT_SECONDS", 1800.0),
            check_timeout_seconds=_env_float("CHECK_TIMEOUT_SECONDS", 1800.0),
            test_timeout_seconds=_env_float("TEST_TIMEOUT_SECONDS", 2700.0),
            build_timeout_seconds=_env_float("BUILD_TIMEOUT_SECONDS", 1800.0),
            status_timeout_seconds=_env_float("STATUS_TIMEOUT_SECONDS", 60.0),
            hustlercore_dir=_env("HUSTLERCORE_DIR", "ios-app/HustlerCore"),
            xcode_project=_env("XCODE_PROJECT", "ios-app/Hustler.xcodeproj"),
            xcode_scheme=_env("XCODE_SCHEME", "Hustler"),
            simulator_name=_env("SIMULATOR_NAME", "iPhone 16"),
            swift_jobs=_env_int("SWIFT_JOBS", 2),
            xcodebuild_jobs=_env_int("XCODEBUILD_JOBS", 2),
        )
        if settings.max_pending_jobs < 1:
            raise ConfigError("MAX_PENDING_JOBS must be at least 1")
        return settings


def split_message(text: str, limit: int = 1900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class TaskProposalView(discord.ui.View):
    """A manager proposal that only runs after an allowlisted confirm press."""

    def __init__(self, bot: "MacBuilderBot", task: str) -> None:
        super().__init__(timeout=3600)
        self.bot = bot
        self.task = task
        self._used = False

    @discord.ui.button(label="Queue this task", style=discord.ButtonStyle.primary)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.bot.require_allowed(interaction):
            return
        if self._used:
            await interaction.response.send_message("This proposal was already handled.", ephemeral=True)
            return
        self._used = True
        button.disabled = True
        job = jobs.make_job(
            self.bot.settings,
            kind="task",
            title=self.task[:100],
            channel_id=interaction.channel_id or 0,
            requester_id=interaction.user.id,
            text=self.task,
        )
        try:
            position = await self.bot.queue.submit(job)
        except jobs.QueueFull as exc:
            self._used = False
            button.disabled = False
            await interaction.response.send_message(str(exc), ephemeral=True)
            await interaction.message.edit(view=self)
            return
        await interaction.response.edit_message(
            content=f"Queued task `{job.id}` (position {position}).",
            view=self,
        )
        await interaction.followup.send(
            f"Freebuff task queued from a manager proposal: `{job.id}`.", ephemeral=True
        )


class ReviewView(discord.ui.View):
    """Approve / Reject. Nothing is pushed until Approve is pressed."""

    def __init__(self, bot: "MacBuilderBot", outcome: ReviewOutcome) -> None:
        super().__init__(timeout=86400)
        self.bot = bot
        self.outcome = outcome
        self.resolved = False
        self._lock = asyncio.Lock()

    def _disable(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _finish(self, interaction: discord.Interaction, text: str) -> None:
        self._disable()
        self.resolved = True
        embed = None
        if interaction.message is not None and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            embed.add_field(name="Decision", value=text, inline=False)
        if interaction.message is not None:
            await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send(text)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.bot.require_allowed(interaction):
            return
        async with self._lock:
            if self.resolved:
                await interaction.response.send_message("Already handled.", ephemeral=True)
                return
            self.resolved = True
        await interaction.response.defer()
        env = self.outcome.environment
        if env is None:
            await self._finish(interaction, "Nothing to push; the worktree was not kept.")
            return
        message = f"Freebuff task {self.outcome.job.id}: {self.outcome.job.title}"[:200]
        try:
            sha = await self.bot.freebuff.approve_and_push(env, message=message)
        except (FreebuffError, ValueError) as exc:
            log.warning("approve failed for %s: %s", env.branch, exc)
            async with self._lock:
                self.resolved = False
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = False
            await interaction.followup.send(f"Push failed, nothing was pushed: {exc}")
            if interaction.message is not None:
                await interaction.message.edit(view=self)
            return
        await self._finish(
            interaction,
            f"✅ Approved by <@{interaction.user.id}>. Pushed `{env.branch}` at `{sha[:7]}`. "
            "Merging is yours; this bot never merges.",
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self.bot.require_allowed(interaction):
            return
        async with self._lock:
            if self.resolved:
                await interaction.response.send_message("Already handled.", ephemeral=True)
                return
            self.resolved = True
        await interaction.response.defer()
        branch = self.outcome.environment.branch if self.outcome.environment else "none"
        await self._finish(
            interaction,
            f"❌ Rejected by <@{interaction.user.id}>. Nothing was pushed. "
            f"Branch `{branch}` and its worktree were kept for inspection.",
        )


class MacBuilderBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.manager = Manager(
            api_base=settings.manager_api_base,
            api_key=settings.manager_api_key,
            model=settings.manager_model,
            timeout_seconds=settings.manager_timeout_seconds,
        )
        self.freebuff = FreebuffRunner(settings)
        self.runner = jobs.JobRunner(
            settings,
            self.manager,
            self.freebuff,
            post_job=self._post_job_result,
            post_review=self._post_review,
        )
        self.queue = jobs.JobQueue(
            self.runner.run,
            max_pending=settings.max_pending_jobs,
            on_crash=self._on_job_crash,
        )
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=12)
        )
        self._queue_started = False

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="Show branch, git state, queue length and the running job",
                callback=self.cmd_status,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="check",
                description="Run swift test in HustlerCore (fast, queued)",
                callback=self.cmd_check,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="test",
                description="Run xcodegen + xcodebuild test on one simulator (queued)",
                callback=self.cmd_test,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="build",
                description="Run xcodegen + xcodebuild build (queued)",
                callback=self.cmd_build,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="logs",
                description="Show the tail of the latest or a given job log",
                callback=self.cmd_logs,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="task",
                description="Queue a Freebuff task on its own freebuff/* branch",
                callback=self.cmd_task,
            )
        )
        self.tree.interaction_check = self.require_allowed
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("synced %d application commands", len(synced))
        if not self._queue_started:
            self.queue.start()
            self._queue_started = True

    async def on_ready(self) -> None:
        log.info(
            "logged in as %s (id=%s); authorized branch %s; manager %s",
            self.user,
            getattr(self.user, "id", "?"),
            self.settings.authorized_branch,
            "enabled" if self.manager.enabled else "DISABLED",
        )
        if not self.manager.enabled:
            log.warning(
                "manager is not configured; reviews will ask for a human decision"
            )

    async def close(self) -> None:
        try:
            await self.runner.shutdown()
            await self.queue.stop()
        finally:
            await self.manager.close()
            await super().close()

    # -------------------------------------------------------------- security

    async def require_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.settings.allowed_user_ids:
            return True
        log.warning(
            "denied interaction from user %s (%s)",
            interaction.user.id,
            interaction.user,
        )
        message = "You are not on this bot's allowlist."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    # ----------------------------------------------------------------- status

    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        code, output = await jobs.probe_status(self.settings)
        snapshot = self.queue.snapshot()
        running = snapshot.get("running") or {}
        lines = [
            f"queue: {snapshot['pending']} waiting",
            f"running: {running.get('id', 'none')}"
            + (f" ({running.get('kind')})" if running else ""),
            f"authorized branch: {self.settings.authorized_branch}",
            f"manager: {'configured' if self.manager.enabled else 'not configured'}",
            f"status.sh exit: {code}",
            "",
            tail_text(output, max_lines=40, max_chars=1400),
        ]
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

    # ----------------------------------------------------------- fixed jobs

    async def _queue_script_job(self, interaction: discord.Interaction, kind: str) -> None:
        if not await self.require_allowed(interaction):
            return
        titles = {
            "check": "swift test (HustlerCore)",
            "test": "xcodegen + xcodebuild test",
            "build": "xcodegen + xcodebuild build",
        }
        job = jobs.make_job(
            self.settings,
            kind=kind,
            title=titles[kind],
            channel_id=interaction.channel_id or 0,
            requester_id=interaction.user.id,
        )
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Queued `/{kind}` as `{job.id}` (position {position}). "
            f"Log: `{job.log_path.name}`.",
            ephemeral=True,
        )

    async def cmd_check(self, interaction: discord.Interaction) -> None:
        await self._queue_script_job(interaction, "check")

    async def cmd_test(self, interaction: discord.Interaction) -> None:
        await self._queue_script_job(interaction, "test")

    async def cmd_build(self, interaction: discord.Interaction) -> None:
        await self._queue_script_job(interaction, "build")

    @app_commands.describe(job_id="Job id to read; leave empty for the newest log")
    async def cmd_logs(
        self, interaction: discord.Interaction, job_id: Optional[str] = None
    ) -> None:
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        path = self._resolve_log(job_id)
        if path is None:
            await interaction.followup.send(
                "No log found. Pass a job id from a queued command.", ephemeral=True
            )
            return
        try:
            data = path.read_bytes()[-20000:]
        except OSError as exc:
            await interaction.followup.send(f"Could not read the log: {exc}", ephemeral=True)
            return
        text = tail_text(data.decode("utf-8", "replace"), max_lines=60, max_chars=1700)
        await interaction.followup.send(
            f"`{path.name}` (last {min(len(data), 20000)} bytes)\n```\n{text}\n```",
            ephemeral=True,
        )

    def _resolve_log(self, job_id: str | None) -> Path | None:
        log_dir = self.settings.log_dir
        if job_id:
            value = job_id.strip()
            if value.endswith(".log"):
                value = value[:-4]
            if not LOG_ID_RE.match(value):
                return None
            path = (log_dir / f"{value}.log").resolve()
            if path.parent != log_dir.resolve():
                return None
            return path if path.is_file() else None
        candidates = []
        for candidate in log_dir.glob("*.log"):
            if not LOG_ID_RE.match(candidate.stem):
                continue
            try:
                candidates.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        if not candidates:
            return None
        return max(candidates)[1]

    # ---------------------------------------------------------------- tasks

    @app_commands.describe(text="What Freebuff should do on a fresh freebuff/* branch")
    async def cmd_task(self, interaction: discord.Interaction, text: str) -> None:
        if not await self.require_allowed(interaction):
            return
        try:
            task_text = rules.sanitize_task_text(text)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        job = jobs.make_job(
            self.settings,
            kind="task",
            title=task_text[:100],
            channel_id=interaction.channel_id or 0,
            requester_id=interaction.user.id,
            text=task_text,
        )
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        warning = "" if self.manager.enabled else " (AI manager is not configured.)"
        await interaction.response.send_message(
            f"Queued Freebuff task `{job.id}` (position {position}).{warning}",
            ephemeral=True,
        )

    # ----------------------------------------------------------- ai channel

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self._is_ai_channel(message.channel):
            return
        if message.author.id not in self.settings.allowed_user_ids:
            return
        content = message.content.strip()
        if not content:
            return
        if not self.manager.enabled:
            await message.reply(
                "The AI manager is not configured. Set MANAGER_API_BASE, "
                "MANAGER_API_KEY and MANAGER_MODEL in `.env`."
            )
            return
        history = self._history[message.channel.id]
        history.append({"role": "user", "content": content[:4000]})
        try:
            async with message.channel.typing():
                reply = await self.manager.chat(list(history), self._ai_context())
        except Exception as exc:  # pragma: no cover - endpoint failures
            log.warning("manager chat failed: %s", exc)
            await message.reply(f"The manager request failed: {exc}")
            return
        history.append({"role": "assistant", "content": reply.text[:4000]})
        for chunk in split_message(reply.text):
            await message.reply(chunk)
        for task in reply.proposed_tasks:
            embed = discord.Embed(
                title="Proposed task",
                description=task,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Nothing runs until you press Queue this task.")
            await message.reply(embed=embed, view=TaskProposalView(self, task))

    def _is_ai_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if self.settings.ai_channel_id is not None:
            return channel.id == self.settings.ai_channel_id
        name = getattr(channel, "name", "") or ""
        return name.lower() == self.settings.ai_channel_name.lower()

    def _ai_context(self) -> str:
        snapshot = self.queue.snapshot()
        running = snapshot.get("running") or {}
        parts = [
            f"repository: {self.settings.repo_dir}",
            f"authorized branch: {self.settings.authorized_branch}",
            f"queue waiting: {snapshot['pending']}",
            "running job: "
            + (
                f"{running.get('id')} ({running.get('kind')})"
                if running
                else "none"
            ),
            f"manager model: {self.settings.manager_model or 'not configured'}",
            "hard rules: diffs over "
            f"{self.settings.max_diff_lines} lines and protected-path changes are rejected "
            "without asking the model; failing checks force revise; the bot never merges.",
            self.runner.last_job_context(),
        ]
        return "\n".join(part for part in parts if part)

    # ------------------------------------------------------------ reporting

    def _target_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None and self.settings.notify_channel_id:
            channel = self.get_channel(self.settings.notify_channel_id)
        return channel  # type: ignore[return-value]

    async def _post_job_result(self, job: Job, ok: bool, summary: str) -> None:
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for job %s", job.id)
            return
        embed = discord.Embed(
            title=("✅ " if ok else "❌ ") + f"{job.kind} finished",
            description=tail_text(summary, max_lines=14, max_chars=1800) or "(no output)",
            color=discord.Color.green() if ok else discord.Color.red(),
        )
        embed.add_field(name="Job", value=f"`{job.id}`", inline=True)
        embed.add_field(name="Log", value=f"`{job.log_path.name}`", inline=True)
        if job.duration is not None:
            embed.add_field(name="Took", value=f"{job.duration:.0f}s", inline=True)
        await channel.send(embed=embed)

    async def _on_job_crash(self, job: Job, exc: BaseException) -> None:
        channel = self._target_channel(job.channel_id)
        if channel is None:
            return
        await channel.send(
            f"❌ Job `{job.id}` crashed unexpectedly: `{type(exc).__name__}: {exc}`. "
            f"See `{job.log_path.name}`."
        )

    async def _post_review(self, outcome: ReviewOutcome) -> None:
        channel = self._target_channel(outcome.job.channel_id)
        if channel is None:
            log.warning("no Discord channel for review of %s", outcome.job.id)
            return
        if outcome.verdict == "blocked":
            embed = discord.Embed(
                title="⛔ Blocked by rules — manager not called",
                description=outcome.notes[:3500] or "The change violated a hard rule.",
                color=discord.Color.dark_red(),
            )
            embed.add_field(name="Branch", value=f"`{outcome.environment.branch if outcome.environment else '?'}`", inline=True)
            embed.add_field(name="Changed lines", value=str(outcome.diff_lines), inline=True)
            embed.add_field(name="Log", value=f"`{outcome.job.log_path.name}`", inline=True)
            embed.set_footer(text="Nothing can be pushed from a blocked change.")
            await channel.send(embed=embed)
            return

        titles = {
            "approve": "✅ Manager approved — waiting for your decision",
            "ask_user": "🟡 Manager needs your decision",
        }
        colors = {
            "approve": discord.Color.green(),
            "ask_user": discord.Color.orange(),
        }
        embed = discord.Embed(
            title=titles.get(outcome.verdict, "Review"),
            description=f"**Task:** {outcome.job.text[:300]}\n\n**Manager notes:**\n{outcome.notes[:3000] or '(none)'}",
            color=colors.get(outcome.verdict, discord.Color.orange()),
        )
        embed.add_field(
            name="Branch",
            value=f"`{outcome.environment.branch if outcome.environment else '?'}`",
            inline=True,
        )
        embed.add_field(name="Changed lines", value=str(outcome.diff_lines), inline=True)
        embed.add_field(name="Check", value=outcome.check_state[:100], inline=True)
        embed.add_field(name="Test", value=outcome.test_state[:100], inline=True)
        embed.add_field(name="Files", value=str(len(outcome.changed_files)), inline=True)
        embed.add_field(name="Job", value=f"`{outcome.job.id}`", inline=True)
        embed.add_field(name="Log", value=f"`{outcome.job.log_path.name}`", inline=False)
        embed.set_footer(text="Nothing is pushed unless you press Approve.")
        await channel.send(embed=embed, view=ReviewView(self, outcome))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(HERE / ".env")
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = Settings.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.worktree_root.mkdir(parents=True, exist_ok=True)
    bot = MacBuilderBot(settings)
    try:
        bot.run(settings.token, log_handler=None)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
