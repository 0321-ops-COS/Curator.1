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


class CiTests(unittest.TestCase):
    def test_a_failure_wins_then_pending_then_passed_then_none(self) -> None:
        self.assertEqual(evidence.ci_label(("success", "failure")), "failed")
        self.assertEqual(evidence.ci_label(("success", "pending")), "pending")
        self.assertEqual(evidence.ci_label(("success",)), "passed")
        self.assertEqual(evidence.ci_label(()), "none")


class SwiftCountingTests(unittest.TestCase):
    def test_xctest_functions_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            (app / "LoginTests.swift").write_text(
                "@MainActor\nfinal class LoginTests: XCTestCase {\n"
                "    func testLogin() async throws {}\n"
                "    func testLogout() {}\n    func helper() {}\n}\n",
                encoding="utf-8")
            self.assertEqual(dan_review.count_swift_tests(app), 2)


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_original_request_and_the_symptom_question(self) -> None:
        brief = manager.ReviewBrief(
            task="Rewrite the login flow.", diff="+let x = 1\n", diff_lines=1, status="",
            changed_files=["ios-app/Hustler/Login.swift"], check_state="passed",
            check_tail="", test_state="not run", test_tail="",
            original_request="Login is broken on iPad; the button does nothing.")
        messages = manager.build_review_messages(brief)
        self.assertIn("ORIGINAL REQUEST", messages[1]["content"])
        self.assertIn("the button does nothing", messages[1]["content"])
        self.assertIn("every symptom", messages[0]["content"])

    def test_untouched_symptoms_are_parsed_or_empty(self) -> None:
        parsed = manager.parse_review(
            '{"verdict": "approve", "notes": "fine", '
            '"untouched_symptoms": ["the icon is still wrong", ""]}')
        self.assertEqual(parsed.untouched_symptoms, ("the icon is still wrong",))
        self.assertEqual(
            manager.parse_review('{"verdict": "approve", "notes": "fine"}')
            .untouched_symptoms, ())


if __name__ == "__main__":
    unittest.main()
