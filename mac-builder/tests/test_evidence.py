"""The evidence a review shows: counts, the block, test-only diffs and the prompt."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dan_review  # noqa: E402
import evidence  # noqa: E402
import manager  # noqa: E402


def outcome(**facts: object) -> SimpleNamespace:
    values = dict(check_command="scripts/check.sh", check_state="passed",
                  check_counts="12 passed, 0 failed", test_command="scripts/test.sh",
                  test_state="not run", test_counts="")
    values.update(facts)
    return SimpleNamespace(**values)


class CountsTests(unittest.TestCase):
    def test_counts_come_from_the_stage_output_or_are_left_out(self) -> None:
        cases = {
            "Test Suite 'All tests' passed\nExecuted 31 tests, with 2 failures":
                "29 passed, 2 failed",
            "Ran 9 tests in 0.2s\n\nFAILED (failures=2, errors=1)": "6 passed, 3 failed",
            "===== 7 passed, 1 failed in 0.30s =====": "7 passed, 1 failed",
            "nothing to count": "",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(evidence.counts(text), expected)


class BlockTests(unittest.TestCase):
    def test_a_block_names_both_stages_and_says_not_run(self) -> None:
        block = evidence.verification_block(outcome())
        self.assertTrue(block.startswith("Verification"))
        self.assertIn("check: scripts/check.sh — passed (12 passed, 0 failed)", block)
        self.assertIn("test: scripts/test.sh — not run", block)
        self.assertNotIn("not run (", block)

    def test_the_block_goes_into_the_commit_body(self) -> None:
        body = evidence.append_block("Freebuff task x: fix it",
                                     evidence.verification_block(outcome()))
        self.assertTrue(body.startswith("Freebuff task x: fix it\n\nVerification\n"))


class TestOnlyTests(unittest.TestCase):
    def test_only_test_files_count_as_test_only(self) -> None:
        self.assertTrue(evidence.is_test_only(
            ["ios-app/HustlerTests/LoginTests.swift", "mac-builder/tests/test_jobs.py"]))
        self.assertFalse(evidence.is_test_only(
            ["ios-app/HustlerTests/LoginTests.swift", "ios-app/Hustler/Login.swift"]))

    def test_a_test_only_fix_cannot_be_plainly_approved(self) -> None:
        note = evidence.test_only_note("fix", ["ios-app/HustlerTests/LoginTests.swift"])
        self.assertEqual(note, "Test-only change: no app code was changed")
        self.assertEqual(evidence.test_only_note("fix", ["ios-app/Hustler/Login.swift"]), "")
        verdict, notes = evidence.block_approve("approve", "Looks fine.", note)
        self.assertEqual(verdict, "ask_user")
        self.assertIn("Test-only change", notes)
        self.assertIn("Looks fine.", notes)
        self.assertEqual(evidence.block_approve("revise", "Do it again.", ""),
                         ("revise", "Do it again."))


if __name__ == "__main__":
    unittest.main()
