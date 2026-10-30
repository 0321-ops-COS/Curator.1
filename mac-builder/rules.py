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
# /dan is the one path that may touch mac-builder/ itself, and it pushes here.
DAN_BRANCH_PREFIX = "dan/"

# For a dan job mac-builder/ is allowed, except these files: the rules, the
# module that pushes, and the tests that pin the guardrails.
DAN_GUARDRAIL_PATHS = frozenset(
    {
        "mac-builder/rules.py",
        "mac-builder/freebuff_runner.py",
        "mac-builder/tests/test_rules.py",
        "mac-builder/tests/test_push_guard.py",
        "mac-builder/tests/test_dan.py",
        "mac-builder/tests/test_approve_recheck.py",
    }
)

DAN_GUARDRAIL_REASONS = {
    "mac-builder/rules.py": "the hard rules live here, so /dan cannot edit its own guardrails",
    "mac-builder/freebuff_runner.py": "this is the push path and the push guard, so it is a guardrail too",
    "mac-builder/tests/test_rules.py": "this test pins the guardrails",
    "mac-builder/tests/test_push_guard.py": "this test pins the push guard",
    "mac-builder/tests/test_dan.py": "this test pins what /dan may and may not change",
    "mac-builder/tests/test_approve_recheck.py": "this test pins the Approve re-check",
}

# The allowlist and the Approve code live in bot.py, so a dan diff may not touch
# the lines that name them either. The manager may narrow /dan; never this.
DAN_BOT_GUARDRAIL_NAMES = (
    "require_allowed",
    "allowed_user_ids",
    "ALLOWED_USER_IDS",
    "ReviewView",
    "approve_and_push",
    "_approve",
)
DAN_GUARDRAIL_MODULE = "mac-builder/bot.py"

# The bot may never push or merge these, whatever the model says.
BLOCKED_BRANCHES = frozenset({"main", "master", "head"})

VERDICTS = ("approve", "revise", "ask_user")

_BRANCH_RE = re.compile(r"^freebuff/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DAN_BRANCH_RE = re.compile(r"^dan/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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


def is_protected_path(path: str, *, dan: bool = False) -> bool:
    """True when a candidate change may not touch this repository path.

    Protected for every job: project.yml, *.xcconfig, .github/, .env*, signing /
    provisioning material, and the mac-builder folder itself. A dan job is the
    one exception: it may edit mac-builder/, except the guardrail files that keep
    /dan bounded (the rules, the push path and the tests that pin them).
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
    if lowered.startswith(".env"):
        return True
    if any(part.lower() in _SIGNING_DIRS for part in parts[:-1]):
        return True
    if lowered.endswith(_SIGNING_SUFFIXES):
        return True
    if lowered in _SIGNING_NAMES:
        return True
    if dan:
        # mac-builder/ is editable for a dan job, apart from its guardrails.
        return value in DAN_GUARDRAIL_PATHS
    if any(part == "mac-builder" for part in parts):
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


def _diff_changed_lines(diff: str, wanted: str) -> list[str]:
    """The added and removed lines of one file in a unified diff."""
    lines: list[str] = []
    current = ""
    for line in diff.splitlines():
        match = _DIFF_HEADER_RE.match(line)
        if match:
            current = normalize_path(match.group(2))
            continue
        if current != wanted:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return lines


def dan_guardrail_violations(diff: str) -> list[RuleViolation]:
    """Reject a dan diff that changes the allowlist or the Approve code.

    Those live in bot.py next to everything else, so they cannot be blocked by
    path alone. Any added or removed line naming them is refused, and the
    message says why, so the model knows what to do instead.
    """
    violations: list[RuleViolation] = []
    seen: set[str] = set()
    for line in _diff_changed_lines(diff, DAN_GUARDRAIL_MODULE):
        for name in DAN_BOT_GUARDRAIL_NAMES:
            if name in line and name not in seen:
                seen.add(name)
                violations.append(
                    RuleViolation(
                        code="dan_guardrail",
                        detail=(
                            f"/dan cannot change the allowlist or the Approve code: "
                            f"this diff touches {name!r} in {DAN_GUARDRAIL_MODULE}. "
                            "They are guardrails, so the owner has to make that "
                            "change in a normal PR; keep the change away from them."
                        ),
                        path=DAN_GUARDRAIL_MODULE,
                    )
                )
    return violations


def _protected_detail(path: str, *, dan: bool) -> str:
    reason = DAN_GUARDRAIL_REASONS.get(normalize_path(path)) if dan else None
    return f"Protected path changed: {path} ({reason})." if reason else f"Protected path changed: {path}"


def evaluate_change(
    *,
    diff_lines: int,
    changed_paths: Sequence[str],
    max_diff_lines: int,
    dan: bool = False,
    diff: str = "",
) -> list[RuleViolation]:
    """Reject oversized diffs and protected-path changes before any review.

    `diff_lines` is the added + removed line count, normally from
    `git diff --numstat` so an enormous diff never has to be loaded in memory.
    A dan job passes `dan=True` (mac-builder/ allowed, its guardrails not) and
    its diff, so the allowlist and Approve code can be checked line by line.
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

    if dan and diff:
        violations.extend(dan_guardrail_violations(diff))

    protected = sorted(
        {
            normalize_path(path)
            for path in changed_paths
            if is_protected_path(path, dan=dan)
        }
    )
    for path in protected[:MAX_PROTECTED_REPORTS]:
        violations.append(
            RuleViolation(
                code="protected_path",
                detail=_protected_detail(path, dan=dan),
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


def is_dan_branch(branch: str) -> bool:
    """True for the dan/* branch a self-build job owns."""
    value = branch.strip()
    if not _DAN_BRANCH_RE.match(value):
        return False
    if ".." in value or value.endswith(".lock"):
        return False
    return True


def validate_push_branch(branch: str, *, kind: str = "task") -> str:
    """Return a safe branch name or raise. Used before every push.

    A task job may only push freebuff/*; a dan job may only push dan/*. Neither
    may push main/master, and the push guard refuses force pushes separately.
    """
    value = branch.strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/") :]
    if value.lower() in BLOCKED_BRANCHES:
        raise ValueError(f"refusing to push blocked branch: {value}")
    if kind == "dan":
        if not is_dan_branch(value):
            raise ValueError(
                f"refusing to push {value!r} from a dan job; only {DAN_BRANCH_PREFIX}* may be pushed"
            )
        return value
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


def force_revise_reason(
    check_ok: bool, test_ok: bool | None, *, dan: bool = False
) -> str | None:
    """Return a hard reason to revise when a real check or test failed.

    `test_ok=None` means the test stage was unavailable (for example the Xcode
    scheme has no test action), which is reported but cannot fail a diff. A dan
    job runs different stages, so the message names those instead.
    """
    failures = []
    if not check_ok:
        failures.append("/check (unittest discover) failed" if dan else "/check (swift test) failed")
    if test_ok is False:
        failures.append(
            "/test (py_compile + bash -n) failed"
            if dan
            else "/test (xcodebuild test) failed"
        )
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
