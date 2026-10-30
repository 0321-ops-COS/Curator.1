"""The local checkouts /status can report on.

The bot is configured with one target checkout (``REPO_DIR``), but the owner has
more repositories than that, and asking "how is the other one doing" should not
need a new deployment. This module lists the git checkouts directly under
``REPOS_ROOT`` (default: the home directory) and maps the name the owner types
onto one of them. It is read-only and filesystem-local, so it works without
Discord, a token or network access.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 20
# A home directory can hold a lot of folders; this is a status line, not a scan.
MAX_REPOS = 20


@dataclass(frozen=True)
class RepoStatus:
    """One local checkout, as much as /status needs to say about it."""

    name: str
    path: Path
    branch: str
    changed: int
    head: str

    def line(self) -> str:
        """One line for the picker: name, branch, dirty count, last commit."""
        dirty = f"{self.changed} changed" if self.changed else "clean"
        return f"{self.name}: {self.branch} ({dirty}) {self.head}"


def _git(path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_git_checkout(path: Path) -> bool:
    """True for a directory git can work in, including a linked worktree."""
    return (Path(path) / ".git").exists()


def describe(path: Path) -> RepoStatus:
    resolved = Path(path).expanduser().resolve()
    branch = _git(resolved, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changed = len(
        [line for line in _git(resolved, "status", "--porcelain").splitlines() if line.strip()]
    )
    head = _git(resolved, "log", "-1", "--format=%h %s") or "(no commits)"
    return RepoStatus(
        name=resolved.name,
        path=resolved,
        branch=branch,
        changed=changed,
        head=head[:90],
    )


def discover(root: Path, *, limit: int = MAX_REPOS) -> tuple[RepoStatus, ...]:
    """Every git checkout directly under root, by name. Not recursive."""
    base = Path(root).expanduser()
    if not base.is_dir():
        return ()
    found: list[RepoStatus] = []
    for child in sorted(base.iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if not is_git_checkout(child):
            continue
        found.append(describe(child))
        if len(found) >= limit:
            break
    return tuple(found)


def find(root: Path, name: str) -> RepoStatus | None:
    """Resolve a name (``Hustler`` or ``owner/Hustler``) to a checkout under root.

    Only names are accepted, never paths: a leading slash, a parent segment or a
    separator that is not the owner/repo form is refused.
    """
    wanted = (name or "").strip()
    if not wanted or wanted in (".", "..") or wanted.startswith("/") or ".." in wanted:
        return None
    if "/" in wanted:
        wanted = wanted.rsplit("/", 1)[-1]
    wanted = wanted.lower()
    if not wanted:
        return None
    for repo in discover(root):
        if repo.name.lower() == wanted:
            return repo
    return None
