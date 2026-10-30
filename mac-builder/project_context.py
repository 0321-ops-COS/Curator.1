"""Which repo, branch and project Melisa is working on, including herself.

Melisa lives in ``mac-builder/`` inside one checkout and builds a target
checkout (``REPO_DIR``). Those can be different repositories, and confusing the
two is how a job ends up with nothing to build. This module keeps one plain
record of both, checks at startup that the target really is what the bot
expects, and renders the record for ``/project``, ``/status`` and the manager
prompts.

It has no Discord or HTTP dependency so it can be tested anywhere.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_FOLDER = "mac-builder"
# The file whose presence says "this checkout is the iOS project Melisa builds".
PROJECT_SPEC = "ios-app/project.yml"

_CREDENTIALS_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@")


class PreflightError(RuntimeError):
    """The target checkout is not something Melisa can build. One line."""


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def redact_remote(url: str) -> str:
    """Drop any user:token@ part of a remote URL before it is shown or sent."""
    return _CREDENTIALS_RE.sub(r"\g<scheme>", url.strip())


def remote_url(repo: Path) -> str:
    code, out = _git(repo, "config", "--get", "remote.origin.url")
    return redact_remote(out) if code == 0 and out else "(no origin remote)"


def toplevel(repo: Path) -> Path:
    code, out = _git(repo, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if code == 0 and out else repo.resolve()


def has_local_branch(repo: Path, branch: str) -> bool:
    code, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return code == 0


@dataclass(frozen=True)
class ProjectContext:
    """Facts about the job Melisa is doing and about Melisa herself."""

    bot_name: str
    project_name: str
    builder_name: str
    target_dir: Path
    target_remote: str
    authorized_branch: str
    self_dir: Path
    self_remote: str

    @property
    def target_is_self(self) -> bool:
        return self.target_dir.resolve() == self.self_dir.resolve()

    def summary(self) -> str:
        """Human text for /project."""
        lines = [
            f"{self.bot_name} is the repo manager for project '{self.project_name}'.",
            "",
            "Building (target):",
            f"  repo:   {self.target_remote}",
            f"  path:   {self.target_dir}",
            f"  branch: {self.authorized_branch} (jobs branch from it as freebuff/<id>)",
            f"  builder: {self.builder_name}",
            "",
            f"{self.bot_name} herself (this bot):",
            f"  repo:   {self.self_remote}",
            f"  folder: {self.self_dir / SELF_FOLDER}",
            f"  protected: edits under {SELF_FOLDER}/ are always rejected; "
            "change her through a normal branch and PR.",
        ]
        if self.target_is_self:
            lines += [
                "",
                "WARNING: the target is the same checkout as the bot, so there is "
                "no project to build here.",
            ]
        return "\n".join(lines)

    def prompt_block(self) -> str:
        """Compact text for the manager's system context."""
        same = " (same checkout as the bot: nothing to build)" if self.target_is_self else ""
        return (
            f"You are {self.bot_name}, the repo manager for project "
            f"'{self.project_name}'. The builder is {self.builder_name}.\n"
            f"Target repo: {self.target_remote}, base branch {self.authorized_branch}"
            f"{same}.\n"
            f"Your own code: {self.self_remote}, folder {SELF_FOLDER}/. "
            f"It is protected; you cannot edit yourself."
        )


def build_context(
    *,
    target_dir: Path,
    authorized_branch: str,
    self_dir: Path,
    project_name: str = "",
    builder_name: str = "Freebuff",
    bot_name: str = "Melisa",
) -> ProjectContext:
    target = toplevel(target_dir)
    return ProjectContext(
        bot_name=bot_name or "Melisa",
        project_name=project_name or target.name,
        builder_name=builder_name or "Freebuff",
        target_dir=target,
        target_remote=remote_url(target),
        authorized_branch=authorized_branch,
        self_dir=toplevel(self_dir),
        self_remote=remote_url(self_dir),
    )


def preflight(ctx: ProjectContext) -> None:
    """Raise PreflightError (one line) unless the target can be built."""
    spec = ctx.target_dir / PROJECT_SPEC
    if not spec.is_file():
        extra = " (it is the bot's own repo)" if ctx.target_is_self else ""
        raise PreflightError(
            f"REPO_DIR {ctx.target_dir} ({ctx.target_remote}) has no {PROJECT_SPEC}"
            f"{extra}; point REPO_DIR at the checkout of the iOS project."
        )
    if not has_local_branch(ctx.target_dir, ctx.authorized_branch):
        raise PreflightError(
            f"AUTHORIZED_BRANCH {ctx.authorized_branch!r} is not a local branch in "
            f"{ctx.target_dir}; fetch and switch to it first."
        )
