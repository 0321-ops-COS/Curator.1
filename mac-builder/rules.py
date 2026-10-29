"""Hard guardrails for the Mac builder bot.

Every rule in this module is enforced in Python, before any model is called and
before anything can be pushed. The AI manager may only narrow what happens next;
it can never widen these limits.

Nothing here runs commands. The functions are pure so they can be unit-tested
without a Mac, Discord or a model endpoint.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Sequence

# Discord and Freebuff prompts are user-supplied text. Keep them small enough
# for a chat turn and for a terminal paste.
MAX_TASK_CHARS = 4000
MAX_NOTES_CHARS = 4000

# Never print an unbounded list of offending paths into Discord.
MAX_PROTECTED_REPORTS = 12

TASK_BRANCH_PREFIX = "freebuff/"

# The bot may never push or merge these, whatever the model says.
BLOCKED_BRANCHES = frozenset({"main", "master", "head"})

VERDICTS = ("approve", "revise", "ask_user")

_BRANCH_RE = re.compile(r"^freebuff/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_SIGNING_DIRS = frozenset({"signing", "provisioning", "provisioning profiles"})
_SIGNING_SUFFIXES = (
    ".entitlements",
    ".mobileprovision",
    ".provisionprofile",
    ".p12",
    ".cer",
    ".pem",
    ".key",
    ".keychain",
)
_SIGNING_NAMES = frozenset({"exportoptions.plist"})


@dataclass(frozen=True)
class RuleViolation:
    """A single reason a candidate change cannot reach approval."""

    code: str
    detail: str
    path: str | None = None


def normalize_path(path: str) -> str:
    """Return a repository-relative path with forward slashes and no './'."""
    value = str(path).replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def is_protected_path(path: str) -> bool:
    """True when a candidate change may not touch this repository path.

    Protected: project.yml, *.xcconfig, .github/, .env*, the mac-builder folder
    itself, and signing / provisioning material.
    """
    value = normalize_path(path)
    if not value:
        return False

    parts = PurePosixPath(value).parts
    name = parts[-1]
    lowered = name.lower()

    if name == "project.yml":
        return True
    if fnmatch.fnmatch(lowered, "*.xcconfig"):
        return True
    if parts and parts[0] == ".github":
        return True
    if any(part == "mac-builder" for part in parts):
        return True
    if lowered.startswith(".env"):
        return True
    if any(part.lower() in _SIGNING_DIRS for part in parts[:-1]):
        return True
    if lowered.endswith(_SIGNING_SUFFIXES):
        return True
    if lowered in _SIGNING_NAMES:
        return True
    return False


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def changed_files_from_diff(diff: str) -> list[str]:
    """Best-effort list of paths named by a unified git diff."""
    paths: set[str] = set()
    for line in diff.splitlines():
        match = _DIFF_HEADER_RE.match(line)
        if match:
            paths.add(normalize_path(match.group(2)))
            continue
        if line.startswith("+++ b/"):
            paths.add(normalize_path(line[6:]))
    return sorted(path for path in paths if path and path != "/dev/null")


def count_diff_lines(diff: str) -> int:
    """Count added + removed content lines, ignoring file headers."""
    total = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            total += 1
    return total


def evaluate_change(
    *,
    diff_lines: int,
    changed_paths: Sequence[str],
    max_diff_lines: int,
) -> list[RuleViolation]:
    """Reject oversized diffs and protected-path changes before any review.

    `diff_lines` is the added + removed line count, normally from
    `git diff --numstat` so an enormous diff never has to be loaded in memory.
    """
    if max_diff_lines < 1:
        raise ValueError("max_diff_lines must be at least 1")

    violations: list[RuleViolation] = []
    if diff_lines > max_diff_lines:
        violations.append(
            RuleViolation(
                code="diff_too_large",
                detail=(
                    f"Diff is {diff_lines} changed lines; the limit is "
                    f"{max_diff_lines} (MAX_DIFF_LINES)."
                ),
            )
        )

    protected = sorted({normalize_path(path) for path in changed_paths if is_protected_path(path)})
    for path in protected[:MAX_PROTECTED_REPORTS]:
        violations.append(
            RuleViolation(
                code="protected_path",
                detail=f"Protected path changed: {path}",
                path=path,
            )
        )
    if len(protected) > MAX_PROTECTED_REPORTS:
        violations.append(
            RuleViolation(
                code="protected_path",
                detail=(
                    f"... and {len(protected) - MAX_PROTECTED_REPORTS} more protected "
                    "paths."
                ),
            )
        )
    return violations


def is_task_branch(branch: str) -> bool:
    """True for a branch the bot or Freebuff is allowed to own and push."""
    value = branch.strip()
    if not _BRANCH_RE.match(value):
        return False
    if ".." in value or value.endswith(".lock"):
        return False
    return True


def validate_push_branch(branch: str) -> str:
    """Return a safe task branch name or raise. Used before every push."""
    value = branch.strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/") :]
    if value.lower() in BLOCKED_BRANCHES:
        raise ValueError(f"refusing to push blocked branch: {value}")
    if not is_task_branch(value):
        raise ValueError(f"refusing to push non-freebuff branch: {value}")
    return value


def assert_not_main(branch: str) -> str:
    """Reject main/master as a configured base branch."""
    value = branch.strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/") :]
    if not value:
        raise ValueError("branch name is empty")
    if value.lower() in BLOCKED_BRANCHES:
        raise ValueError(f"refusing to use {value} as the base branch")
    return value


def force_revise_reason(check_ok: bool, test_ok: bool | None) -> str | None:
    """Return a hard reason to revise when a real check or test failed.

    `test_ok=None` means the test stage was unavailable (for example the Xcode
    scheme has no test action), which is reported but cannot fail a diff.
    """
    failures = []
    if not check_ok:
        failures.append("/check (swift test) failed")
    if test_ok is False:
        failures.append("/test (xcodebuild test) failed")
    if failures:
        return "; ".join(failures) + ". Fix the failure before this can be approved."
    return None


def coerce_verdict(value: object) -> str:
    """Map a model verdict onto the three allowed values."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in VERDICTS:
            return lowered
    return "ask_user"


def clamp_notes(value: object) -> str:
    if not isinstance(value, str):
        return ""
    notes = value.strip()
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS].rstrip() + "\n[notes truncated]"
    return notes


def sanitize_task_text(text: str) -> str:
    """Normalise Discord task text. Raises ValueError when unusable."""
    if not isinstance(text, str):
        raise ValueError("task text must be a string")
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(
        ch for ch in value if ch in ("\n", "\t") or (ord(ch) >= 32 and ch != "\x7f")
    )
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        raise ValueError("task text is empty")
    if len(value) > MAX_TASK_CHARS:
        raise ValueError(
            f"task text is {len(value)} characters; the limit is {MAX_TASK_CHARS}"
        )
    return value


def protected_summary(violations: Iterable[RuleViolation]) -> str:
    """One short line for Discord and logs."""
    items = list(violations)
    if not items:
        return ""
    return "\n".join(f"- {item.detail}" for item in items)
