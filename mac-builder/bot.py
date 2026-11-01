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
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, replace
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
import planner  # noqa: E402
import project_context  # noqa: E402
import questions  # noqa: E402
import rules  # noqa: E402
from freebuff_runner import FreebuffError, FreebuffRunner  # noqa: E402
from jobs import Job, ReviewOutcome, tail_text  # noqa: E402
from manager import Manager, ManagerUnavailable  # noqa: E402

log = logging.getLogger("macbuilder")

DEFAULT_AUTHORIZED_BRANCH = "freebuff/ce4b8907c250c30103d8993e"
LOG_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class ConfigError(RuntimeError):
    pass


def _git_line(root: Path, *args: str) -> str:
    """One read-only `git -C root` line, or 'unknown'. argv only, never a shell."""
    try:
        finished = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if finished.returncode != 0:
        return "unknown"
    return finished.stdout.strip() or "unknown"


def code_location(root: Path) -> str:
    """'branch@short-commit' of the code that is about to run, for the startup log.

    This answers one question at a glance: after a checkout moved (mac-builder
    out of Hustler into Curator.1) the bot could otherwise run old code from a
    folder nobody was editing, and nothing said which folder that was.
    """
    branch = _git_line(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "unknown":
        return "not a git checkout"
    commit = _git_line(root, "rev-parse", "--short", "HEAD")
    return f"{branch}@{commit}"


def _http_error_text(exc: "discord.HTTPException") -> str:
    """What went wrong with a Discord HTTP call, in one line and without the token."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if status:
        parts.append(f"HTTP {status}")
    if code:
        parts.append(f"code {code}")
    head = " ".join(parts)
    text = (getattr(exc, "text", "") or "").strip()
    return f"{head}: {text[:200]}" if text else head


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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be 0 or 1, got {raw!r}")


def _env_models(name: str) -> tuple[str, ...]:
    """A comma-separated list of model ids, trimmed, empty entries dropped."""
    return tuple(part.strip() for part in _env(name).split(",") if part.strip())


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
    manager_fallback_models: tuple[str, ...]
    manager_timeout_seconds: float
    manager_max_tokens: int
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
    project_name: str = ""
    builder_name: str = "Freebuff"
    bot_name: str = "Melisa"
    # How long a Freebuff question box may hold a job before it is dismissed
    # with Esc and the job fails (QUESTION_WAIT_SECONDS).
    question_wait_seconds: float = 1800.0
    # The manager may answer a question, but only with high confidence.
    question_auto_answer: bool = False
    # At most this many auto-answers per job; then every question goes to you.
    question_auto_answer_max: int = 3
    # After a 429 or a timeout, skip manager calls for this long.
    question_manager_cooldown_seconds: float = 30.0
    # Post the manager's advice as a follow-up on the question. Never an answer.
    question_suggest: bool = False
    # Write the pane to the job log after every key sent to a question box.
    question_debug: bool = False
    # Filled in by main() after the startup preflight; None before that.
    project: "project_context.ProjectContext | None" = None

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
            manager_fallback_models=_env_models("MANAGER_FALLBACK_MODELS"),
            manager_timeout_seconds=_env_float("MANAGER_TIMEOUT_SECONDS", 120.0),
            manager_max_tokens=_env_int("MANAGER_MAX_TOKENS", 4000),
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
            project_name=_env("PROJECT_NAME"),
            builder_name=_env("BUILDER_NAME", "Freebuff"),
            bot_name=_env("BOT_NAME", "Melisa"),
            question_wait_seconds=_env_float("QUESTION_WAIT_SECONDS", 1800.0),
            question_auto_answer=_env_bool("QUESTION_AUTO_ANSWER", False),
            question_auto_answer_max=_env_int("QUESTION_AUTO_ANSWER_MAX", 3),
            question_manager_cooldown_seconds=_env_float(
                "QUESTION_MANAGER_COOLDOWN_SECONDS", 30.0
            ),
            question_suggest=_env_bool("QUESTION_SUGGEST", False),
            question_debug=_env_bool("QUESTION_DEBUG", False),
        )
        if settings.max_pending_jobs < 1:
            raise ConfigError("MAX_PENDING_JOBS must be at least 1")
        if settings.manager_max_tokens < 1:
            raise ConfigError("MANAGER_MAX_TOKENS must be at least 1")
        if settings.question_wait_seconds < 1:
            raise ConfigError("QUESTION_WAIT_SECONDS must be at least 1")
        if settings.question_auto_answer_max < 0:
            raise ConfigError("QUESTION_AUTO_ANSWER_MAX must be 0 or more")
        if settings.question_manager_cooldown_seconds < 0:
            raise ConfigError("QUESTION_MANAGER_COOLDOWN_SECONDS must be 0 or more")
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


def is_approvable(outcome: ReviewOutcome) -> bool:
    """A failed check or a real test failure removes the Approve button.

    test_ok is None when /test was unavailable (the scheme has no test action),
    which is reported but must not block a diff.
    """
    return outcome.check_ok and outcome.test_ok is not False


class ReviewView(discord.ui.View):
    """Approve / Reject. Nothing is pushed until Approve is pressed."""

    def __init__(self, bot: "MacBuilderBot", outcome: ReviewOutcome) -> None:
        super().__init__(timeout=86400)
        self.bot = bot
        self.outcome = outcome
        self.approvable = is_approvable(outcome)
        self.resolved = False
        self._lock = asyncio.Lock()
        if not self.approvable:
            # A red check or test has nothing safe to approve; Reject remains so
            # the worktree is not left in limbo.
            for child in list(self.children):
                if isinstance(child, discord.ui.Button) and child.label == "Approve":
                    self.remove_item(child)

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
        if not self.approvable:
            await interaction.response.send_message(
                "Not approvable: a check or test failed. Fix it and run the task again.",
                ephemeral=True,
            )
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
            sha = await self.bot.freebuff.approve_and_push(
                env, message=message, expected_snapshot=self.outcome.snapshot
            )
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
        self.outcome.job.status = "approved"
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
        self.outcome.job.status = "rejected"
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
            fallback_models=settings.manager_fallback_models,
            timeout_seconds=settings.manager_timeout_seconds,
            max_tokens=settings.manager_max_tokens,
        )
        self.freebuff = FreebuffRunner(settings)
        self.runner = jobs.JobRunner(
            settings,
            self.manager,
            self.freebuff,
            post_job=self._post_job_result,
            post_review=self._post_review,
            post_question=self._post_question,
            post_pane=self._post_pane,
            post_note=self._post_note,
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
                name="project",
                description="Show which repo, branch and project I work on, and my own code",
                callback=self.cmd_project,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="cancel",
                description="Cancel the running job or a queued one by id",
                callback=self.cmd_cancel,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="plan",
                description="Write a read-only build plan as a markdown file (creates no job)",
                callback=self.cmd_plan,
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
        self.tree.add_command(
            app_commands.Command(
                name="dan",
                description="Have Dan change my own code on a fresh dan/* branch",
                callback=self.cmd_dan,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="answer",
                description="Answer the question Freebuff is waiting on (option number or text)",
                callback=self.cmd_answer,
            )
        )
        self.tree.interaction_check = self.require_allowed
        await self.sync_commands()
        if not self._queue_started:
            self.queue.start()
            self._queue_started = True

    async def _guild_line(self, guild_id: int) -> str:
        """Name GUILD_ID and say whether the bot can see it, before READY.

        setup_hook runs before the gateway sends READY, so self.guilds and
        self.get_guild() are still empty there; the REST listing works at that
        point. A diagnostic that fails must never stop startup, so everything is
        caught here and only ids and names are logged.
        """
        try:
            guilds = await self.http.get_guilds(200, with_counts=False)
        except Exception as exc:  # a diagnostic must never stop startup
            return (
                f"GUILD_ID={guild_id}; could not list the bot's guilds "
                f"({type(exc).__name__})"
            )
        known = [
            (int(item["id"]), str(item.get("name") or "?"))
            for item in guilds or []
            if item.get("id")
        ]
        for known_id, name in known:
            if known_id == guild_id:
                return f"guild {name!r} (id={known_id}): visible to the bot"
        listed = ", ".join(f"{known_id} {name!r}" for known_id, name in known[:10])
        return (
            f"GUILD_ID={guild_id} is not one of the {len(known)} guild(s) this bot "
            f"is in ({listed or 'none'})"
        )

    def _sync_warning(self, exc: discord.HTTPException, where: str) -> None:
        """One line for a sync that failed; the bot keeps running regardless."""
        log.warning(
            "could not sync application commands%s: %s. The bot keeps running with "
            "the commands from its last successful sync; code 50001 means it was "
            "invited without the applications.commands scope, or that GUILD_ID "
            "names a server it is not in.",
            where,
            _http_error_text(exc),
        )

    async def _sync_global(self) -> None:
        """Publish globally after a 50001; a failure here is still a warning."""
        try:
            synced = await self.tree.sync()
        except discord.HTTPException as exc:
            self._sync_warning(exc, " globally")
            return
        log.info("published %d application commands globally", len(synced))

    async def sync_commands(self) -> None:
        """Publish the slash commands; a failed sync must not stop the bot.

        Discord answers 403 with code 50001 (Missing Access) when the bot was
        invited without the `applications.commands` scope, or when GUILD_ID names
        a server it cannot see. That used to escape setup_hook and kill the
        process during login, although the commands from the last successful sync
        still work. It is a warning, not a crash, and a guild sync refused with
        50001 retries once globally so the commands are still published.
        """
        guild_id = self.settings.guild_id
        if not guild_id:
            await self._sync_global()
            return
        log.info("guild diagnostics: %s", await self._guild_line(guild_id))
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) != 50001:
                self._sync_warning(exc, f" to guild {guild_id}")
                return
            log.warning(
                "guild sync to %s failed with code 50001 (Missing Access): %s. "
                "Retrying once globally so the commands are still published; "
                "re-invite the bot with the applications.commands scope to get "
                "them in the guild immediately.",
                guild_id,
                _http_error_text(exc),
            )
            await self._sync_global()
            return
        log.info("synced %d application commands", len(synced))

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
        ctx = self.settings.project
        lines: list[str] = []
        if ctx is not None:
            lines += [f"project: {ctx.project_name}", f"target repo: {ctx.target_remote}"]
        lines += [
            f"queue: {snapshot['pending']} waiting",
            f"running: {running.get('id', 'none')}"
            + (
                f" ({running.get('kind')}, {jobs.format_elapsed(running.get('elapsed'))})"
                if running
                else ""
            ),
            f"authorized branch: {self.settings.authorized_branch}",
            f"manager: {'configured' if self.manager.enabled else 'not configured'}",
            f"questions: detection on · wait {int(self.settings.question_wait_seconds)}s"
            f" · auto-answer {'on' if self.settings.question_auto_answer else 'off'}"
            f" (max {self.settings.question_auto_answer_max}/job)"
            f" · suggest {'on' if self.settings.question_suggest else 'off'}"
            f" · debug {'on' if self.settings.question_debug else 'off'}",
            f"status.sh exit: {code}",
            "",
            tail_text(output, max_lines=40, max_chars=1400),
        ]
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

    async def cmd_project(self, interaction: discord.Interaction) -> None:
        if not await self.require_allowed(interaction):
            return
        ctx = self.settings.project
        text = ctx.summary() if ctx is not None else "Project context is not loaded."
        await interaction.response.send_message("```\n" + text + "\n```", ephemeral=True)

    @app_commands.describe(job_id="Job id to cancel; empty cancels the running job")
    async def cmd_cancel(
        self, interaction: discord.Interaction, job_id: Optional[str] = None
    ) -> None:
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = (job_id or "").strip()
        if target.endswith(".log"):
            target = target[:-4]
        if not target:
            snapshot = self.queue.snapshot()
            running = snapshot.get("running") or {}
            target = str(running.get("id") or "")
            if not target:
                await interaction.followup.send(
                    "Nothing is running; pass a queued job id to cancel one.",
                    ephemeral=True,
                )
                return
        if not LOG_ID_RE.match(target):
            await interaction.followup.send(
                "That is not a job id; copy it from the job message or /logs.",
                ephemeral=True,
            )
            return
        state, job = await self.queue.cancel(target)
        if job is None:
            await interaction.followup.send(
                f"No running or queued job `{target}`.", ephemeral=True
            )
            return
        removed = await self.freebuff.cancel_job(target)
        detail = f" and removed its {', '.join(removed)}" if removed else ""
        await interaction.followup.send(
            f"Cancelled the {state} job `{target}`{detail}. Nothing was pushed and "
            "the branch was left in place.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ plan

    @app_commands.describe(goal="What you want planned; nothing is built and no job is queued")
    async def cmd_plan(self, interaction: discord.Interaction, goal: str) -> None:
        """Text only: no Job, no tmux, no worktree, no edits under mac-builder/."""
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(thinking=True)
        if not self.manager.enabled:
            await interaction.followup.send(
                "The AI manager is not configured, so there is no plan to write. Set "
                "MANAGER_API_BASE, MANAGER_API_KEY and MANAGER_MODEL in `.env`."
            )
            return
        ctx = self.settings.project
        inputs = planner.gather(
            goal=goal,
            project_block=ctx.prompt_block() if ctx is not None else "",
            log_dir=self.settings.log_dir,
            target_dir=self.settings.repo_dir,
            readme=HERE / "README.md",
        )
        try:
            result = await self.manager.plan(inputs)
        except ManagerUnavailable as exc:
            await interaction.followup.send(f"Could not write a plan: {exc}")
            return
        document = planner.plan_document(
            goal,
            result.text,
            bot_name=self.settings.bot_name,
            truncated=result.truncated,
        )
        try:
            path = planner.write_plan(self.settings.log_dir, document)
        except OSError as exc:
            await interaction.followup.send(f"Could not save the plan: {exc}")
            return
        note = (
            " It was cut off at the model's token limit, so it is incomplete."
            if result.truncated
            else ""
        )
        await interaction.followup.send(
            f"Plan for `{inputs.goal[:80]}`.{note} Read-only: no job was created and "
            f"nothing was built. Saved as `{planner.PLAN_DIR_NAME}/{path.name}`; every "
            "claim is labelled verified or assumed.",
            file=discord.File(path, filename=path.name),
        )

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

    @app_commands.describe(text="What Dan should change in mac-builder/ on a fresh dan/* branch")
    async def cmd_dan(self, interaction: discord.Interaction, text: str) -> None:
        """A self-build: her own checkout, branched from a freshly fetched origin/main."""
        if not await self.require_allowed(interaction):
            return
        try:
            task_text = rules.sanitize_task_text(text)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        job = jobs.make_job(
            self.settings,
            kind="dan",
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
        await interaction.response.send_message(
            f"Queued self-build `{job.id}` (position {position}) on `dan/{job.id}`, "
            "branched from a freshly fetched origin/main. It may edit `mac-builder/` "
            "except the guardrail files; nothing is pushed until you press Approve "
            "(only `dan/*` can be pushed from it).",
            ephemeral=True,
        )

    # ------------------------------------------------------------- questions

    @app_commands.describe(text="An option number from the question, or free text")
    async def cmd_answer(self, interaction: discord.Interaction, text: str) -> None:
        """Answer the one pending Freebuff question; the allowlist is checked first."""
        if not await self.require_allowed(interaction):
            return
        pending = self.freebuff.pending_question
        if pending is None:
            await interaction.response.send_message(
                "There is no pending question: Freebuff is not waiting on anything.",
                ephemeral=True,
            )
            return
        answer = questions.flatten_answer(text)
        if not answer:
            await interaction.response.send_message(
                "That answer is empty; send an option number or some text.",
                ephemeral=True,
            )
            return
        count = len(pending.options)
        shown = questions.option_number(answer, count)
        if shown is None and answer.isdigit() and count:
            # The number must name a row the pane actually shows.
            await interaction.response.send_message(
                f"Question {pending.number} has {count} option"
                f"{'' if count == 1 else 's'}; send 1-{count} or some free text.",
                ephemeral=True,
            )
            return
        if not await self.freebuff.submit_answer(answer):
            await interaction.response.send_message(
                "That question was already answered or is gone.", ephemeral=True
            )
            return
        if shown is not None:
            detail = f"option {shown} ({pending.options[shown - 1].label})"
        elif pending.custom_index is not None:
            detail = "the Custom field"
        else:
            detail = "free text"
        await interaction.response.send_message(
            f"Answered question {pending.number} of {pending.total} as {detail}: "
            f"`{answer[:100]}`",
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
        ctx = self.settings.project
        parts = [
            ctx.prompt_block() if ctx is not None else "",
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

    async def _post_question(self, job: Job, question: questions.Question) -> bool:
        """Post a pending question where the job was started; /answer resolves it."""
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for the question in job %s", job.id)
            return False
        lines = [f"**{questions.redact(question.text)}**", ""]
        for index, option in enumerate(question.options, start=1):
            line = f"`{index}` {questions.redact(option.label)}"
            if option.description:
                line += f"\n   {questions.redact(option.description)}"
            lines.append(line)
        if question.free_text and not question.options:
            lines += ["", "This one is free text: send your answer as the text."]
        lines += ["", "Answer with `/answer <number>` or `/answer <text>`."]
        embed = discord.Embed(
            title=(
                f"❓ Freebuff is waiting for an answer "
                f"(question {question.number} of {question.total})"
            ),
            description="\n".join(lines)[:3900],
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Job", value=f"`{job.id}`", inline=True)
        embed.add_field(name="Kind", value=job.kind, inline=True)
        embed.add_field(name="Log", value=f"`{job.log_path.name}`", inline=True)
        embed.set_footer(
            text=(
                "The job is waiting and its idle timer is paused. It answers one "
                "question at a time and only then presses Submit."
            )
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("could not post the question for job %s", job.id)
            return False
        return True

    async def _post_note(self, job: Job, text: str) -> None:
        """A manager line for the owner: an auto-answer record or a suggestion."""
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for a manager note in job %s", job.id)
            return
        try:
            await channel.send(questions.redact(text)[:1900])
        except Exception:
            log.exception("could not post a manager note for job %s", job.id)

    async def _post_pane(self, job: Job, tail: str) -> bool:
        """A box that did not parse: show the pane so the owner is never blind."""
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for the unreadable box in job %s", job.id)
            return False
        pane = questions.redact(tail or "(the pane was empty)").replace("```", "'''")[-1800:]
        embed = discord.Embed(
            title="⚠️ Freebuff is on a question box I could not read",
            description=(
                "The box is on screen but no numbered question could be parsed from "
                "it, so nothing was guessed and nothing was typed. The job keeps "
                "waiting; the pane is below. `/logs` has the same text.\n\n```\n"
                + pane
                + "\n```"
            )[:4000],
            color=discord.Color.orange(),
        )
        embed.add_field(name="Job", value=f"`{job.id}`", inline=True)
        embed.add_field(name="Log", value=f"`{job.log_path.name}`", inline=True)
        embed.set_footer(
            text=(
                "If it stays unreadable it is dismissed with Esc and the job fails "
                "after QUESTION_WAIT_SECONDS."
            )
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("could not post the pane tail for job %s", job.id)
            return False
        return True

    async def _post_job_result(self, job: Job, ok: bool, summary: str) -> None:
        if job.status in ("awaiting_review", "blocked"):
            # The review embed is this task's result; a second embed would only
            # repeat it with less information.
            return
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for job %s", job.id)
            return
        embed = discord.Embed(
            title=("✅ " if ok else "❌ ") + f"{job.kind} finished",
            # job.summary can carry a note from a review that never reached
            # Discord; the summary argument alone would not include it.
            description=tail_text(job.summary or summary, max_lines=14, max_chars=1800)
            or "(no output)",
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
            if await self._send_review(channel, outcome, embed=embed):
                outcome.job.status = "blocked"
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
        embed.set_footer(
            text=(
                "Nothing is pushed unless you press Approve."
                if is_approvable(outcome)
                else "A check or test failed, so there is no Approve button."
            )
        )
        view = ReviewView(self, outcome)
        if await self._send_review(channel, outcome, embed=embed, view=view):
            outcome.job.status = "awaiting_review"

    async def _send_review(
        self,
        channel: discord.abc.Messageable,
        outcome: ReviewOutcome,
        *,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> bool:
        """Post the review. A Discord failure must not hide the result too."""
        try:
            await channel.send(embed=embed, view=view)
        except Exception as exc:
            # Exception, not BaseException: a cancellation still propagates.
            log.exception("could not post review for job %s", outcome.job.id)
            self._note_review_failure(outcome, exc)
            return False
        return True

    def _note_review_failure(self, outcome: ReviewOutcome, exc: BaseException) -> None:
        """Say on the job that a review is waiting but never reached Discord."""
        branch = outcome.environment.branch if outcome.environment else "?"
        note = (
            f"Review could not be posted to Discord ({type(exc).__name__}). "
            f"Branch {branch} and its worktree are kept. "
            "There is no Approve button; inspect the branch or re-run."
        )
        existing = (outcome.job.summary or "").strip()
        outcome.job.summary = f"{existing}\n\n{note}" if existing else note


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

    # Say which folder this process is really running from, before anything can
    # hide it: the plist's path, the .env it loaded and where the logs land.
    env_file = HERE / ".env"
    log.info(
        "code: %s [%s] · env: %s · logs: %s",
        REPO_ROOT,
        code_location(REPO_ROOT),
        f"{env_file}{'' if env_file.is_file() else ' (missing)'}",
        settings.log_dir,
    )

    ctx = project_context.build_context(
        target_dir=settings.repo_dir,
        authorized_branch=settings.authorized_branch,
        self_dir=REPO_ROOT,
        project_name=settings.project_name,
        builder_name=settings.builder_name,
        bot_name=settings.bot_name,
    )
    try:
        project_context.preflight(ctx)
    except project_context.PreflightError as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1
    settings = replace(settings, project=ctx)
    log.info("project context:\n%s", ctx.summary())
    log.info(
        "question detection enabled: poll %.0fs, wait %.0fs, auto-answer %s "
        "(max %d/job, cooldown %.0fs), suggest %s, debug %s",
        settings.freebuff_poll_seconds,
        settings.question_wait_seconds,
        "on" if settings.question_auto_answer else "off",
        settings.question_auto_answer_max,
        settings.question_manager_cooldown_seconds,
        "on" if settings.question_suggest else "off",
        "on" if settings.question_debug else "off",
    )

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
