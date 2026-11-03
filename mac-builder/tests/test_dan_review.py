"""The deterministic /dan checks: what they flag and what they leave alone."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dan_review  # noqa: E402


class TaskLineTests(unittest.TestCase):
    def test_each_do_not_line_is_checked(self) -> None:
        problems = dan_review.no_edit_problems(
            "Do not edit mac-builder/rules.py.\n"
            "- do not touch mac-builder/tests/*\n"
            "Please fix the bug.\n"
            "Do not edit or touch .env*\n",
            ["mac-builder/rules.py", "mac-builder/tests/test_rules.py", ".env.local"],
        )
        self.assertEqual(len(problems), 3)

    def test_two_paths_on_one_line_are_two_patterns(self) -> None:
        problems = dan_review.no_edit_problems(
            "Do not edit mac-builder/rules.py or mac-builder/freebuff_runner.py",
            ["mac-builder/rules.py", "mac-builder/freebuff_runner.py"],
        )
        self.assertEqual(len(problems), 2)

    def test_globs_directories_and_basenames_match(self) -> None:
        cases = {
            "mac-builder/tests/*": "mac-builder/tests/test_rules.py",
            "mac-builder/tests": "mac-builder/tests/test_rules.py",
            "*.json": "mac-builder/projects.json",
            ".env*": "mac-builder/.env.local",
        }
        for pattern, path in cases.items():
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    dan_review.no_edit_problems(f"Do not touch {pattern}", [path])
                )

    def test_an_untouched_file_is_not_a_problem(self) -> None:
        self.assertEqual(
            dan_review.no_edit_problems(
                "Do not edit mac-builder/rules.py", ["mac-builder/jobs.py"]
            ),
            [],
        )
        self.assertEqual(
            dan_review.no_edit_problems(
                "Do not edit mac-builder/rules.py", ["nested/mac-builder/rules.py"]
            ),
            [],
        )


class CompanionTests(unittest.TestCase):
    def test_a_module_change_needs_a_test_change(self) -> None:
        problems = dan_review.test_companion_problems(["mac-builder/jobs.py"])
        self.assertEqual(len(problems), 1)
        self.assertIn("mac-builder/jobs.py", problems[0])

    def test_a_test_change_satisfies_it(self) -> None:
        self.assertEqual(
            dan_review.test_companion_problems(
                ["mac-builder/jobs.py", "mac-builder/tests/test_jobs.py"]
            ),
            [],
        )

    def test_nested_or_non_python_changes_need_no_test(self) -> None:
        self.assertEqual(
            dan_review.test_companion_problems(
                ["mac-builder/README.md", "mac-builder/fixtures/x.py", "other/a.py"]
            ),
            [],
        )


class EnvFileTests(unittest.TestCase):
    def test_env_files_are_a_problem(self) -> None:
        problems = dan_review.env_file_problems(
            [".env", "mac-builder/.env.local", "mac-builder/jobs.py"]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn(".env", problems[0])

    def test_similar_names_are_not(self) -> None:
        self.assertEqual(
            dan_review.env_file_problems(
                ["config/env.py", "mac-builder/environment.py"]
            ),
            [],
        )


class CountTests(unittest.TestCase):
    def test_count_tests_reads_test_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "test_a.py").write_text(
                "class A:\n    def test_one(self):\n        pass\n"
                "\n\ndef test_two():\n    pass\n\n\ndef helper():\n    pass\n",
                encoding="utf-8",
            )
            (tests / "test_b.py").write_text(
                "def test_three():\n    pass\n", encoding="utf-8"
            )
            self.assertEqual(dan_review.count_tests(tests), 3)


class TestCountAgainstMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        tests = self.repo / "mac-builder" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_sample.py").write_text(
            "class T:\n    def test_one(self):\n        pass\n\n"
            "    def test_two(self):\n        pass\n",
            encoding="utf-8",
        )
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "mac-builder/tests/test_sample.py")
        self._git("commit", "-qm", "base")
        self._git("branch", "-M", "main")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def test_a_removed_test_is_a_problem(self) -> None:
        self.assertEqual(dan_review.count_tests_at_ref(self.repo, "main"), 2)
        (self.repo / "mac-builder/tests/test_sample.py").write_text(
            "class T:\n    def test_one(self):\n        pass\n", encoding="utf-8"
        )
        problems = dan_review.test_count_problems(
            self.repo, ["mac-builder/tests/test_sample.py"], base_ref="main"
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("fewer than", problems[0])

    def test_untouched_tests_are_not_compared(self) -> None:
        self.assertEqual(
            dan_review.test_count_problems(
                self.repo, ["mac-builder/jobs.py"], base_ref="main"
            ),
            [],
        )


class CombinedTests(unittest.TestCase):
    def test_problems_are_reported_in_a_fixed_order(self) -> None:
        problems = dan_review.problems(
            "Do not touch mac-builder/rules.py",
            ["mac-builder/rules.py", "mac-builder/jobs.py", ".env"],
        )
        self.assertEqual(len(problems), 3)
        self.assertIn("do not edit", problems[0])
        self.assertIn("no changed or added file", problems[1])
        self.assertIn(".env", problems[2])

    def test_a_clean_change_has_no_problems(self) -> None:
        self.assertEqual(
            dan_review.problems(
                "Add a CLI flag",
                ["mac-builder/jobs.py", "mac-builder/tests/test_jobs.py"],
            ),
            [],
        )
