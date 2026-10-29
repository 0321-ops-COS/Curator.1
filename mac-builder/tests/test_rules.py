from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rules  # noqa: E402


class ProtectedPathTests(unittest.TestCase):
    def test_project_yml_is_protected_anywhere(self) -> None:
        self.assertTrue(rules.is_protected_path("project.yml"))
        self.assertTrue(rules.is_protected_path("ios-app/project.yml"))

    def test_xcconfig_is_protected(self) -> None:
        self.assertTrue(rules.is_protected_path("ios-app/Config/Debug.xcconfig"))

    def test_env_files_are_protected(self) -> None:
        for path in (".env", "mac-builder/.env", ".env.local", "config/.env.production"):
            with self.subTest(path=path):
                self.assertTrue(rules.is_protected_path(path))

    def test_github_and_mac_builder_are_protected(self) -> None:
        self.assertTrue(rules.is_protected_path(".github/workflows/ios.yml"))
        self.assertTrue(rules.is_protected_path("mac-builder/bot.py"))
        self.assertTrue(rules.is_protected_path("nested/mac-builder/rules.py"))

    def test_signing_material_is_protected(self) -> None:
        for path in (
            "ios-app/Hustler/Hustler.entitlements",
            "profiles/App.mobileprovision",
            "certs/dist.p12",
            "certs/dist.cer",
            "certs/key.pem",
            "signing/notes.md",
            "ExportOptions.plist",
        ):
            with self.subTest(path=path):
                self.assertTrue(rules.is_protected_path(path))

    def test_application_sources_are_not_protected(self) -> None:
        for path in (
            "ios-app/Hustler/App/RootView.swift",
            "ios-app/HustlerCore/Sources/HustlerCore/Money.swift",
            "ios-app/HustlerCore/Tests/HustlerCoreTests/MoneyTests.swift",
            "README.md",
            "docs/notes.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(rules.is_protected_path(path))


class EvaluateChangeTests(unittest.TestCase):
    def test_oversized_diff_is_rejected(self) -> None:
        violations = rules.evaluate_change(
            diff_lines=401, changed_paths=["ios-app/Hustler/App/RootView.swift"], max_diff_lines=400
        )
        self.assertEqual([item.code for item in violations], ["diff_too_large"])

    def test_diff_at_limit_is_allowed(self) -> None:
        violations = rules.evaluate_change(
            diff_lines=400, changed_paths=["ios-app/Hustler/App/RootView.swift"], max_diff_lines=400
        )
        self.assertEqual(violations, [])

    def test_protected_change_is_rejected(self) -> None:
        violations = rules.evaluate_change(
            diff_lines=3,
            changed_paths=["ios-app/project.yml", "ios-app/Hustler/App/RootView.swift"],
            max_diff_lines=400,
        )
        self.assertEqual([item.code for item in violations], ["protected_path"])
        self.assertEqual(violations[0].path, "ios-app/project.yml")

    def test_protected_list_is_capped(self) -> None:
        paths = [f"mac-builder/file{i}.py" for i in range(20)]
        violations = rules.evaluate_change(
            diff_lines=1, changed_paths=paths, max_diff_lines=400
        )
        self.assertEqual(len(violations), rules.MAX_PROTECTED_REPORTS + 1)
        self.assertIn("more protected", violations[-1].detail)

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rules.evaluate_change(diff_lines=0, changed_paths=[], max_diff_lines=0)


class DiffParsingTests(unittest.TestCase):
    def test_counts_content_lines_only(self) -> None:
        diff = (
            "diff --git a/a.swift b/a.swift\n"
            "index 111..222 100644\n"
            "--- a/a.swift\n"
            "+++ b/a.swift\n"
            "@@ -1,2 +1,2 @@\n"
            "-let a = 1\n"
            "+let a = 2\n"
            " let b = 3\n"
        )
        self.assertEqual(rules.count_diff_lines(diff), 2)

    def test_extracts_paths(self) -> None:
        diff = (
            "diff --git a/ios-app/A.swift b/ios-app/A.swift\n"
            "--- a/ios-app/A.swift\n"
            "+++ b/ios-app/A.swift\n"
            "diff --git a/ios-app/B.swift b/ios-app/B.swift\n"
        )
        self.assertEqual(
            rules.changed_files_from_diff(diff), ["ios-app/A.swift", "ios-app/B.swift"]
        )


class VerdictTests(unittest.TestCase):
    def test_coerce_verdict(self) -> None:
        self.assertEqual(rules.coerce_verdict("APPROVE"), "approve")
        self.assertEqual(rules.coerce_verdict(" revise\n"), "revise")
        self.assertEqual(rules.coerce_verdict("ask_user"), "ask_user")
        self.assertEqual(rules.coerce_verdict("merge it"), "ask_user")
        self.assertEqual(rules.coerce_verdict(None), "ask_user")

    def test_failed_check_forces_revise(self) -> None:
        self.assertIn("/check", rules.force_revise_reason(False, True) or "")

    def test_failed_test_forces_revise(self) -> None:
        self.assertIn("/test", rules.force_revise_reason(True, False) or "")

    def test_unavailable_test_does_not_force_revise(self) -> None:
        self.assertIsNone(rules.force_revise_reason(True, None))

    def test_passing_stages_do_not_force_revise(self) -> None:
        self.assertIsNone(rules.force_revise_reason(True, True))


class BranchTests(unittest.TestCase):
    def test_task_branches(self) -> None:
        self.assertTrue(rules.is_task_branch("freebuff/0123abcd"))
        self.assertTrue(rules.is_task_branch("freebuff/ce4b8907c250c30103d8993e"))

    def test_non_task_branches(self) -> None:
        for branch in ("main", "master", "feature/x", "freebuff/", "freebuff/../x", "freebuff/a/b"):
            with self.subTest(branch=branch):
                self.assertFalse(rules.is_task_branch(branch))

    def test_push_validation(self) -> None:
        self.assertEqual(
            rules.validate_push_branch("refs/heads/freebuff/abc123"), "freebuff/abc123"
        )
        for branch in ("main", "master", "HEAD", "refs/heads/main"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    rules.validate_push_branch(branch)

    def test_base_branch_cannot_be_main(self) -> None:
        with self.assertRaises(ValueError):
            rules.assert_not_main("main")
        with self.assertRaises(ValueError):
            rules.assert_not_main("master")
        self.assertEqual(rules.assert_not_main("freebuff/abc"), "freebuff/abc")


class TaskTextTests(unittest.TestCase):
    def test_normalises_and_collapses_blank_lines(self) -> None:
        self.assertEqual(rules.sanitize_task_text("  hello\r\n\r\n\r\nworld  "), "hello\n\nworld")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            rules.sanitize_task_text("   \n  ")

    def test_rejects_oversized(self) -> None:
        with self.assertRaises(ValueError):
            rules.sanitize_task_text("x" * (rules.MAX_TASK_CHARS + 1))

    def test_strips_control_characters(self) -> None:
        self.assertEqual(rules.sanitize_task_text("a\x00b\x07c"), "abc")


class NotesTests(unittest.TestCase):
    def test_notes_are_capped(self) -> None:
        notes = rules.clamp_notes("x" * (rules.MAX_NOTES_CHARS + 100))
        self.assertLessEqual(len(notes), rules.MAX_NOTES_CHARS + len("\n[notes truncated]"))
        self.assertTrue(notes.endswith("[notes truncated]"))

    def test_missing_notes(self) -> None:
        self.assertEqual(rules.clamp_notes(None), "")


if __name__ == "__main__":
    unittest.main()
