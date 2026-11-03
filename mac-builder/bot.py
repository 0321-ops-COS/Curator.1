#!/usr/bin/env python3
"""Discord front end for the private Mac build bot.

All Discord input is treated as untrusted. Commands only select fixed scripts or
plain task text for Freebuff; buttons check the allowlist again on every press.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
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

import audit  # noqa: E402
import compare_link  # noqa: E402
import github_client  # noqa: E402
import jobs  # noqa: E402
import merge_command  # noqa: E402
import review_record  # noqa: E402
import roadmap  # noqa: E402
import test_card  # noqa: E402
import planner  # noqa: E402
import project_context  # noqa: E402
import projects  # noqa: E402
import questions  # noqa: E402
import rules  # noqa: E402
import screens  # noqa: E402
import selftest  # noqa: E402
import sheet_card  # noqa: E402
import heartbeat
import status_board  # noqa: E402
import supervision  # noqa: E402
import update_watch  # noqa: E402
from freebuff_runner import FreebuffError, FreebuffRunner  # noqa: E402
from jobs import Job, ReviewOutcome, tail_text  # noqa: E402
from manager import Manager, ManagerUnavailable  # noqa: E402

log = logging.getLogger("macbuilder")

DEFAULT_AUTHORIZED_BRANCH = "freebuff/ce4b8907c250c30103d8993e"
LOG_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
# How often the board re-checks queue positions and silent stages.
BOARD_TICK_SECONDS = 30.0
# A roadmap review that nobody has approved yet is re-pinged this often.
ROADMAP_PING_SECONDS = 6 * 3600.0
# Waiting for a merge survives a restart, so it is written beside the roadmap.
ROADMAP_MERGE_FILE = "roadmap-merge.json"
# How long a /roadmap without a sheet waits for that sheet as the next message.
ROADMAP_WAIT_SECONDS = 10 * 60.0
# A waiting update re-checks idle this often, and pings the channel this often.
UPDATE_TICK_SECONDS = 30.0
UPDATE_PING_SECONDS = 3600.0
# How often the working job's pane is read for the heartbeat.
HEARTBEAT_TICK_SECONDS = 20.0
# Discord rejects a message over 2000 characters; every /roadmap reply fits this.
REPLY_LIMIT = 1900


def _clip_reply(text: str) -> str:
    """One Discord-safe reply; the roadmap preview already stays under the limit."""
    if len(text) <= REPLY_LIMIT:
        return text
    return text[:REPLY_LIMIT - 3].rstrip() + "..."


class DiscordBoardTransport(status_board.BoardTransport):
    """The only Discord-aware piece of the board: send text, edit a message.

    Both calls answer False/None instead of raising, so a deleted board or a
    failed edit costs one new message and never touches the job.
    """

    def __init__(self, bot: "MacBuilderBot") -> None:
        self.bot = bot

    async def send(self, channel_id: int, text: str) -> str | None:
        channel = self.bot._target_channel(channel_id)
        if channel is None:
            return None
        try:
            message = await channel.send(text[:1900])
        except Exception:
            log.exception("could not post a status board")
            return None
        message_id = str(getattr(message, "id", "") or "")
        return message_id or None

    async def edit(self, channel_id: int, message_id: str, text: str) -> bool:
        channel = self.bot._target_channel(channel_id)
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=text[:1900])
        except Exception:
            # Deleted, or no permission: the caller posts one new board.
            return False
        return True


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
    # A pane and a worktree both unchanged this long is a stuck screen: it is
    # posted once with /screen, /key and /send, and the clocks pause.
    stall_seconds: float = 90.0
    # The live board: how often one message may be edited, and whether messages
    # are sent at all. The pane heartbeat replaces the stage-stall ping: one
    # "still working" message this often while the pane keeps changing, and the
    # only silence that ever counts as stuck.
    status_edit_min_seconds: float = 15.0
    status_pings: bool = True
    heartbeat_ping_seconds: float = 600.0
    stall_no_output_seconds: float = 300.0
    # While Freebuff works, one short digest ping this often; 0 turns it off.
    status_digest_seconds: float = 900.0
    # How often to look for newer code on origin; 0 disables the check.
    update_check_seconds: float = 600.0
    # Filled in by main() after the startup preflight; None before that.
    project: "project_context.ProjectContext | None" = None
    # The projects jobs may be routed to; "hustler" is always in it.
    projects: "projects.Registry | None" = None

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

        # "hustler" is this checkout, so a Mac with no projects.json is exactly
        # the single-project bot it was; extras come from that optional file.
        registry = projects.load(
            repo_dir=repo_dir,
            authorized_branch=branch,
            project_name=_env("PROJECT_NAME"),
            path=HERE / "projects.json",
        )

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
            stall_seconds=_env_float("STALL_SECONDS", 90.0),
            status_edit_min_seconds=_env_float("STATUS_EDIT_MIN_SECONDS", 15.0),
            heartbeat_ping_seconds=_env_float("HEARTBEAT_PING_SECONDS", 600.0),
            stall_no_output_seconds=_env_float("STALL_NO_OUTPUT_SECONDS", 300.0),
            status_pings=_env_bool("STATUS_PINGS", True),
            status_digest_seconds=_env_float("STATUS_DIGEST_SECONDS", 900.0),
            update_check_seconds=_env_float("UPDATE_CHECK_SECONDS", 600.0),
            projects=registry,
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
        if settings.stall_seconds < 1:
            raise ConfigError("STALL_SECONDS must be at least 1")
        if settings.status_edit_min_seconds < 0:
            raise ConfigError("STATUS_EDIT_MIN_SECONDS must be 0 or more")
        if settings.heartbeat_ping_seconds < 0:
            raise ConfigError("HEARTBEAT_PING_SECONDS must be 0 or more")
        if settings.stall_no_output_seconds < 1:
            raise ConfigError("STALL_NO_OUTPUT_SECONDS must be at least 1")
        if settings.status_digest_seconds < 0:
            raise ConfigError("STATUS_DIGEST_SECONDS must be 0 or more")
        if settings.update_check_seconds < 0:
            raise ConfigError("UPDATE_CHECK_SECONDS must be 0 or more")
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


def project_label(settings: object, job: object) -> str:
    """The project a job builds, in words, for /status and a review card.

    A /dan job builds mac-builder/ in her own checkout, which is not one of the
    registered projects, so it says so instead of naming the default project.
    """
    if str(getattr(job, "kind", "")) == "dan":
        return "mac-builder (her own code)"
    return projects.label_for(settings, getattr(job, "project", ""))


def review_compare_url(settings: object, outcome: ReviewOutcome) -> str | None:
    """The GitHub compare link for a review card, or None when it cannot exist.

    The repo is the one the job's branch was pushed to: the target checkout for
    a /task, her own checkout for a /dan. The base drops git's ``origin/``
    prefix, so a /task shows the authorized branch and a /dan shows ``main``.
    """
    env = outcome.environment
    project = getattr(settings, "project", None)
    if env is None or project is None:
        return None
    remote = projects.remote_for(
        settings,
        getattr(outcome.job, "project", ""),
        dan=getattr(env, "kind", "task") == "dan",
    )
    if not remote:
        return None
    base = getattr(env, "base_branch", "") or ""
    if base.startswith("origin/"):
        base = base[len("origin/"):]
    return compare_link.compare_url(remote, base, env.branch)


def approve_confirm_text(settings: object, outcome: ReviewOutcome, user_id: int,
                         sha: str) -> str:
    """What the Approve button confirms, with the link still in front of him."""
    link = review_compare_url(settings, outcome)
    return (
        f"✅ Approved by <@{user_id}>. Pushed `{outcome.environment.branch}` at `{sha[:7]}`. "
        "Merging is yours; this bot never merges.\n"
        + (f"Open and merge: {link}" if link else "No compare link available")
    )


# Stages no builder job runs: a live Discord and GitHub call, and the simulator
# needs a booted GUI session. The report names them instead of implying they ran.
UNVERIFIED_STAGES = ("live Discord", "live GitHub", "simulator")


def dan_report_text(outcome: ReviewOutcome) -> str:
    """What the job recorded, with what was not verified named.

    Every value is a recorded fact (repo and branch from the runner, commit from
    the review's snapshot, counts from the change set, the check stage's state);
    a value nobody recorded says so, and the builder's summary is no evidence.
    """
    job, env = outcome.job, outcome.environment
    branch = getattr(env, "branch", "") or job.branch or "not recorded"
    head = outcome.snapshot.partition(":")[0]
    files = outcome.changed_files
    names = ", ".join(f"`{name}`" for name in files[:3])
    if len(files) > 3:
        names += f" and {len(files) - 3} more"
    check = outcome.check_state or "not recorded"
    check += f" · test {outcome.test_state}" if outcome.test_state else ""
    lines = [
        f"**Report — `{job.id}`**",
        f"Repo: `{job.repo_slug or 'not recorded'}`",
        f"Branch: `{branch}`",
        f"Commit: `{head or 'not recorded'}`",
        f"Files changed: {len(files)}" + (f" — {names}" if names else ""),
        f"Diff lines: {outcome.diff_lines}",
        f"Check: {check}",
        "Not verified: " + ", ".join(UNVERIFIED_STAGES),
    ]
    return "\n".join(lines)[:1900]


def is_approvable(outcome: ReviewOutcome) -> bool:
    """A failed check or a real test failure removes the Approve button.

    test_ok is None when /test was unavailable (the scheme has no test action),
    which is reported but must not block a diff.
    """
    return outcome.check_ok and outcome.test_ok is not False


def review_embed(settings: object, outcome: ReviewOutcome) -> discord.Embed:
    """The review card. A /review refresh builds it from the same facts."""
    titles = {"approve": "✅ Manager approved — waiting for your decision",
              "ask_user": "🟡 Manager needs your decision"}
    colors = {"approve": discord.Color.green(), "ask_user": discord.Color.orange()}
    embed = discord.Embed(
        title=titles.get(outcome.verdict, "Review"),
        description=(f"**Task:** {outcome.job.text[:300]}\n\n**Manager notes:**\n"
                     f"{outcome.notes[:3000] or '(none)'}"),
        color=colors.get(outcome.verdict, discord.Color.orange()))
    branch = outcome.environment.branch if outcome.environment else "?"
    embed.add_field(name="Project", value=project_label(settings, outcome.job), inline=True)
    embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(name="Changed lines", value=str(outcome.diff_lines), inline=True)
    embed.add_field(name="Check", value=outcome.check_state[:100], inline=True)
    embed.add_field(name="Test", value=outcome.test_state[:100], inline=True)
    embed.add_field(name="Files", value=str(len(outcome.changed_files)), inline=True)
    embed.add_field(name="Job", value=f"`{outcome.job.id}`", inline=True)
    embed.add_field(name="Log", value=f"`{outcome.job.log_path.name}`", inline=False)
    if outcome.changed_files:
        embed.add_field(name="Uncommitted", inline=False, value=(
            f"{len(outcome.changed_files)} file(s) not committed yet; Approve "
            "commits them and pushes only this branch"))
    link = review_compare_url(settings, outcome)
    if link:
        embed.add_field(name="Compare / merge link", value=link, inline=False)
    embed.set_footer(text=(
        "Nothing is pushed unless you press Approve." if is_approvable(outcome)
        else "A check or test failed, so there is no Approve button."))
    return embed


class ReviewView(discord.ui.View):
    """Approve / Reject. Nothing is pushed until Approve is pressed."""

    def __init__(self, bot: "MacBuilderBot", outcome: ReviewOutcome, *,
                 refreshed: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.outcome = outcome
        # A refreshed card re-checks the saved review and the worktree first: the
        # buttons are new, but nothing about what may be pushed is looser.
        self.refreshed = refreshed
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

    def _decided(self, status: str) -> None:
        """Tell the bot what was decided; a fake bot without the hook is fine."""
        hook = getattr(self.bot, "review_decided", None)
        if hook is not None:
            hook(self.outcome.job, status)

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
        if self.refreshed:
            why = await self.bot.review_refusal(self.outcome)
            if why:
                await self._finish(interaction, why)
                return
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
        self._decided("approved")
        await self._finish(
            interaction,
            approve_confirm_text(self.bot.settings, self.outcome, interaction.user.id, sha),
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
        self._decided("rejected")
        branch = self.outcome.environment.branch if self.outcome.environment else "none"
        await self._finish(
            interaction,
            f"❌ Rejected by <@{interaction.user.id}>. Nothing was pushed. "
            f"Branch `{branch}` and its worktree were kept for inspection.",
        )


class UpdateNoticeView(discord.ui.View):
    """The update offer; every button checks the allowlist again."""

    def __init__(self, bot: "MacBuilderBot", tip: str) -> None:
        super().__init__(timeout=None)
        self.bot, self.tip = bot, tip

    @discord.ui.button(label="Update when idle", style=discord.ButtonStyle.success)
    async def update(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            await self.bot.start_update_wait(i, self.tip)

    @discord.ui.button(label="Later", style=discord.ButtonStyle.secondary)
    async def later(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            await i.response.send_message("OK, later. Nothing changed.", ephemeral=True)

    @discord.ui.button(label="Skip this version", style=discord.ButtonStyle.danger)
    async def skip(self, i: discord.Interaction, b: discord.ui.Button) -> None:
        if await self.bot.require_allowed(i):
            self.bot._skipped_tip = self.tip
            await i.response.send_message(
                f"Skipped `{self.tip[:7]}`; I won't offer it again.", ephemeral=True)


class MacBuilderBot(discord.Client):
    _update_waiting: dict | None = None
    _sheet_draft: sheet_card.Draft | None = None

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents)
        self.settings = settings
        # Read-only: fetch and inspect only, never a pull or a checkout.
        self._update_watcher = update_watch.UpdateWatcher(
            root=REPO_ROOT,
            repo_dir=getattr(settings, "repo_dir", REPO_ROOT),
            branch=getattr(settings, "authorized_branch", ""),
            interval_seconds=getattr(settings, "update_check_seconds", 600.0),
        )
        self._update_ticker: asyncio.Task[None] | None = None
        self.supervisor = supervision.Supervisor(REPO_ROOT, settings.log_dir)
        self._supervised = supervision.is_supervised()
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
        self._board = status_board.BoardPublisher(
            DiscordBoardTransport(self),
            edit_min_seconds=settings.status_edit_min_seconds,
            # The pane heartbeat owns "stuck" now; a stage clock no longer does.
            stall_ping_seconds=0.0,
            pings=settings.status_pings,
            digest_seconds=settings.status_digest_seconds,
        )
        self._heartbeat = heartbeat.Heartbeat(
            ping_seconds=settings.heartbeat_ping_seconds,
            stall_seconds=settings.stall_no_output_seconds,
        )
        self._heartbeat_ticker: asyncio.Task[None] | None = None
        self._board_tasks: set[asyncio.Task[None]] = set()
        # The reviewed jobs /merge may gate, by id, newest last.
        self._merge_jobs: dict[str, Job] = {}
        self._board_ticker: asyncio.Task[None] | None = None
        self._roadmap_jobs: dict[str, Job] = {}
        self._roadmap_job: Job | None = None
        # A roadmap saved before the last restart is resumed, not forgotten.
        (self._roadmap_steps, self._roadmap_channel_id, self._roadmap_requester_id,
         self._roadmap_paused) = roadmap.load(self._roadmap_path())
        self._roadmap_restored = len(self._roadmap_steps)
        # The job the roadmap was following is saved beside it, so a restart picks
        # it up again even when the roadmap was paused; without it a pause before
        # a restart left a roadmap that could be resumed but not continued.
        self._roadmap_job = self._roadmap_restore_current()
        self._roadmap_stuck = ""
        # When each waiting job was last reminded of, or is next due one.
        self._roadmap_reminder: dict[str, float] = {}
        # Requester id -> (channel id, mode, expiry) for a sheet still to arrive.
        self._roadmap_waiting: dict[int, tuple[int, str, float]] = {}
        # The approved job whose merge the next sheet job waits for, if any.
        self._roadmap_merge: dict | None = self._roadmap_merge_load()
        self._sheet_draft = sheet_card.load(self._sheet_draft_path())
        self.runner = jobs.JobRunner(
            settings,
            self.manager,
            self.freebuff,
            post_job=self._post_job_result,
            post_review=self._post_review,
            post_question=self._post_question,
            post_pane=self._post_pane,
            post_screen=self._post_screen,
            post_note=self._post_note,
            on_stage=self._on_stage,
        )
        self.queue = jobs.JobQueue(
            self._run_job,
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
                description="Run swift test in HustlerCore on a branch (fast, queued)",
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
                description="Build a branch from origin with xcodegen + xcodebuild (queued)",
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
        self.tree.add_command(
            app_commands.Command(
                name="screen",
                description="Show the Freebuff pane for the running job (read-only)",
                callback=self.cmd_screen,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="key",
                description="Send one key to a stuck Freebuff screen (Enter, Esc, arrows, Tab, y, n)",
                callback=self.cmd_key,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="send",
                description="Paste text into a stuck Freebuff screen, the way /task does",
                callback=self.cmd_send,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="board",
                description="Post a fresh status board for the running job",
                callback=self.cmd_board,
            )
        )
        self.tree.add_command(app_commands.Command(
            name="merge", description="Gate an approved review and merge it (dry run)",
            callback=self.cmd_merge))
        self.tree.add_command(app_commands.Command(
            name="roadmap", callback=self.cmd_roadmap,
            description="Queue a build sheet: one job per '## Job', one at a time"))
        self.tree.add_command(app_commands.Command(
            name="review", callback=self.cmd_review,
            description="Re-post live Approve/Reject cards for reviews waiting on you"))
        self.tree.add_command(app_commands.Command(
            name="audit", callback=self.cmd_audit,
            description="Run check, build, test and the parity drift on detached worktrees"))
        self.tree.add_command(app_commands.Command(
            name="selftest", callback=self.cmd_selftest, description="Run each stage on fakes"))
        self.tree.interaction_check = self.require_allowed
        if self._board_ticker is None or self._board_ticker.done():
            self._board_ticker = asyncio.create_task(self._board_loop())
        if self._heartbeat_ticker is None or self._heartbeat_ticker.done():
            self._heartbeat_ticker = asyncio.create_task(self._heartbeat_loop())
        if self._update_watcher.enabled and (
            self._update_ticker is None or self._update_ticker.done()
        ):
            self._update_ticker = asyncio.create_task(self._update_loop())
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
        applied = self.supervisor.take_applied() if getattr(self, "supervisor", None) else None
        if applied:
            self._roadmap_restored = 0
            left = len(self._roadmap_steps)
            channel = self._target_channel(int(applied.get("channel_id") or 0))
            state = f"Roadmap restored: {left} job(s) left." if left else "No roadmap was queued."
            try:
                if channel is not None:
                    await channel.send(
                        f"Updated `{applied['old'][:7]}` -> `{applied['new'][:7]}`: "
                        f"{applied.get('subjects') or 'no commits'}. {state}")
                    posted = await self._repost_reviews(channel)
                    if posted:
                        await channel.send(f"Re-posted `{', '.join(posted)}`; nothing was approved.")
            except Exception:
                log.exception("could not announce the applied update")
        if self._roadmap_restored:
            channel = self._target_channel(self._roadmap_channel_id)
            try:
                if channel is not None:
                    await channel.send(
                        f"Roadmap restored: {self._roadmap_restored} job(s) left")
                    self._roadmap_restored = 0
            except Exception:
                log.warning("could not announce the restored roadmap")
        if self._sheet_draft is not None:
            await self._restore_sheet_draft()

    async def close(self) -> None:
        tickers = [self._board_ticker, self._heartbeat_ticker, self._update_ticker]
        self._board_ticker = self._heartbeat_ticker = self._update_ticker = None
        for ticker in tickers:
            if ticker is not None:
                ticker.cancel()
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
        """The live board for the running job plus one line per waiting job.

        Read-only and immediate: no probe, no git, nothing that can stall a
        phone. The numbers are the real stage, the real elapsed time and the
        real queue position; nothing here guesses at completion.
        """
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        running = getattr(self.queue, "running", None)
        publisher = getattr(self, "_board", None)
        board = None
        if running is not None and publisher is not None:
            board = publisher.board_for(running.id)
        lines: list[str] = []
        if board is not None:
            lines.append(status_board.render(board, now=time.time()))
        elif running is not None:
            lines.append(
                f"`{running.id}` · {running.kind} · running "
                f"{jobs.format_elapsed(running.duration)}"
            )
        else:
            lines.append("No job is running.")
        waiting_fn = getattr(self.queue, "waiting", None)
        waiting_jobs = list(waiting_fn()) if callable(waiting_fn) else []
        waiting = [
            (position, job.id, job.kind, job.title)
            for position, job in enumerate(waiting_jobs, start=1)
        ]
        lines += ["", "waiting:", status_board.render_waiting(waiting), ""]
        code, output = await jobs.probe_status(self.settings)
        snapshot = self.queue.snapshot()
        if getattr(self, "_roadmap_paused", False):
            lines.append(f"roadmap: paused, {self._roadmap_left()} job(s) left")
        ctx = getattr(self.settings, "project", None)
        if ctx is not None:
            lines += [f"project: {ctx.project_name}", f"target repo: {ctx.target_remote}"]
        registry = getattr(self.settings, "projects", None)
        if isinstance(registry, projects.Registry):
            lines.append(f"projects: {', '.join(registry.ids())}")
            if running is not None:
                lines.append(f"job project: {project_label(self.settings, running)}")
        watcher = getattr(self, "_update_watcher", None)
        lines += [
            f"update: {watcher.status_line(getattr(self, '_supervised', None)) if watcher else 'not checked yet'}",
            f"queue: {snapshot['pending']} waiting",
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
        await interaction.followup.send(
            "```\n" + "\n".join(lines) + "\n```", ephemeral=True
        )

    async def cmd_project(self, interaction: discord.Interaction) -> None:
        if not await self.require_allowed(interaction):
            return
        ctx = self.settings.project
        text = ctx.summary() if ctx is not None else "Project context is not loaded."
        registry = getattr(self.settings, "projects", None)
        if isinstance(registry, projects.Registry):
            text += "\n\n" + "\n".join(registry.summary_lines())
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

    async def _queue_script_job(
        self,
        interaction: discord.Interaction,
        kind: str,
        branch: Optional[str] = None,
    ) -> None:
        if not await self.require_allowed(interaction):
            return
        try:
            chosen = jobs.resolve_branch(self.settings, branch)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # One build at a time: the same worktree would otherwise be checked out
        # from under the running script.
        running = self.queue.running
        if running is not None and running.kind in jobs.BUILD_KINDS:
            await interaction.response.send_message(
                f"`/{running.kind}` is already running (`{running.id}`, "
                f"{jobs.format_elapsed(running.duration)}); nothing was queued. "
                "Wait for it, or `/cancel` it first.",
                ephemeral=True,
            )
            return
        titles = {
            "check": "swift test (HustlerCore)",
            "test": "xcodegen + xcodebuild test",
            "build": "xcodegen + xcodebuild build",
        }
        job = jobs.make_job(
            self.settings,
            kind=kind,
            title=f"{titles[kind]} on {chosen}",
            channel_id=interaction.channel_id or 0,
            requester_id=interaction.user.id,
            branch=chosen,
        )
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Queued `/{kind}` on `{chosen}` as `{job.id}` (position {position}). "
            f"Log: `{job.log_path.name}`.",
            ephemeral=True,
        )

    @app_commands.describe(
        branch="Branch to check: the authorized branch, or a freebuff/ or dan/ branch"
    )
    async def cmd_check(
        self, interaction: discord.Interaction, branch: Optional[str] = None
    ) -> None:
        await self._queue_script_job(interaction, "check", branch)

    async def cmd_test(self, interaction: discord.Interaction) -> None:
        await self._queue_script_job(interaction, "test")

    @app_commands.describe(
        branch="Branch to build: the authorized branch, or a freebuff/ or dan/ branch"
    )
    async def cmd_build(
        self, interaction: discord.Interaction, branch: Optional[str] = None
    ) -> None:
        await self._queue_script_job(interaction, "build", branch)

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

    async def _task_text(
        self,
        interaction: discord.Interaction,
        text: str,
        attachment: Optional[discord.Attachment],
    ) -> str | None:
        """Typed text plus an attached .txt/.md file; None after saying why not."""
        try:
            typed = rules.sanitize_task_text(text) if (text or "").strip() else ""
            attached = ""
            if attachment is not None:
                try:
                    data = await attachment.read()
                except discord.HTTPException:
                    await interaction.response.send_message(
                        "I could not read that attachment; post the file again.",
                        ephemeral=True,
                    )
                    return None
                attached = rules.attachment_task_text(
                    getattr(attachment, "filename", ""), data
                )
            combined = rules.combine_task_text(typed, attached)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return None
        if not combined:
            await interaction.response.send_message(
                "Give me a task: some text, a .txt/.md attachment, or both.",
                ephemeral=True,
            )
            return None
        return combined

    async def task_project_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """The registry's projects, filtered by what has been typed so far."""
        registry = getattr(self.settings, "projects", None)
        if not isinstance(registry, projects.Registry):
            return []
        typed = (current or "").strip().lower()
        return [
            app_commands.Choice(name=f"{name} ({project_id})", value=project_id)
            for project_id, name in registry.choices()
            if typed in project_id.lower() or typed in name.lower()
        ][:25]

    @app_commands.describe(
        text="What Freebuff should do on a fresh freebuff/* branch",
        attachment="Optional .txt or .md file (max 200 KB) appended to the text",
        project="Which registered project to build; empty is hustler",
    )
    @app_commands.autocomplete(project=task_project_autocomplete)
    async def cmd_task(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, rules.MAX_TASK_CHARS] = "",
        attachment: Optional[discord.Attachment] = None,
        project: Optional[str] = None,
    ) -> None:
        """A Freebuff task on its own branch; its project is fixed when queued."""
        if not await self.require_allowed(interaction):
            return
        refusal = self._update_refusal()
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        chosen = (project or "").strip()
        registry = getattr(self.settings, "projects", None)
        if chosen and isinstance(registry, projects.Registry) and registry.get(chosen) is None:
            await interaction.response.send_message(
                f"Unknown project {chosen!r}. Known: {', '.join(registry.ids())}.",
                ephemeral=True,
            )
            return
        task_text = await self._task_text(interaction, text, attachment)
        if task_text is None:
            return
        job = jobs.make_job(
            self.settings,
            kind="task",
            title=task_text[:100],
            channel_id=interaction.channel_id or 0,
            requester_id=interaction.user.id,
            text=task_text,
            project=chosen,
        )
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self._track_board(job, position)
        warning = "" if self.manager.enabled else " (AI manager is not configured.)"
        where = f" on project `{job.project}`" if chosen else ""
        await interaction.response.send_message(
            f"Queued Freebuff task `{job.id}` (position {position}){where}.{warning}",
            ephemeral=True,
        )

    @app_commands.describe(
        text="What Dan should change in mac-builder/ on a fresh dan/* branch",
        attachment="Optional .txt or .md file (max 200 KB) appended to the text",
    )
    async def cmd_dan(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, rules.MAX_TASK_CHARS] = "",
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """A self-build: her own checkout, branched from a freshly fetched origin/main."""
        if not await self.require_allowed(interaction):
            return
        refusal = self._update_refusal()
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        task_text = await self._task_text(interaction, text, attachment)
        if task_text is None:
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
        await self._track_board(job, position)
        await interaction.response.send_message(
            f"Queued self-build `{job.id}` (position {position}) on `dan/{job.id}`, "
            "branched from a freshly fetched origin/main. It may edit `mac-builder/` "
            "except the guardrail files; nothing is pushed until you press Approve "
            "(only `dan/*` can be pushed from it).",
            ephemeral=True,
        )

    # ------------------------------------------------------------- roadmap

    @app_commands.describe(
        sheet="One .md build sheet: each '## Job' heading starts a job",
        action="status lists it, pause, resume, skip or stop it; preview or start takes the sheet next",
    )
    async def cmd_roadmap(self, interaction: discord.Interaction,
                          sheet: Optional[discord.Attachment] = None,
                          action: Optional[str] = None) -> None:
        """Queue a build sheet one job at a time; every job still waits for Approve."""
        if not await self.require_allowed(interaction):
            return
        # One waiting sheet per requester: any new command replaces the old one.
        self._roadmap_waiting.pop(interaction.user.id, None)
        choice = (action or "").strip().lower()
        if choice in ("status", "stop", "pause", "resume", "skip"):
            if choice == "status" and self._update_waiting is not None:
                text = (f"Roadmap paused while an update waits: "
                        f"{self._roadmap_left()} job(s) left.")
            elif choice in ("pause", "resume"):
                text = self._roadmap_set_paused(choice == "pause")
            elif choice == "skip":
                text = await self._roadmap_skip()
            else:
                text = self._roadmap_status() if choice == "status" else self._roadmap_stop()
            await interaction.response.send_message(text, ephemeral=True)
            return
        if sheet is not None:
            if not sheet.filename.lower().endswith(".md"):
                await interaction.response.send_message(
                    "Attach one .md build sheet, or use `action` status, preview, "
                    "pause, resume, skip or stop.", ephemeral=True)
                return
            text = await self._roadmap_apply(
                sheet, "preview" if choice == "preview" else "start",
                interaction.channel_id or 0, interaction.user.id)
            await interaction.response.send_message(text, ephemeral=True)
            return
        # No file attached (a phone cannot add one): take it as the next message.
        self._roadmap_waiting[interaction.user.id] = (
            interaction.channel_id or 0, "preview" if choice == "preview" else "start",
            time.time() + ROADMAP_WAIT_SECONDS)
        await interaction.response.send_message(
            "Send the .md sheet as your next message in this channel within 10 minutes.",
            ephemeral=True)

    # --------------------------------------------------------------- review

    @app_commands.describe(job="A job id, or nothing for every job awaiting review")
    async def cmd_review(self, interaction: discord.Interaction,
                         job: Optional[str] = None) -> None:
        """Fresh cards for reviews whose buttons died with the process.

        Rebuilt from the record saved when the review was posted and from the
        worktree on disk; Approve on one is the same strict path as the original.
        """
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        wanted = (job or "").strip()
        records = [record for record in review_record.load(self.settings)
                   if not wanted or str(record["job"].get("id", "")) == wanted]
        if not records:
            saved = review_record.load_one(self.settings, wanted) if wanted else None
            if saved is None:
                text = (f"No saved review for `{wanted}`; nothing to refresh."
                        if wanted else "No job is awaiting review.")
            else:
                state = str(saved.get("status", "")).replace("_", " ")
                text = f"`{wanted}` is not awaiting review ({state}); nothing to refresh."
            await interaction.followup.send(text, ephemeral=True)
            return
        posted: list[str] = []
        for record in records:
            rebuilt = review_record.outcome(record)
            channel = self._target_channel(rebuilt.job.channel_id) or interaction.channel
            if channel is None:
                continue
            try:
                await channel.send(
                    content=(f"Refreshed review for `{rebuilt.job.id}`: these buttons "
                             "work now; Approve still pushes only this branch."),
                    embed=review_embed(self.settings, rebuilt),
                    view=ReviewView(self, rebuilt, refreshed=True))
            except Exception:
                log.exception("could not refresh the review for job %s", rebuilt.job.id)
                continue
            posted.append(rebuilt.job.id)
        await interaction.followup.send(
            (f"Fresh card(s) for {', '.join(f'`{item}`' for item in posted)}."
             if posted else "The saved review could not be posted; the log says why."),
            ephemeral=True)

    # ------------------------------------------------------------- selftest

    async def cmd_selftest(self, interaction: discord.Interaction) -> None:
        """Every stage on fakes: no Freebuff, GitHub, repo write, push or queue."""
        if not await self.require_allowed(interaction):
            return
        stages = await selftest.run(self, dan_report_text)
        await interaction.response.send_message(selftest.report(stages), ephemeral=True)

    # ------------------------------------------------------------- questions

    @app_commands.describe(text="An option number from the question, or free text")
    async def cmd_answer(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, questions.MAX_ANSWER_CHARS] = "",
    ) -> None:
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

    # --------------------------------------------------------------- screens

    async def cmd_screen(self, interaction: discord.Interaction) -> None:
        """The running job's pane, or the screen it is waiting on. Read-only."""
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pane = await self.freebuff.current_pane()
        if pane is None:
            await interaction.followup.send(
                "No Freebuff session is running, so there is no screen to show.",
                ephemeral=True,
            )
            return
        waiting = self.freebuff.pending_screen
        header = (
            f"`{waiting.kind}` — Freebuff is waiting on {screens.describe(waiting.kind)}\n"
            if waiting is not None
            else "Freebuff is not waiting on a screen.\n"
        )
        body = tail_text(pane, max_lines=40, max_chars=1700).replace("```", "'''")
        await interaction.followup.send(
            f"{header}```\n{body}\n```\n", ephemeral=True
        )

    @app_commands.describe(name="One key: Enter, Esc, Up, Down, Left, Right, Tab, y or n")
    async def cmd_key(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 6000] = "",
    ) -> None:
        """One key from the fixed allowlist, only while a screen is waiting."""
        if not await self.require_allowed(interaction):
            return
        key = screens.key_sequence(name)
        if key is None:
            allowed = ", ".join(f"`{item}`" for item in screens.KEY_NAMES)
            await interaction.response.send_message(
                f"That is not one of the keys I may send. Use one of: {allowed}.",
                ephemeral=True,
            )
            return
        if not await self.freebuff.send_key(key):
            await interaction.response.send_message(
                "No Freebuff session is waiting on a screen, so no key was sent.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"Sent `{key}`.", ephemeral=True)

    @app_commands.describe(text="Text to paste into Freebuff and submit, like /task")
    async def cmd_send(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, screens.MAX_SEND_CHARS] = "",
    ) -> None:
        """Paste text into a waiting screen: load-buffer, paste-buffer, Enter."""
        if not await self.require_allowed(interaction):
            return
        # The cap is on what was typed, before it is flattened: a long message
        # is refused, never silently cut to something else.
        raw = " ".join((text or "").split())
        if len(raw) > screens.MAX_SEND_CHARS:
            await interaction.response.send_message(
                f"That is {len(raw)} characters; `/send` takes at most "
                f"{screens.MAX_SEND_CHARS}.",
                ephemeral=True,
            )
            return
        value = screens.flatten_send(raw)
        if not value:
            await interaction.response.send_message(
                "That text is empty, so nothing was sent.", ephemeral=True
            )
            return
        if not await self.freebuff.send_screen_text(value):
            await interaction.response.send_message(
                "No Freebuff session is waiting on a screen, so nothing was typed.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Typed into Freebuff: `{screens.redact_pane(value)[:100]}`", ephemeral=True
        )

    async def cmd_board(self, interaction: discord.Interaction) -> None:
        """Re-post the running job's board, for when the message was lost."""
        if not await self.require_allowed(interaction):
            return
        running = getattr(self.queue, "running", None)
        publisher = getattr(self, "_board", None)
        if running is None or publisher is None:
            await interaction.response.send_message(
                "No job is running, so there is no board to post.", ephemeral=True
            )
            return
        posted = False
        try:
            posted = await publisher.repost(running.id)
        except Exception:
            log.exception("could not repost the board for job %s", running.id)
        if posted:
            await interaction.response.send_message(
                f"Posted a fresh board for `{running.id}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No board is tracked for the running job yet; try again in a moment.",
                ephemeral=True,
            )

    @app_commands.describe(
        job_id="Job id of the approved review; empty uses the newest",
        dry_run="Check the gates and remember the PR head without merging (default)",
    )
    async def cmd_merge(self, interaction: discord.Interaction, job_id: Optional[str] = None,
                        dry_run: bool = True) -> None:
        """The only merge path: gated, dry run unless it is turned off."""
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not os.environ.get(github_client.TOKEN_ENV, "").strip():
            await interaction.followup.send(
                f"{github_client.TOKEN_ENV} is not set, so nothing was checked or merged."
            )
            return
        job = self._merge_job(job_id)
        if job is None:
            await interaction.followup.send(
                "There is no approved review to gate; pass its job id. Nothing merged.")
            return
        try:
            outcome = await merge_command.run_merge(job, self.settings, dry_run=dry_run)
        except github_client.GitHubError as exc:
            await interaction.followup.send(f"GitHub: {exc}. Nothing was merged.")
            return
        job.merge_dry_run_sha = outcome.dry_run_sha or job.merge_dry_run_sha
        await interaction.followup.send(outcome.text[:1900])

    def _remember_merge_job(self, job: Job) -> None:
        """Keep a reviewed job so /merge can read what it recorded."""
        registry = getattr(self, "_merge_jobs", None)
        if registry is None:
            return
        registry.pop(job.id, None)
        registry[job.id] = job
        while len(registry) > jobs.MAX_TRACKED_REVIEWS:
            registry.pop(next(iter(registry)))

    def _merge_job(self, job_id: str | None) -> Job | None:
        """A reviewed job by id, or the newest one when no id is given."""
        registry, target = getattr(self, "_merge_jobs", None) or {}, (job_id or "").strip()
        value = target[:-4] if target.endswith(".log") else target
        if not value:
            return next(reversed(registry.values()), None) if registry else None
        return registry.get(value) if LOG_ID_RE.match(value) else None

    # ----------------------------------------------------------- ai channel

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if await self._take_roadmap_sheet(message):
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
            "without asking the model; failing checks force revise; a merge only "
            "happens through the gated /merge, which is a dry run unless asked otherwise.",
            self.runner.last_job_context(),
        ]
        return "\n".join(part for part in parts if part)

    # ------------------------------------------------------------ reporting

    async def _roadmap_apply(self, sheet: discord.Attachment, mode: str,
                             channel_id: int, requester_id: int) -> str:
        """The one path for a sheet: read it, then preview it or queue its first job.

        Every reply goes out through _clip_reply, a last net under the preview's
        own limit, so no /roadmap text can hit Discord's 2000-character ceiling.
        """
        try:
            data = await sheet.read()
        except discord.HTTPException:
            return "I could not read that attachment; post the file again."
        try:
            steps = roadmap.parse(data.decode("utf-8", "replace"))
        except roadmap.RoadmapError as exc:
            return _clip_reply(f"Not {'previewed' if mode == 'preview' else 'queued'}: {exc}.")
        registry = getattr(self.settings, "projects", None)
        if isinstance(registry, projects.Registry):
            unknown = sorted(
                {step.project for step in steps if registry.get(step.project) is None}
            )
            if unknown:
                return _clip_reply(
                    f"Not {'previewed' if mode == 'preview' else 'queued'}: unknown "
                    f"project {unknown[0]!r}. Known: {', '.join(registry.ids())}.")
        if mode == "draft":
            return await sheet_card.replace_draft(self, steps, channel_id, requester_id)
        if mode == "preview":
            return _clip_reply(roadmap.preview(steps))
        return await self._roadmap_start(steps, channel_id, requester_id)

    async def _roadmap_start(self, steps: list[roadmap.Step], channel_id: int,
                             requester_id: int) -> str:
        """Start a parsed sheet: ONE first job, the roadmap saved, nothing else."""
        if self._update_waiting is not None:
            return _clip_reply(self._update_refusal())
        self._roadmap_steps = steps
        self._roadmap_channel_id = channel_id
        self._roadmap_requester_id = requester_id
        self._roadmap_paused = False  # a new sheet starts running
        job = await self._queue_roadmap_step(steps[0])
        if job is None:
            self._roadmap_steps = []
            return "The queue is full; nothing was queued."
        self._roadmap_save()
        return _clip_reply(
            f"Roadmap queued: {len(steps)} job(s), first is `{job.id}` ({steps[0].kind}). "
            "Each still waits for your Approve; nothing is pushed or merged.")

    async def _take_roadmap_sheet(self, message: discord.Message) -> bool:
        """True when this message was the sheet a /roadmap is waiting for.

        Only its requester, only in the channel it was asked in, and only before it
        expires; a message without a .md file leaves the wait running.
        """
        waiting = self._roadmap_waiting.get(message.author.id)
        if waiting is None:
            return False
        channel_id, mode, expires = waiting
        if message.channel.id != channel_id:
            return False
        del self._roadmap_waiting[message.author.id]
        if time.time() >= expires:
            log.info("the waiting roadmap sheet from %s expired", message.author.id)
            return False
        sheet = next((item for item in message.attachments
                      if item.filename.lower().endswith(".md")), None)
        if sheet is None:
            self._roadmap_waiting[message.author.id] = waiting
            return False
        await message.reply(await self._roadmap_apply(sheet, mode, channel_id, message.author.id))
        return True

    def _roadmap_path(self) -> Path:
        return Path(self.settings.log_dir) / "roadmap.json"

    def _roadmap_save(self) -> None:
        """Save the roadmap with the job it is following, so a restart has a place.

        Every write goes through here: the current job id must be on disk even
        while the roadmap is paused and the tick is doing nothing at all.
        """
        roadmap.save(self._roadmap_path(), self._roadmap_steps,
                     channel_id=self._roadmap_channel_id,
                     requester_id=self._roadmap_requester_id,
                     paused=self._roadmap_paused,
                     current=getattr(self._roadmap_job, "id", "") or "")

    def _roadmap_restore_current(self) -> Job | None:
        """After a restart: the job the saved roadmap names, rebuilt from its review.

        The saved review is the truth about it: ``awaiting_review`` restores the
        reminder, ``approved`` restores the merge check. Anything else is left
        alone, so a stopped roadmap still stops. It is restored while paused too:
        a pause holds back the tick, not the roadmap's place.
        """
        job_id = roadmap.current(self._roadmap_path())
        if not job_id or not any(step.job_id == job_id for step in self._roadmap_steps):
            return None
        record = review_record.load_one(self.settings, job_id)
        if record is None:
            return None
        if str(record.get("status") or "") not in (review_record.AWAITING, "approved"):
            return None
        try:
            job = review_record.outcome(record).job
        except Exception:  # a record that will not rebuild must not stop a start
            log.exception("could not rebuild the roadmap job %s from its review", job_id)
            return None
        self._roadmap_jobs[job.id] = job
        log.info("roadmap job %s restored after a restart (%s)", job.id, job.status)
        return job

    def _sheet_draft_path(self) -> Path:
        return Path(self.settings.log_dir) / sheet_card.DRAFT_FILE

    async def _restore_sheet_draft(self) -> None:
        await sheet_card.post(self, self._target_channel(self._sheet_draft.channel_id),
                              note="Restored after a restart; nothing is queued.")

    def _roadmap_left(self) -> int:
        """Jobs the sheet never queued: the only ones a pause holds back."""
        return len([step for step in self._roadmap_steps if not step.job_id])

    def _roadmap_set_paused(self, paused: bool) -> str:
        """Pause or resume queueing; the queued or running job is never touched."""
        self._roadmap_paused = paused
        self._roadmap_save()
        if not self._roadmap_steps:
            return f"{'Roadmap paused.' if paused else 'Roadmap resumed.'} No roadmap is queued."
        if paused:
            return (f"Roadmap paused, {self._roadmap_left()} job(s) left. No next job is "
                    "queued until `/roadmap action:resume`; the queued or running job is "
                    "untouched.")
        return (f"Roadmap resumed; {self._roadmap_left()} job(s) left. "
                "The next tick queues the next job as normal.")

    def _roadmap_last_queued(self) -> str:
        """The id of the newest job the sheet queued, or "" when none was."""
        return next((step.job_id for step in reversed(self._roadmap_steps)
                     if step.job_id), "")

    def _roadmap_current_step(self) -> roadmap.Step | None:
        """The step the roadmap is on: its current job's, else the newest queued."""
        ids = [getattr(self._roadmap_job, "id", "") or "", self._roadmap_last_queued()]
        for job_id in ids:
            if not job_id:
                continue
            step = next((item for item in self._roadmap_steps
                         if item.job_id == job_id), None)
            if step is not None:
                return step
        return None

    async def _roadmap_stuck_notice(self) -> None:
        """Say once, plainly, that the roadmap has nowhere to go, and what to do.

        A pause before a restart used to leave the roadmap with no current job,
        and the tick then did nothing at all: nothing was posted, so it looked as
        if the roadmap had just stopped. The notice names the newest queued job
        and the three ways out; a new job, or a skip, starts it again.
        """
        if self._roadmap_paused or not self._roadmap_left():
            return
        last = self._roadmap_last_queued()
        key = f"{last}:{len(self._roadmap_steps)}"
        if getattr(self, "_roadmap_stuck", "") == key:
            return
        self._roadmap_stuck = key
        await self._roadmap_say(
            f"Roadmap is stuck: {self._roadmap_left()} job(s) left, but I no longer "
            f"follow the job `{last or 'none'}` I last queued.\n"
            "`/roadmap action:skip` marks it done without Approve and queues the "
            "next job, `/roadmap action:stop` drops the rest, and `/review` shows "
            "a saved card if one is still waiting.")

    async def _roadmap_skip(self) -> str:
        """Mark the current step done without Approve, then queue the next one.

        Only for a roadmap that is paused or stuck with no current job: a running
        one steps on its own status, and skipping it would hide a real failure.
        The skipped job is never pushed or merged; the reply says so outright.
        """
        if not self._roadmap_steps:
            return "No roadmap is queued."
        current = self._roadmap_job
        if current is not None and not self._roadmap_paused:
            return (f"`{current.id}` is still the roadmap's current job; skip needs "
                    "the roadmap paused (`/roadmap action:pause`), or stuck with no "
                    "current job.")
        step = self._roadmap_current_step()
        if step is None:
            return ("Nothing to skip: no job of this roadmap is recorded as current. "
                    "`/roadmap action:stop` drops it.")
        skipped = step.job_id
        self._roadmap_steps = [item for item in self._roadmap_steps if item is not step]
        self._roadmap_jobs.pop(skipped, None)
        self._roadmap_reminder.pop(skipped, None)
        if (self._roadmap_merge or {}).get("job_id") == skipped:
            self._roadmap_merge = None
            self._roadmap_merge_save()
        if current is not None and current.id == skipped:
            self._roadmap_job = None
        self._roadmap_stuck = ""
        nxt = next((item for item in self._roadmap_steps if not item.job_id), None)
        if nxt is None:
            await self._roadmap_finish(f"Skipped `{skipped}` without Approve; nothing "
                                       "was pushed or merged.")
            return f"Skipped `{skipped}` without Approve; no job(s) left, roadmap done."
        job = await self._queue_roadmap_step(nxt)
        self._roadmap_save()
        if job is None:
            return (f"Skipped `{skipped}` without Approve, but the queue is full, so "
                    "nothing new was queued.")
        return (f"Skipped `{skipped}` without Approve — it was never pushed or merged. "
                f"Queued `{job.id}` ({nxt.kind}) next; it still waits for Approve. "
                f"{self._roadmap_left()} job(s) left after it.")

    async def _queue_roadmap_step(self, step: roadmap.Step) -> Job | None:
        """Queue one sheet job exactly as /task or /dan would, then record it."""
        job = jobs.make_job(self.settings, kind=step.kind, title=step.text[:100],
                            channel_id=self._roadmap_channel_id, text=step.text,
                            requester_id=self._roadmap_requester_id,
                            project=step.project)
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            log.warning("roadmap job not queued: %s", exc)
            return None
        step.job_id = job.id
        self._roadmap_job = job
        self._roadmap_jobs[job.id] = job
        self._roadmap_stuck = ""  # a new job is a new place: the stall is over
        await self._track_board(job, position)
        return job

    async def _roadmap_tick(self) -> None:
        """Queue the next job only after the current one is approved and merged.

        Approve only pushes, so an approved job waits for its branch to reach the
        authorized branch; the next job queues when the merge is seen. Paused, it
        does nothing at all: nothing is queued, pinged or stopped. A job already
        queued or running keeps going and still waits for Approve.
        """
        if self._roadmap_paused or self._update_waiting is not None:
            return
        if not self._roadmap_job and self._roadmap_steps:
            self._roadmap_job = self._roadmap_adopt() or self._roadmap_merge_job()
        current = self._roadmap_job
        if not self._roadmap_steps:
            return
        if current is None:
            await self._roadmap_stuck_notice()
            return
        if current.status in roadmap.STOPPED_STATUSES:
            await self._roadmap_finish(
                f"Roadmap stopped: `{current.id}` {current.status}. Nothing else was queued.")
        elif current.status == "merged":
            await self._roadmap_next_step(current)
        elif current.status == "approved":
            await self._roadmap_wait_merge(current)
        elif current.status == "awaiting_review":
            await self._roadmap_remind(current)

    async def _roadmap_next_step(self, job: Job) -> None:
        """Queue the next sheet job, or finish the roadmap when there is none."""
        step = next((item for item in self._roadmap_steps if not item.job_id), None)
        if step is None:
            await self._roadmap_finish("roadmap done")
        elif await self._queue_roadmap_step(step) is not None:
            self._roadmap_save()

    async def _roadmap_wait_merge(self, job: Job) -> None:
        """Hold the next job until this one's branch is in the authorized branch.

        Approve only pushes; merging is the owner's. So the roadmap says once
        that it is waiting (with the compare link), re-pings every
        ROADMAP_PING_SECONDS, and queues the next job only once the branch tip
        is an ancestor of ``origin/<base>``. That job fetches the base before it
        cuts its own branch, so it starts with the merge already in it.
        """
        branch, base = self._roadmap_merge_target(job)
        if not branch:
            log.warning("roadmap job %s has no branch; not waiting for a merge", job.id)
            await self._roadmap_next_step(job)
            return
        merged = await self._roadmap_merged(branch, base, job.kind)
        wait = self._roadmap_merge
        if not merged:  # False, or None when the check could not run this tick
            now = time.time()
            if wait is None or wait.get("job_id") != job.id:
                self._roadmap_merge = {
                    "job_id": job.id, "branch": branch, "base": base,
                    "kind": job.kind, "since": now,
                    "due": now + ROADMAP_PING_SECONDS,
                }
                self._roadmap_merge_save()
                await self._roadmap_say(self._roadmap_merge_text(branch, base, job.kind))
            elif now >= float(wait.get("due") or 0.0):
                wait["due"] = now + ROADMAP_PING_SECONDS
                self._roadmap_merge_save()
                await self._roadmap_say(self._roadmap_merge_text(branch, base, job.kind,
                                                                first=False))
            return
        job.status = "merged"
        self._roadmap_merge = None
        self._roadmap_merge_save()
        await self._roadmap_say(
            f"Merged: `{branch}` is in `{base}`. Queuing the next job, which is "
            f"cut from the fetched `{base}`.")
        await self._roadmap_next_step(job)

    def _roadmap_merge_target(self, job: Job) -> tuple[str, str]:
        """The branch Approve pushed and the base it has to land in."""
        record = review_record.load_one(self.settings, job.id) or {}
        env = record.get("env") or {}
        branch = str(env.get("branch") or job.branch or "")
        base = str(env.get("base_branch") or "")
        if base.startswith("origin/"):
            base = base[len("origin/"):]
        return branch, base or self._roadmap_base(job.kind)

    def _roadmap_base(self, kind: str) -> str:
        """Where a job's work lands: main for a /dan, else its project's branch."""
        if kind == "dan":
            return "main"
        registry = getattr(self.settings, "projects", None)
        if isinstance(registry, projects.Registry):
            job = getattr(self, "_roadmap_job", None)
            project = registry.get(getattr(job, "project", ""))
            if project is not None:
                return project.authorized_branch
        return str(getattr(self.settings, "authorized_branch", "") or "")

    def _roadmap_repo(self, kind: str):
        """The checkout a roadmap job's branch lives in: her own, or its project's."""
        if kind == "dan":
            return self.freebuff.self_repo
        job = getattr(self, "_roadmap_job", None)
        project_id = str(getattr(job, "project", "") or "")
        registry = getattr(self.settings, "projects", None)
        resolver = getattr(self.freebuff, "repo_for_project", None)
        if (
            project_id
            and project_id != projects.DEFAULT_PROJECT
            and callable(resolver)
            and isinstance(registry, projects.Registry)
            and registry.get(project_id) is not None
        ):
            return resolver(project_id)
        return self.freebuff.repo

    async def _roadmap_merged(self, branch: str, base: str, kind: str) -> bool | None:
        """Is ``origin/<branch>`` already in ``origin/<base>``? Fetch only.

        Read-only: a fetch moves remote-tracking refs and nothing else, and no
        branch, worktree or origin is written. None means the question could not
        be asked this tick (no remote, a failed fetch); that is not a merge, so
        nothing is queued and the next tick tries again.
        """
        if not branch or not base:
            return None
        repo = self._roadmap_repo(kind)
        try:
            await repo.fetch_branch(base)
            await repo.fetch_branch(branch)
        except FreebuffError as exc:
            log.warning("roadmap merge check could not fetch: %s", exc)
            return None
        probe = await repo._run(
            ["merge-base", "--is-ancestor", f"origin/{branch}", f"origin/{base}"],
            check=False,
        )
        if probe.code == 0:
            return True
        return False if probe.code == 1 else None

    def _roadmap_merge_text(self, branch: str, base: str, kind: str, *,
                            first: bool = True) -> str:
        """The one waiting message and its six-hour re-ping, with the compare link."""
        link = self._roadmap_merge_link(branch, base, kind)
        head = (f"Approved. Roadmap waiting for the merge of `{branch}` into `{base}` "
                "before the next job is queued." if first
                else f"Still waiting for the merge of `{branch}` into `{base}`.")
        return head + "\n" + (f"Open and merge: {link}" if link
                              else "No compare link available")

    def _roadmap_merge_link(self, branch: str, base: str, kind: str) -> str | None:
        """The GitHub compare link for the waiting branch, when it can exist."""
        project = getattr(self.settings, "project", None)
        if project is None:
            return None
        job = getattr(self, "_roadmap_job", None)
        remote = projects.remote_for(
            self.settings, getattr(job, "project", ""), dan=kind == "dan"
        )
        if not remote:
            return None
        return compare_link.compare_url(remote, base, branch)

    def _roadmap_merge_path(self) -> Path:
        return Path(self.settings.log_dir) / ROADMAP_MERGE_FILE

    def _roadmap_merge_load(self) -> dict | None:
        """The merge the roadmap was waiting for before the last restart."""
        try:
            data = json.loads(self._roadmap_merge_path().read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("job_id") else None

    def _roadmap_merge_save(self) -> None:
        """Keep the wait across a restart, or forget it once the merge is seen."""
        path = self._roadmap_merge_path()
        try:
            if self._roadmap_merge is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(self._roadmap_merge, indent=2), "utf-8")
        except OSError:
            log.exception("could not save the roadmap merge wait")

    def _roadmap_merge_job(self) -> Job | None:
        """After a restart: the approved job whose merge the roadmap waits on."""
        wait = self._roadmap_merge or {}
        step = next((item for item in self._roadmap_steps
                     if item.job_id and item.job_id == wait.get("job_id")), None)
        if step is None or not wait.get("branch"):
            return None
        job = Job(id=step.job_id, kind=step.kind, title=step.text[:100],
                  channel_id=self._roadmap_channel_id,
                  requester_id=self._roadmap_requester_id,
                  log_path=Path(self.settings.log_dir) / f"{step.job_id}.log",
                  text=step.text, branch=str(wait["branch"]))
        job.status = "approved"
        self._roadmap_jobs[job.id] = job
        return job

    async def _roadmap_say(self, text: str) -> None:
        """One line where the roadmap was started; a lost channel is not fatal."""
        who = f"<@{self._roadmap_requester_id}> " if self._roadmap_requester_id else ""
        try:
            channel = self._target_channel(self._roadmap_channel_id)
            if channel is not None:
                await channel.send(who + text)
        except Exception:
            log.exception("could not post a roadmap update")

    def _roadmap_adopt(self) -> Job | None:
        """After a restart: the roadmap's own job, if the queue still has it."""
        ids = {step.job_id for step in self._roadmap_steps if step.job_id}
        running = getattr(self.queue, "running", None)
        running = running() if callable(running) else running
        for job in [running, *self.queue.waiting()]:
            if job is not None and job.id in ids:
                return job
        return None

    async def _roadmap_remind(self, job: Job) -> None:
        """One ping every ROADMAP_PING_SECONDS while the review waits on Approve."""
        now = time.time()
        due = self._roadmap_reminder.setdefault(job.id, now + ROADMAP_PING_SECONDS)
        if now < due:
            return
        self._roadmap_reminder[job.id] = now + ROADMAP_PING_SECONDS
        channel = self._target_channel(self._roadmap_channel_id)
        try:
            if channel is not None:
                await channel.send(
                    f"Roadmap waiting on your Approve: `{job.id}`\n"
                    "Buttons not working? Run /review")
        except Exception:
            log.exception("could not post the roadmap reminder")

    def _roadmap_status(self) -> str:
        """One line per sheet job: its number, kind, job id and recorded state."""
        if not self._roadmap_steps:
            return "No roadmap is queued."
        waiting = (self._roadmap_merge or {}).get("job_id")
        rows = []
        for number, step in enumerate(self._roadmap_steps, start=1):
            job = self._roadmap_jobs.get(step.job_id)
            state = job.status if job else ("queued" if step.job_id else "waiting")
            if step.job_id and step.job_id == waiting:
                state = "approved, waiting for merge"
            rows.append(f"{number}. {step.kind} · `{step.job_id or 'not queued'}` · {state}")
        header = f"Roadmap: {len(rows)} jobs"
        if self._roadmap_paused:
            header += f"; paused, {self._roadmap_left()} job(s) left"
        return "\n".join([header, *rows])

    def _roadmap_stop(self) -> str:
        """Drop the jobs never queued; the one already in line is untouched."""
        dropped = len([step for step in self._roadmap_steps if not step.job_id])
        self._roadmap_steps, self._roadmap_job = [], None
        self._roadmap_jobs.clear()
        self._roadmap_reminder.clear()
        self._roadmap_merge = None
        self._roadmap_merge_save()
        self._roadmap_paused = False
        roadmap.save(self._roadmap_path(), [])
        if not dropped:
            return "No roadmap is queued."
        return (f"Stopped the roadmap; {dropped} job(s) not yet queued were dropped; "
                "the queued job is unchanged and still waits for Approve.")

    async def _roadmap_finish(self, text: str) -> None:
        """End the roadmap and say why, once, where it was started."""
        self._roadmap_steps, self._roadmap_job = [], None
        self._roadmap_jobs.clear()
        self._roadmap_reminder.clear()
        self._roadmap_merge = None
        self._roadmap_merge_save()
        self._roadmap_paused = False
        roadmap.save(self._roadmap_path(), [])
        await self._roadmap_say(text)

    def _board_channel_id(self, job: Job) -> int:
        """Boards live in NOTIFY_CHANNEL_ID, else where the job was started."""
        return getattr(self.settings, "notify_channel_id", None) or job.channel_id

    async def _track_board(self, job: Job, position: int) -> None:
        """Start the live board for a builder job, then post it once."""
        if job.kind not in ("task", "dan"):
            return
        publisher = getattr(self, "_board", None)
        if publisher is None:  # a bot without a board still queues jobs
            return
        ctx = getattr(self.settings, "project", None)
        try:
            publisher.track(
                job.id,
                channel_id=self._board_channel_id(job),
                requester_id=job.requester_id,
                kind=job.kind,
                project=getattr(ctx, "project_name", "") or "",
                started_at=getattr(job, "started", None),
                queue_position=position,
            )
            await publisher.report(job.id, status_board.QUEUED, "queued")
        except Exception:  # a board must never stop a job being queued
            log.exception("could not post the board for job %s", job.id)

    def _on_stage(self, job: Job, stage: str, detail: str = "") -> None:
        """THE status hook, installed in JobRunner. Never raises, never waits.

        The board update runs as its own task: a slow Discord call must not add
        a millisecond to a job, and an exception here is only logged.
        """
        if job.kind not in ("task", "dan") or stage == status_board.QUEUED:
            return
        publisher = getattr(self, "_board", None)
        if publisher is None:
            return
        base = getattr(job, "base_label", "")
        if base:
            publisher.set_base(job.id, base)
        screen_kind = detail if stage == status_board.WAITING_ON_YOU else ""
        try:
            task = asyncio.create_task(
                publisher.report(
                    job.id,
                    stage,
                    "" if screen_kind else detail,
                    screen_kind=screen_kind,
                    started_at=job.started,
                )
            )
        except Exception:
            log.exception("could not schedule a board update for job %s", job.id)
            return
        self._board_tasks.add(task)
        task.add_done_callback(self._board_task_done)

    def _board_task_done(self, task: "asyncio.Task[None]") -> None:
        self._board_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return
        if error is not None:
            log.warning("a board update failed: %s", error)

    async def _board_loop(self) -> None:
        """Keep queue positions honest and ping a stage that has not moved."""
        while True:
            try:
                await asyncio.sleep(BOARD_TICK_SECONDS)
                ids = [job.id for job in self.queue.waiting()]
                await self._board.tick(queue_ids=ids)
                try:
                    await self._roadmap_tick()
                except Exception:  # the roadmap must never stop the board
                    log.exception("roadmap tick failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # the board must never stop the bot
                log.exception("status board tick failed")

    async def _heartbeat_loop(self) -> None:
        """Every 20s: read the pane and say whether the job is really moving."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_TICK_SECONDS)
                await self._heartbeat_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # the heartbeat must never stop the bot
                log.exception("heartbeat tick failed")

    async def _heartbeat_tick(self) -> None:
        """One capture of the working job's pane, then what it proves."""
        board = None
        for job_id in self._board.tracked_ids():
            candidate = self._board.board_for(job_id)
            if candidate is not None and candidate.stage in heartbeat.WORKING_STAGES:
                board = candidate
                break
        if board is None:
            return
        pane = await self.freebuff.current_pane()
        if not pane:
            return
        channel_id = self._board.channel_for(board.job_id)
        for action in self._heartbeat.tick(board, pane):
            if action.kind == heartbeat.ACTIVITY:
                await self._board.set_activity(board.job_id, action.text)
                continue
            if not channel_id:
                continue
            try:
                await self._board.transport.send(channel_id, action.text)
            except Exception:  # a message must never touch the job
                log.warning("could not post a heartbeat for %s", board.job_id)

    async def _update_loop(self) -> None:
        """UPDATE_CHECK_SECONDS: read-only git in a thread, then the notices.

        A failed check or post is logged and never stops the bot, and nothing
        here changes a branch or a worktree.
        """
        while True:
            if self._update_waiting is not None:
                try:
                    await self._update_wait_tick()
                except Exception:
                    log.exception("the update wait failed; still waiting")
                await asyncio.sleep(UPDATE_TICK_SECONDS)
                continue
            try:
                notices = await asyncio.to_thread(self._update_watcher.check)
                skipped = getattr(self, "_skipped_tip", "")
                for notice in notices:
                    if getattr(notice, "kind", "") == "self" and notice.tip == skipped:
                        continue
                    channel_id = getattr(self.settings, "notify_channel_id", None)
                    channel = self._target_channel(channel_id) if channel_id else None
                    if channel is None:
                        log.warning("no NOTIFY_CHANNEL_ID for an update notice")
                        continue
                    offer = UpdateNoticeView(self, notice.tip) if getattr(notice, "kind", "") == "self" else None
                    await channel.send(str(notice), view=offer)
            except asyncio.CancelledError:
                raise
            except Exception:  # an update notice must never stop the bot
                log.exception("update check failed")
            await asyncio.sleep(max(1.0, self._update_watcher.interval))

    async def start_update_wait(self, interaction: discord.Interaction, tip: str) -> None:
        """Offer accepted: wait for idle, then verify, pull and exit for launchd."""
        if not getattr(self, "_supervised", False):
            await interaction.response.send_message(supervision.manual_restart(), ephemeral=True)
            return
        reason = self._busy_reason() or "nothing is running, so it starts now"
        self._update_waiting = {"tip": tip, "channel_id": interaction.channel_id or 0,
                                "due": time.time() + UPDATE_PING_SECONDS}
        await interaction.response.send_message(
            f"Waiting to update to `{tip[:7]}`: {reason}. New jobs are paused; I check "
            "every 30s and ping hourly while work is running.")

    async def _update_wait_tick(self) -> None:
        wait = self._update_waiting
        if wait is None:
            return
        reason = self._busy_reason()
        if reason:
            if time.time() >= float(wait["due"]):
                wait["due"] = time.time() + UPDATE_PING_SECONDS
                await self._update_say(
                    f"Still waiting to update to `{str(wait['tip'])[:7]}`: {reason}.", wait)
            return
        await self._apply_update(wait)

    def _busy_reason(self) -> str:
        """Why an update must wait: a running job (even on a question) or a queue."""
        running = self.queue.running
        if running is not None:
            asked = " and waiting on a question" if self.freebuff.pending_question else ""
            return f"`{running.id}` is still {running.status}{asked}"
        waiting = self.queue.pending_count
        return f"{waiting} job(s) are still queued" if waiting else ""

    async def _update_say(self, text: str, wait: dict) -> None:
        channel = self._target_channel(int(wait.get("channel_id") or 0))
        if channel is not None:
            await channel.send(_clip_reply(text))

    async def _apply_update(self, wait: dict) -> None:
        """Verify the new code in a worktree, pull only if it passes, then exit."""
        tip, supervisor = str(wait["tip"]), self.supervisor
        old, reason = supervisor.head(), supervisor.refusal(tip)
        if reason:
            self._update_waiting = None
            return await self._update_say(f"Not updating: {reason}. Nothing was pulled.", wait)
        ok, tail = await asyncio.to_thread(supervisor.verify, tip)
        if not ok:
            self._update_waiting = None
            return await self._update_say(f"Staying on `{old[:7]}`: the suite failed on "
                                          f"`{tip[:7]}`.\n```\n{tail[-1400:]}\n```", wait)
        if self._busy_reason():
            return  # work started while the suite ran; the next tick waits again
        pulled, detail = await asyncio.to_thread(supervisor.pull)
        if not pulled:
            self._update_waiting = None
            return await self._update_say(f"Staying on `{old[:7]}`: pull failed: {detail}", wait)
        new = supervisor.head()
        supervisor.note_applied(old, new, supervisor.subjects(old, new), wait["channel_id"])
        self._update_waiting = None
        await self._update_say(f"Restarting at `{new[:7]}` from `{old[:7]}`.", wait)
        await self._exit_for_restart()

    async def _exit_for_restart(self) -> None:
        """Exit 0 so launchd's KeepAlive starts the new code; only when idle."""
        await self.close()
        os._exit(0)

    def _update_refusal(self) -> str:
        wait = self._update_waiting
        if wait is None:
            return ""
        return (f"An update to `{str(wait.get('tip'))[:7]}` is waiting to apply, so I am "
                "not taking new jobs; nothing is queued until it lands.")

    async def _repost_reviews(self, channel: discord.abc.Messageable) -> list[str]:
        """Fresh cards for saved reviews still awaiting a decision; same as /review."""
        posted: list[str] = []
        for record in review_record.load(self.settings):
            rebuilt = review_record.outcome(record)
            try:
                await (self._target_channel(rebuilt.job.channel_id) or channel).send(
                    content=f"Refreshed review for `{rebuilt.job.id}`: these buttons work now; Approve still pushes only this branch.",
                    embed=review_embed(self.settings, rebuilt),
                    view=ReviewView(self, rebuilt, refreshed=True))
            except Exception:
                log.exception("could not refresh the review for job %s", rebuilt.job.id)
                continue
            posted.append(rebuilt.job.id)
        return posted

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

    async def _post_screen(self, job: Job, screen: screens.Screen, tail: str) -> bool:
        """A screen needs a person: show the pane and what may be sent to it."""
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for the screen in job %s", job.id)
            return False
        pane = (tail or "(the pane was empty)").replace("```", "'''")[-1600:]
        embed = discord.Embed(
            title=f"🖥️ Freebuff is waiting on {screens.describe(screen.kind)}",
            description=(
                "The worktree and the pane both stopped changing, so this is not "
                "progress. The idle timer and the hard timeout are paused while you "
                "deal with it.\n\n"
                "`/screen` shows the pane again, `/key <name>` sends one key "
                "(Enter, Esc, Up, Down, Left, Right, Tab, y, n), and `/send <text>` "
                "pastes text and submits it, the same way `/task` does.\n\n```\n"
                + pane
                + "\n```"
            )[:4000],
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Job", value=f"`{job.id}`", inline=True)
        embed.add_field(name="Kind", value=screen.kind, inline=True)
        embed.add_field(name="Log", value=f"`{job.log_path.name}`", inline=True)
        embed.set_footer(
            text=(
                "If it does not change within QUESTION_WAIT_SECONDS it is dismissed "
                "with Esc and the job fails; nothing is guessed."
            )
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("could not post the screen for job %s", job.id)
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
        # /build and /check name the branch and commit they ran, so a green card
        # can never be read as "the newest work" by mistake.
        if job.branch:
            embed.add_field(name="Branch", value=f"`{job.branch}`", inline=True)
        if job.commit_label:
            embed.add_field(name="Commit", value=f"`{job.commit_label[:100]}`", inline=True)
        if job.elapsed_label:
            embed.add_field(name="Took", value=job.elapsed_label, inline=True)
        elif job.duration is not None:
            embed.add_field(name="Took", value=f"{job.duration:.0f}s", inline=True)
        await channel.send(embed=embed)

    # ----------------------------------------------------------------- audit

    @app_commands.describe(branch="Audit only this branch; empty audits the base and unmerged reviews")
    async def cmd_audit(
        self, interaction: discord.Interaction, branch: Optional[str] = None
    ) -> None:
        """Queue one read-only audit; it waits behind any running Freebuff job."""
        if not await self.require_allowed(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            requested = jobs.resolve_branch(self.settings, branch) if (branch or "").strip() else ""
            picked = await self._audit_targets(requested)
        except (ValueError, FreebuffError) as exc:
            await interaction.followup.send(f"Cannot audit that: {exc}")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job = jobs.make_job(
            self.settings, kind=audit.KIND, title=f"audit {len(picked)} branch(es)",
            channel_id=interaction.channel_id or 0, requester_id=interaction.user.id,
            text=json.dumps({"stamp": stamp, "targets": picked}))
        try:
            position = await self.queue.submit(job)
        except jobs.QueueFull as exc:
            await interaction.followup.send(str(exc))
            return
        names = ", ".join(f"`{name}`" for name in picked)
        await interaction.followup.send(
            f"Audit `{job.id}` queued (position {position}) for {names}. Check, build, test "
            f"and the parity drift run one after another in a detached worktree under "
            f"`audit-{stamp}`, which is removed again. Read-only: nothing is committed, "
            "pushed, merged or approved.")

    async def _audit_targets(self, requested: str) -> list[str]:
        """One requested branch, else the base and every review not merged into it.

        Read-only: the merge check the roadmap merge gate uses; a branch that
        cannot be fetched is audited anyway and its report says why.
        """
        base = str(self.settings.authorized_branch)
        reviewed = audit.reviewed(self.settings)
        if requested or not reviewed:
            return audit.targets(base, requested=requested)
        await self.freebuff.repo.fetch_branch(base)
        skip = {branch for branch, _status in reviewed
                if await self._roadmap_merged(branch, base, "task")}
        return audit.targets(base, reviewed=reviewed, skip=skip)

    async def _run_job(self, job: Job) -> None:
        """Audits report themselves; every other kind is the Freebuff runner's."""
        if job.kind == audit.KIND:
            await self._audit_run(job)
        else:
            await self.runner.run(job)

    async def _audit_run(self, job: Job) -> None:
        """One worktree per branch, the four stages, then one file and one message."""
        payload = json.loads(job.text or "{}")
        stamp = str(payload.get("stamp") or time.strftime("%Y%m%d-%H%M%S"))
        results = [await self._audit_one(str(name), stamp) for name in payload.get("targets") or []]
        await audit.add_causes(self.manager, results)
        base = str(self.settings.authorized_branch)
        report = audit.report(stamp, results, base)
        path = Path(self.settings.log_dir) / f"audit-{stamp}.md"
        try:
            path.write_text(report, "utf-8")
        except OSError as exc:
            job.summary = f"audit {stamp}: could not write the report: {exc}"
            return
        files = [discord.File(path, filename=path.name)]
        extra = "\nNo failures, so no fix sheet was generated."
        steps: list[roadmap.Step] = []
        sheet = audit.fix_sheet(stamp, results, base)
        if sheet:
            sheet_path = Path(self.settings.log_dir) / f"audit-{stamp}-sheet.md"
            try:
                sheet_path.write_text(sheet, "utf-8")
                steps = roadmap.parse(sheet)
            except (OSError, roadmap.RoadmapError) as exc:
                extra = f"\nThe fix sheet could not be posted: {exc}"
            else:
                files.append(discord.File(sheet_path, filename=sheet_path.name))
                extra = f"\nFix sheet: `{sheet_path.name}`, {len(steps)} job(s); nothing queued."
        job.summary = audit.summary(stamp, results, path.name) + extra
        channel = self._target_channel(job.channel_id)
        if channel is None:
            log.warning("no Discord channel for the audit in job %s", job.id)
            return
        await channel.send(job.summary, files=files)
        if not steps:
            return
        self._sheet_draft = sheet_card.Draft(steps, job.channel_id, job.requester_id)
        sheet_card.save(self._sheet_draft_path(), self._sheet_draft)
        await sheet_card.post(self, channel, note=f"From audit `{stamp}`.")

    async def _audit_one(self, branch: str, stamp: str) -> audit.TargetResult:
        """A detached worktree at origin/<branch>, the stages, then remove it."""
        repo = self.freebuff.repo
        worktree = Path(self.settings.worktree_root) / f"audit-{stamp}" / audit.safe_name(branch)
        try:
            await repo.fetch_branch(branch)
        except FreebuffError as exc:
            return audit.TargetResult(branch, error=f"could not fetch it: {exc}")
        added = await repo._run(
            ["worktree", "add", "--detach", str(worktree), f"origin/{branch}"], check=False)
        if added.code != 0:
            return audit.TargetResult(branch, error=f"no worktree: {added.stderr.strip()[:300]}")
        try:
            env = jobs.build_script_env(self.settings, worktree)
            stages = []
            for name, script, timeout in (
                ("check", "check.sh", self.settings.check_timeout_seconds),
                ("build", "build.sh", self.settings.build_timeout_seconds),
                ("test", "test.sh", self.settings.test_timeout_seconds),
            ):
                path = HERE / "scripts" / script
                stages.append(await asyncio.to_thread(
                    audit.run_stage, name, ["bash", str(path)], worktree, env,
                    float(timeout), path))
            drift = worktree / "ios-app/tools/parity/drift.mjs"
            stages.append(await asyncio.to_thread(
                audit.run_stage, "drift", ["node", "ios-app/tools/parity/drift.mjs"],
                worktree, env, 1800.0, drift))
            stat = await repo._run(
                ["diff", "--stat", f"origin/{self.settings.authorized_branch}...origin/{branch}"],
                check=False)
            return audit.TargetResult(
                branch, stages, diff_stat=stat.stdout.strip() if stat.code == 0 else "")
        finally:
            await repo._run(["worktree", "remove", "--force", str(worktree)], check=False)

    async def _on_job_crash(self, job: Job, exc: BaseException) -> None:
        channel = self._target_channel(job.channel_id)
        if channel is None:
            return
        await channel.send(
            f"❌ Job `{job.id}` crashed unexpectedly: `{type(exc).__name__}: {exc}`. "
            f"See `{job.log_path.name}`."
        )

    async def _post_report(self, channel: object, outcome: ReviewOutcome) -> None:
        """The job's report, then its test card: two plain messages, no embed."""
        for text in (dan_report_text(outcome), test_card.card(outcome.job.text)):
            try:
                await channel.send(text)
            except Exception:  # a report may never fail a review
                log.exception("could not post a report line for job %s", outcome.job.id)

    async def review_refusal(self, outcome: ReviewOutcome) -> str | None:
        """Why a refreshed card must not act, in plain words; None to go ahead."""
        record = review_record.load_one(self.settings, outcome.job.id)
        state = (record or {}).get("status", "missing")
        if state != review_record.AWAITING:
            said = ("was never saved" if state == "missing"
                    else f"was already {str(state).replace('_', ' ')}")
            return (f"`{outcome.job.id}` {said}, so this card has nothing to approve. "
                    "Nothing was pushed.")
        env = outcome.environment
        if env is None or not env.worktree.is_dir():
            return "The worktree is gone, so there is nothing to push. Nothing was pushed."
        # Read-only: a branch on origin is the only proof an Approve already ran.
        pushed = await self.freebuff._repo_for(env)._run(
            ["ls-remote", "--heads", "origin", env.branch], check=False)
        if pushed.code == 0 and pushed.stdout.strip():
            return (f"`{env.branch}` is already on origin, so there is nothing to push. "
                    "Run /merge to gate the merge.")
        try:
            changes = await self.freebuff.collect(env, int(self.settings.max_diff_lines))
        except FreebuffError as exc:
            return f"The worktree could not be re-read ({exc}); nothing was pushed."
        if changes.snapshot != outcome.snapshot:
            return ("The worktree changed since this card was shown, so it is not what "
                    "would be pushed. Nothing was pushed. Run /review again.")
        if sorted(changes.changed_paths) != sorted(outcome.changed_files):
            return ("The changed files differ from what this card showed. Nothing was "
                    "pushed. Run /review again.")
        return None

    def review_decided(self, job: Job, status: str) -> None:
        """Keep the decision, and let the roadmap advance on a refreshed Approve."""
        review_record.mark(self.settings, job.id, status)
        job.status = status
        if status != "approved":
            return
        if any(step.job_id == job.id for step in getattr(self, "_roadmap_steps", [])):
            # This card is the Approve the roadmap was waiting for: the next tick
            # queues the next step exactly as an original Approve would, and a
            # paused roadmap still holds it back. The place is saved here and not
            # only by the tick, so a pause before a restart keeps it.
            self._roadmap_job = job
            self._roadmap_jobs[job.id] = job
            self._roadmap_save()

    async def _post_review(self, outcome: ReviewOutcome) -> None:
        # Save the card's facts before posting: a send that never lands or a
        # restart that loses the buttons is then recoverable with /review.
        if outcome.verdict != "blocked":
            review_record.save(self.settings, outcome)
        self._remember_merge_job(outcome.job)
        channel = self._target_channel(outcome.job.channel_id)
        if channel is None:
            log.warning("no Discord channel for review of %s", outcome.job.id)
            return
        await self._post_report(channel, outcome)
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

        embed = review_embed(self.settings, outcome)
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
