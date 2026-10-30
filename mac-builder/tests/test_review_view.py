"""The Approve button must not exist when a check or test failed.

Building the view needs no Discord connection: the buttons are plain objects and
the allowlist is only consulted when one is pressed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402


def outcome(
    *,
    check_ok: bool = True,
    test_ok: bool | None = True,
    check_state: str = "passed",
    test_state: str = "passed",
) -> jobs.ReviewOutcome:
    return jobs.ReviewOutcome(
        job=jobs.Job(
            id="20260101-000000-deadbeef",
            kind="task",
            title="add a button",
            channel_id=1,
            requester_id=2,
            log_path=Path("/tmp/20260101-000000-deadbeef.log"),
        ),
        verdict="ask_user",
        notes="",
        environment=None,
        check_state=check_state,
        test_state=test_state,
        diff_lines=4,
        changed_files=("ios-app/RootView.swift",),
        check_ok=check_ok,
        test_ok=test_ok,
    )


class ApproveButtonTests(unittest.TestCase):
    def labels(self, **kwargs: object) -> list[str]:
        view = bot.ReviewView(SimpleNamespace(), outcome(**kwargs))
        return [child.label for child in view.children]

    def test_failed_check_has_no_approve_button(self) -> None:
        labels = self.labels(check_ok=False, check_state="failed (exit 1)")
        self.assertNotIn("Approve", labels)
        self.assertIn("Reject", labels)

    def test_failed_test_has_no_approve_button(self) -> None:
        labels = self.labels(test_ok=False, test_state="failed (exit 65)")
        self.assertNotIn("Approve", labels)
        self.assertIn("Reject", labels)

    def test_unavailable_test_stays_approvable(self) -> None:
        labels = self.labels(test_ok=None, test_state="unavailable (scheme has no test action)")
        self.assertIn("Approve", labels)

    def test_passing_stages_stay_approvable(self) -> None:
        self.assertIn("Approve", self.labels())

    def test_stale_press_is_refused_even_with_a_button(self) -> None:
        self.assertFalse(bot.is_approvable(outcome(check_ok=False)))
        self.assertFalse(bot.is_approvable(outcome(test_ok=False)))
        self.assertTrue(bot.is_approvable(outcome(test_ok=None)))


if __name__ == "__main__":
    unittest.main()
