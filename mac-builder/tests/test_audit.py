"""`/audit`: the branch list, stage classification, failure parsing and the report."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TargetTests(unittest.TestCase):
    """The base tip plus every review not merged yet; one branch when asked."""

    def test_the_list_is_the_base_then_unmerged_reviews(self) -> None:
        reviewed = (("freebuff/aaaa", "awaiting_review"), ("freebuff/bbbb", "approved"),
                    ("freebuff/cccc", "approved"), ("freebuff/dddd", "rejected"))
        self.assertEqual(audit.targets("freebuff/base", reviewed=reviewed,
                                       skip={"freebuff/cccc"}),
                         ["freebuff/base", "freebuff/aaaa", "freebuff/bbbb"])

    def test_one_requested_branch_is_the_whole_list(self) -> None:
        self.assertEqual(audit.targets("freebuff/base", requested="freebuff/zzzz"),
                         ["freebuff/zzzz"])
        self.assertEqual(audit.targets("freebuff/base",
                                       reviewed=(("freebuff/base", "approved"),)),
                         ["freebuff/base"])
        name = audit.safe_name("../../evil/branch")  # only ever a folder name
        self.assertNotIn("/", name)
        self.assertFalse(name.startswith("."))

    def test_only_awaiting_and_approved_reviews_name_a_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, status, branch in (("a", "awaiting_review", "freebuff/aaaa"),
                                         ("b", "approved", "freebuff/bbbb"),
                                         ("c", "rejected", "freebuff/cccc")):
                (root / f"{name}.review.json").write_text(
                    json.dumps({"status": status, "env": {"branch": branch}}), "utf-8")
            (root / "d.review.json").write_text("not json", "utf-8")
            self.assertEqual(audit.reviewed(SimpleNamespace(log_dir=root)),
                             [("freebuff/aaaa", "awaiting_review"),
                              ("freebuff/bbbb", "approved")])


class StageTests(unittest.TestCase):
    """A missing tool is unavailable, and a stage that did not run is not run."""

    def test_exit_codes_are_classified(self) -> None:
        self.assertEqual(audit.classify(0), (audit.PASSED, ""))
        self.assertEqual(audit.classify(127), (audit.UNAVAILABLE, "tool not installed"))
        self.assertEqual(audit.classify(78), (audit.UNAVAILABLE, "no test action"))
        self.assertEqual(audit.classify(65)[0], audit.FAILED)
        self.assertEqual(audit.classify(0, timed_out=True)[0], audit.FAILED)

    def test_a_stage_that_did_not_run_is_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = audit.run_stage("drift", ["true"], Path(tmp), {}, 5.0,
                                     Path(tmp) / "drift.mjs")
        self.assertEqual((result.state, result.code), (audit.NOT_RUN, None))

    def test_a_real_exit_code_is_kept_with_its_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = audit.run_stage("check", ["bash", "-c", "echo boom; exit 2"],
                                     Path(tmp), {}, 10.0)
        self.assertEqual((result.state, result.code), (audit.FAILED, 2))
        self.assertIn("boom", result.tail)


class FailureTests(unittest.TestCase):
    """Failures come out of the logs with plain patterns, never from the model."""

    def test_compiler_errors_and_failing_test_names_are_read(self) -> None:
        found = audit.parse_failures((FIXTURES / "audit-check.log").read_text("utf-8"))
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].split("` error: ")[1], "cannot find 'theme' in scope")
        self.assertTrue(found[1].endswith("Theme.swift:7` error: value of type 'Color' has no member 'accent'"))
        self.assertEqual(found[2], "failed test HustlerCoreTests.ThemeTests testAccentContrast")

    def test_swift_testing_failures_are_named_too(self) -> None:
        self.assertEqual(audit.parse_failures((FIXTURES / "audit-drift.log").read_text("utf-8")),
                         ["failed test themeContrasts",
                          "failed test HustlerCoreTests.ThemeTests testAccentContrast"])
        self.assertEqual(audit.parse_failures("Test Case '-[A b]' passed (0.1 seconds)."), [])


class ReportTests(unittest.TestCase):
    """The report has a section per state and says CI results are not visible."""

    def test_the_report_has_every_section_and_names_missing_stages(self) -> None:
        results = [audit.TargetResult("freebuff/aaaa", [
            audit.StageResult("check", audit.PASSED, 0),
            audit.StageResult("build", audit.UNAVAILABLE, 127, note="tool not installed"),
            audit.StageResult("test", audit.FAILED, 65, tail=["x.swift:3: error: nope"])])]
        text = audit.report("20260101-000000", results, "freebuff/base")
        for heading in ("## Passed", "## Failed", "## Unavailable", "## Not run"):
            self.assertIn(heading, text)
        self.assertIn("CI results are not visible", text)
        self.assertIn("`freebuff/aaaa` drift", text)
        self.assertIn("passed 1 · failed 1 · unavailable 1 · not run 1",
                      audit.summary("20260101-000000", results, "audit-x.md"))
