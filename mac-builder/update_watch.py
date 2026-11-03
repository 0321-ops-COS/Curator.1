"""Read-only update watch: what origin has that the running code does not.

Two questions, neither answered by changing a checkout: is the code the bot was
started from behind origin/main, and has the Hustler authorized branch moved
past the commit her jobs would branch from? Nothing here pulls, resets, checks
out or edits; the notices are facts and a person decides what to do with them.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_BRANCH, WATCH_REF = "main", "refs/mac-builder-watch"
MAX_SUBJECTS, MAX_NAMES, NOTICE_LIMIT = 10, 4, 1900
# The groups the owner asked for; anything else is reported as "other".
BOT_CODE_RE = re.compile(r"^mac-builder/(?:[^/]+\.py|scripts/|launchd/)")
TESTS_RE = re.compile(r"^mac-builder/tests/")
DOCS_RE = re.compile(r"\.md$|^docs/")
GROUPS = ("bot code", "tests", "docs only", "other")


def _git(root: Path, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """One argv-only git call; never a shell and never an exception."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _line(root: Path, *args: str, timeout: float = 30.0) -> str:
    code, out, _ = _git(root, *args, timeout=timeout)
    return out.strip() if code == 0 else ""


def group_for(path: str) -> str:
    if TESTS_RE.search(path):
        return "tests"
    if BOT_CODE_RE.search(path):
        return "bot code"
    return "docs only" if DOCS_RE.search(path) else "other"


@dataclass(frozen=True)
class Changes:
    subjects: tuple[str, ...]
    total: int
    files: tuple[str, ...]
    added: int
    removed: int
    fast_forward: bool
    dirty: int  # uncommitted paths in the checkout

    def group_lines(self) -> list[str]:
        buckets: dict[str, list[str]] = {name: [] for name in GROUPS}
        for path in self.files:
            buckets[group_for(path)].append(path)
        lines = []
        for name in GROUPS:
            names = buckets[name]
            if names:
                shown = ", ".join(f"`{path}`" for path in names[:MAX_NAMES])
                more = f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
                lines.append(f"{name} ({len(names)}): {shown}{more}")
        return lines


def _changes(root: Path, base: str, tip: str, *, forward_from: str = "HEAD") -> Changes:
    """What tip adds on top of base, as git sees it right now."""
    _, log, _ = _git(root, "log", f"-{MAX_SUBJECTS}", "--format=%h %s", f"{base}..{tip}")
    subjects = tuple(line.strip() for line in log.splitlines() if line.strip())
    total = int(_line(root, "rev-list", "--count", f"{base}..{tip}") or 0)
    files = tuple(sorted(set(_line(root, "diff", "--name-only", f"{base}...{tip}").splitlines())))
    added = removed = 0
    for row in _line(root, "diff", "--numstat", f"{base}...{tip}").splitlines():
        parts = row.split("\t")
        if len(parts) >= 2:
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    dirty = len(_line(root, "status", "--porcelain").splitlines())
    forward = _git(root, "merge-base", "--is-ancestor", forward_from, tip)[0] == 0
    return Changes(subjects, total, files, added, removed, forward, dirty)


def _notice(header: str, behind: int, changes: Changes) -> str:
    """One Discord-sized notice: facts, and an offer only when it is safe."""
    lines = [header, f"{behind} commit(s) behind", "", "commits:"]
    lines += [f"· {subject}" for subject in changes.subjects]
    if changes.total > len(changes.subjects):
        lines.append(f"· and {changes.total - len(changes.subjects)} more")
    lines += ["", f"files changed: {len(changes.files)}", *changes.group_lines(), "",
              f"changed lines: {changes.added + changes.removed} "
              f"(+{changes.added}/-{changes.removed})",
              f"working tree: dirty ({changes.dirty} uncommitted path(s))"
              if changes.dirty else "working tree: clean"]
    if not changes.fast_forward:
        lines.append("fast-forward: no — this has commits the base does not, so a "
                     "pull would not fast-forward; resolve it by hand.")
    elif changes.dirty:
        lines.append("fast-forward: yes, but not with a dirty tree; nothing is offered.")
    else:
        lines.append("fast-forward: yes — a plain `git pull --ff-only` would update it; "
                     "restart to load it.")
    return "\n".join(lines)[:NOTICE_LIMIT]


class UpdateWatcher:
    """Answers both questions with git; posts nothing itself."""

    def __init__(self, *, root: Path, repo_dir: Path, branch: str,
                 interval_seconds: float = 600.0) -> None:
        self.root = Path(root)
        self.repo_dir = Path(repo_dir)
        self.branch = branch
        self.interval = float(interval_seconds)
        # The commit in the startup "code:" line, held for the whole run.
        self.running = _line(self.root, "rev-parse", "HEAD")
        self.origin = "unknown"
        self.behind: int | None = None
        self.error = ""
        self._posted: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self.interval > 0 and bool(self.running)

    def status_line(self) -> str:
        """The one-line /status field: running, origin, behind, supervised."""
        if not self.running:
            return "update: not a git checkout · supervised unknown"
        if not self.enabled:
            return (f"update: running {self.running[:7]} · checks disabled "
                    "(UPDATE_CHECK_SECONDS=0) · supervised unknown")
        origin = self.origin if self.origin == "unknown" else self.origin[:7]
        behind = "unknown" if self.behind is None else str(self.behind)
        line = (f"update: running {self.running[:7]} · origin/main {origin} · "
                f"{behind} behind · supervised unknown")
        return line + (" · last check failed" if self.error else "")

    def check(self) -> tuple[str, ...]:
        """One read-only pass; at most one notice per newly seen tip."""
        if not self.enabled:
            return ()
        return self._self_notice() + self._target_notice()

    def _self_notice(self) -> tuple[str, ...]:
        code, _, err = _git(self.root, "fetch", "origin", SELF_BRANCH, timeout=180)
        if code != 0:
            self.error = (err or "git fetch failed").strip()[-200:].replace("\n", " ")
            return ()
        self.error = ""
        tip = _line(self.root, "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{SELF_BRANCH}")
        if not tip:  # a checkout without the usual fetch refspec
            tip = _line(self.root, "rev-parse", "FETCH_HEAD^{commit}")
        if not tip:
            return ()
        self.origin = tip
        self.behind = int(_line(self.root, "rev-list", "--count", f"{self.running}..{tip}") or 0)
        if not self.behind or f"self:{tip}" in self._posted:
            return ()
        self._posted.add(f"self:{tip}")
        return (_notice(f"🔄 New commits on origin/main for the bot's own code\n"
                        f"running `{self.running[:7]}` · origin/main `{tip[:7]}`",
                        self.behind, _changes(self.root, self.running, tip)),)

    def _target_notice(self) -> tuple[str, ...]:
        if not self.branch:
            return ()
        ref = f"{WATCH_REF}/{self.branch}"
        # Read the base before the fetch, and keep the configured refspec out
        # of it (--refmap=), so origin/<branch> cannot move under a job.
        base = _line(self.repo_dir, "rev-parse", "--verify", "--quiet",
                     f"refs/remotes/origin/{self.branch}")
        code, _, _ = _git(self.repo_dir, "fetch", "--quiet", "--no-tags", "--refmap=",
                          "origin", f"+refs/heads/{self.branch}:{ref}", timeout=180)
        tip = _line(self.repo_dir, "rev-parse", "--verify", "--quiet", ref) if code == 0 else ""
        if not tip or not base or base == tip or f"target:{tip}" in self._posted:
            return ()
        self._posted.add(f"target:{tip}")
        behind = int(_line(self.repo_dir, "rev-list", "--count", f"{base}..{tip}") or 0)
        changes = _changes(self.repo_dir, base, tip, forward_from=base)
        return (_notice(f"🔄 Hustler `{self.branch}` moved past the base her jobs use\n"
                        f"base `{base[:7]}` · origin `{tip[:7]}`", behind, changes),)
