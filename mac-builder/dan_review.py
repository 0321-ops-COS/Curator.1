"""Deterministic /dan checks, run before the manager review; no model calls."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

import rules

DEFAULT_BASE_REF = "origin/main"
TEST_DIR = "mac-builder/tests"
MAX_REPORTED_PATHS = 5

_DO_NOT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?do\s+not\s+(?:edit|touch)(?:\s+or\s+(?:edit|touch))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|\bor\b)\s*", re.IGNORECASE)
_TEST_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test", re.MULTILINE)


def _normalized(paths: Iterable[str]) -> list[str]:
    return [value for value in (rules.normalize_path(p) for p in paths) if value]


def _pattern_matches(pattern: str, path: str) -> bool:
    if fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("/") + "/"):
        return True
    parts = PurePosixPath(path).parts
    return "/" not in pattern and any(
        fnmatch.fnmatch(part, pattern) for part in (parts[-1], *parts)
    )


def no_edit_problems(task: str, changed_paths: Sequence[str]) -> list[str]:
    """A task line says do not edit/touch a path, and the diff touches it."""
    paths = _normalized(changed_paths)
    problems: list[str] = []
    for line in task.splitlines():
        match = _DO_NOT_RE.match(line)
        if match is None:
            continue
        for piece in _SPLIT_RE.split(match.group(1).strip().strip("`'\"")):
            pattern = rules.normalize_path(piece.strip().strip("`'\"").rstrip(".,;:"))
            touched = sorted({p for p in paths if _pattern_matches(pattern, p)})
            if touched:
                problems.append(
                    f"The task says do not edit {pattern}, but the diff touches "
                    f"{', '.join(touched[:MAX_REPORTED_PATHS])}."
                )
    return problems


def test_companion_problems(changed_paths: Sequence[str]) -> list[str]:
    """mac-builder/*.py changed with no changed or added file under tests/."""
    paths = _normalized(changed_paths)
    modules = [p for p in paths if p.endswith(".py") and len(PurePosixPath(p).parts) == 2
               and PurePosixPath(p).parts[0] == "mac-builder"]
    if not modules or any(p.startswith(TEST_DIR + "/") for p in paths):
        return []
    return [f"Changed mac-builder/*.py file(s) ({', '.join(modules)}) with no "
            f"changed or added file under {TEST_DIR}/; add or update a test."]


def env_file_problems(changed_paths: Sequence[str]) -> list[str]:
    """Any changed file whose name starts with .env."""
    paths = [p for p in _normalized(changed_paths)
             if PurePosixPath(p).name.lower().startswith(".env")]
    return [f"Changed .env file(s) ({', '.join(paths)}); environment files carry "
            "secrets and may not be edited by a job."] if paths else []


def count_tests(directory: Path) -> int:
    """Test functions defined by the Python files under this directory."""
    total = 0
    for path in Path(directory).rglob("*.py"):
        try:
            total += len(_TEST_DEF_RE.findall(
                path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return total


def _git(worktree: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(worktree), *args], text=True,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def count_tests_at_ref(worktree: Path, base_ref: str = DEFAULT_BASE_REF) -> int | None:
    """Test functions in mac-builder/tests at base_ref, or None if unreadable."""
    ref = base_ref.strip() or DEFAULT_BASE_REF
    listed = _git(worktree, "ls-tree", "-r", "--name-only", ref, "--", TEST_DIR)
    if listed is None:
        return None
    sources = [_git(worktree, "show", f"{ref}:{name}")
               for name in listed.splitlines() if name.endswith(".py")]
    if None in sources:
        return None
    return sum(len(_TEST_DEF_RE.findall(source)) for source in sources if source)


def test_count_problems(
    worktree: Path, changed_paths: Sequence[str], *, base_ref: str = DEFAULT_BASE_REF,
) -> list[str]:
    """Compare the test count with base_ref, but only when a test file changed."""
    if not any(p.startswith(TEST_DIR + "/") for p in _normalized(changed_paths)):
        return []
    before = count_tests_at_ref(worktree, base_ref)
    after = count_tests(Path(worktree) / TEST_DIR)
    if before is None or after >= before:
        return []
    return [f"The change leaves {after} Python test(s) in {TEST_DIR}/, fewer than "
            f"main's {before}; keep at least as many tests as main has."]


def problems(
    task: str, changed_paths: Sequence[str], *,
    worktree: Path | None = None, base_ref: str = DEFAULT_BASE_REF,
) -> list[str]:
    """Every deterministic problem with this /dan change, in a fixed order."""
    paths = _normalized(changed_paths)
    found = no_edit_problems(task, paths) + test_companion_problems(paths)
    found += env_file_problems(paths)
    if worktree is not None:
        found += test_count_problems(Path(worktree), paths, base_ref=base_ref)
    return found
