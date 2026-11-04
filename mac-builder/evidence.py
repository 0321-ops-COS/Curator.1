"""The evidence a review can show: commands, counts, CI state and test-only diffs.

Pure text and path logic, so the commit body, the review card and the manager
prompt quote the same recorded facts instead of a claim.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

TEST_ONLY_NOTE = "Test-only change: no app code was changed"
# Kinds whose jobs change app or bot code, so a test-only diff proves nothing.
TEST_ONLY_KINDS = ("fix", "bug", "task", "dan")
PYTHON_TEST_DIR = "mac-builder/tests/"
IOS_DIR = "ios-app"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# xcodebuild and swift test both end their run with this line.
_XCODE = re.compile(r"Executed (\d+) tests?, with (\d+) failures?")
_RAN = re.compile(r"Ran (\d+) tests?")
_FAILED = re.compile(r"(?:failures|errors)=(\d+)")
_PYTEST_PASSED = re.compile(r"(\d+) passed")
_PYTEST_FAILED = re.compile(r"(\d+) failed")
_SWIFT_TEST_SUFFIXES = ("Tests.swift", "Test.swift")


def counts(output: str) -> str:
    """'12 passed, 0 failed' parsed from a stage's output, or '' when silent."""
    text = _ANSI.sub("", output or "")
    executed = _XCODE.findall(text)
    if executed:  # the last line is the whole run, the earlier ones are suites
        tests, failures = executed[-1]
        return f"{max(0, int(tests) - int(failures))} passed, {int(failures)} failed"
    ran = _RAN.findall(text)
    if ran:
        total = int(ran[-1])
        failed = sum(int(value) for value in _FAILED.findall(text)) if "FAILED" in text else 0
        return f"{max(0, total - failed)} passed, {failed} failed"
    passed, failed = _PYTEST_PASSED.findall(text), _PYTEST_FAILED.findall(text)
    if passed or failed:
        return (f"{int(passed[-1]) if passed else 0} passed, "
                f"{int(failed[-1]) if failed else 0} failed")
    return ""


def stage_line(label: str, command: str, state: str, counts_text: str = "") -> str:
    """One stage as the card and the commit body word it; never omitted."""
    said = str(state or "").strip() or "not run"
    line = f"{label}: {str(command or '').strip() or 'not run'} — {said}"
    return f"{line} ({counts_text})" if str(counts_text or "").strip() else line


def verification_block(outcome: object) -> str:
    """The exact check, test and build commands run, with their state.

    The build only appears when it ran: a scheme with a test action already
    built the app, and a stage that did not run is never named as one that did.
    """
    lines = [
        stage_line("check", getattr(outcome, "check_command", ""),
                   getattr(outcome, "check_state", ""),
                   getattr(outcome, "check_counts", "")),
        stage_line("test", getattr(outcome, "test_command", ""),
                   getattr(outcome, "test_state", ""),
                   getattr(outcome, "test_counts", "")),
    ]
    build_state = str(getattr(outcome, "build_state", "") or "").strip()
    if build_state and build_state != "not run":
        lines.append(stage_line("build", getattr(outcome, "build_command", ""),
                                build_state))
    return "Verification\n" + "\n".join(lines)


def append_block(message: str, block: str) -> str:
    """A commit message with the verification block in its body."""
    body, head = (block or "").strip(), (message or "").strip()
    if not body:
        return head
    return f"{head}\n\n{body}" if head else body


def original_request(job: object) -> str:
    """The owner's own wording, before any rewrite; '' when none was kept."""
    for value in (getattr(job, "original_text", ""), getattr(job, "text", "")):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def is_swift_test_path(path: str) -> bool:
    """True for an XCTest file under ios-app."""
    value = str(path or "").replace("\\", "/").lstrip("./")
    if not value.startswith(IOS_DIR + "/") or not value.endswith(".swift"):
        return False
    return value.endswith(_SWIFT_TEST_SUFFIXES) or "/Tests/" in value


def is_test_path(path: str) -> bool:
    """A Python test the bot owns, or a Swift XCTest file."""
    value = str(path or "").replace("\\", "/").lstrip("./")
    return value.startswith(PYTHON_TEST_DIR) or is_swift_test_path(value)


def is_test_only(changed_paths: Iterable[str]) -> bool:
    """True when every changed file is a test file."""
    paths = [str(path).strip() for path in changed_paths if str(path).strip()]
    return bool(paths) and all(is_test_path(path) for path in paths)


def test_only_note(kind: str, changed_paths: Iterable[str]) -> str:
    """The card's line for a fix that changed only tests: it proves nothing."""
    return TEST_ONLY_NOTE if (str(kind) in TEST_ONLY_KINDS
                              and is_test_only(changed_paths)) else ""


def ci_label(states: Sequence[str]) -> str:
    """One word for a CI state list; a failure wins over everything else."""
    items = [str(state).strip().lower() for state in states or () if str(state).strip()]
    if any(state in ("failure", "failed", "error", "timed_out") for state in items):
        return "failed"
    if "pending" in items:
        return "pending"
    return "passed" if items else "none"


def block_approve(verdict: str, notes: str, reason: str) -> tuple[str, str]:
    """An approve that may not stand becomes ask_user, with the reason named."""
    if not reason:
        return verdict, notes
    forced = "ask_user" if verdict == "approve" else verdict
    return forced, reason + (f"\n\n{notes}" if notes else "")
