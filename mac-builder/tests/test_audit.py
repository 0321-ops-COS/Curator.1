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
import roadmap  # noqa: E402
from manager import (Manager, ManagerUnavailable,  # noqa: E402
                     build_cause_messages, parse_cause)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def failed_stage(name: str = "check", fixture: str = "audit-check.log") -> audit.StageResult:
    tail = (FIXTURES / fixture).read_text("utf-8").splitlines()
    return audit.StageResult(name, audit.FAILED, 65, tail=tail)


class FakeManager:
    """Only the cause call; the real Manager owns the HTTP and JSON parsing."""

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply, self.error, self.calls = reply, error, []

    async def cause(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.reply


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


class CauseTests(unittest.IsolatedAsyncioTestCase):
    """The model's opinion sits beside the raw errors, never instead of them."""

    async def test_every_failed_stage_gets_one_opinion_with_fixture_evidence(self) -> None:
        results = [audit.TargetResult("freebuff/aaaa", [failed_stage(),
                                                        audit.StageResult("build", audit.PASSED, 0)])]
        manager = FakeManager("A theme symbol is referenced but never defined.")
        await audit.add_causes(manager, results)
        self.assertEqual(manager.calls[0]["stage"], "check")
        self.assertTrue(any("RootView.swift:42" in line for line in manager.calls[0]["errors"]))
        self.assertIn("cannot find 'theme' in scope", manager.calls[0]["tail"])
        text = audit.report("20260101-000000", results, "freebuff/base")
        self.assertIn("cannot find 'theme' in scope", text)
        self.assertIn("likely cause (model opinion) for check: "
                      "A theme symbol is referenced but never defined.", text)
        self.assertEqual(len(manager.calls), 1, "a passing stage is never asked about")

    async def test_an_unavailable_manager_says_so_and_keeps_the_evidence(self) -> None:
        results = [audit.TargetResult("freebuff/aaaa", [failed_stage()])]
        await audit.add_causes(FakeManager(error=ManagerUnavailable("no key")), results)
        text = audit.report("20260101-000000", results, "freebuff/base")
        self.assertIn("manager unavailable", text)
        self.assertIn("cannot find 'theme' in scope", text)


class ManagerCauseTests(unittest.IsolatedAsyncioTestCase):
    """The cause reuses the manager's gate, JSON parsing and redaction."""

    async def test_owner_only_evidence_is_not_sent_to_the_model(self) -> None:
        self.assertEqual(await Manager().cause(stage="check", errors=[],
                                               tail="change project.yml", diff_stat=""), "")

    def test_the_cause_json_is_parsed_and_the_evidence_is_redacted(self) -> None:
        self.assertEqual(parse_cause('{"cause":"Fix the theme."}'), "Fix the theme.")
        self.assertEqual(parse_cause("prose, not json"), "")
        messages = build_cause_messages(stage="check", errors=["x"],
                                        tail="api_key=supersecret123", diff_stat="")
        self.assertNotIn("supersecret123", messages[1]["content"])


class SheetTests(unittest.TestCase):
    """One parseable job per distinct failure, the authorized branch first."""

    def test_jobs_are_parseable_and_ordered_with_the_rules_block(self) -> None:
        results = [
            audit.TargetResult("freebuff/job", [failed_stage("build", "audit-drift.log")]),
            audit.TargetResult("freebuff/base", [failed_stage()]),
            audit.TargetResult("freebuff/base", [audit.StageResult("drift", audit.PASSED, 0)]),
            audit.TargetResult("freebuff/gone", error="could not fetch it: nope"),
        ]
        steps = roadmap.parse(audit.fix_sheet("20260101-000000", results, "freebuff/base"))
        self.assertEqual([step.kind for step in steps], ["task", "task", "task"])
        self.assertIn("freebuff/base", steps[0].text)
        self.assertIn("freebuff/job", steps[1].text)
        self.assertIn("freebuff/gone", steps[2].text)
        for step in steps:
            self.assertIn("Rules for this job", step.text)
            self.assertIn("project.yml", step.text)
            self.assertIn("under 300 lines", step.text)
            self.assertIn("real test results", step.text)
            self.assertLessEqual(len(step.text), 4000)
        self.assertIn("cannot find 'theme' in scope", steps[0].text)

    def test_a_long_failure_keeps_the_rules_and_the_limit(self) -> None:
        long = audit.StageResult("test", audit.FAILED, 65,
                                 tail=[f"x.swift:{n}: error: boom" for n in range(300)])
        step = roadmap.parse(audit.fix_sheet(
            "s", [audit.TargetResult("freebuff/base", [long])], "freebuff/base"))[0]
        self.assertLessEqual(len(step.text), 4000)
        self.assertIn("Rules for this job", step.text)
        self.assertIn("under 300 lines", step.text)

    def test_nothing_failed_means_no_sheet(self) -> None:
        results = [audit.TargetResult("freebuff/base", [audit.StageResult("check", audit.PASSED, 0)])]
        self.assertEqual(audit.fix_sheet("s", results, "freebuff/base"), "")
