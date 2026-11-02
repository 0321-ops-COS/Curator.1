"""The test card: the task's own steps, quoted, capped, and never invented."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_card  # noqa: E402

TASK = """Add a share button.

## Acceptance
1. The button is on the home screen.
2. Tapping it opens the share sheet.

## Notes
Nothing else changes.
"""


class CardTests(unittest.TestCase):
    def test_steps_come_from_the_acceptance_heading_only(self) -> None:
        self.assertEqual(test_card.steps(TASK), [
            "The button is on the home screen.", "Tapping it opens the share sheet."])
        self.assertNotIn("Nothing else changes", test_card.card(TASK))

    def test_a_test_line_is_a_step_without_a_heading(self) -> None:
        self.assertEqual(test_card.steps("Do it.\nTest: open the app twice"),
                         ["open the app twice"])

    def test_at_most_five_steps_are_taken(self) -> None:
        task = "## Test\n" + "\n".join(f"- step {number}" for number in range(9))
        self.assertEqual(len(test_card.steps(task)), test_card.MAX_STEPS)

    def test_a_task_with_no_steps_says_so_instead_of_guessing(self) -> None:
        self.assertEqual(test_card.card("Make it nicer."), "No test steps in the task")

if __name__ == "__main__":
    unittest.main()
