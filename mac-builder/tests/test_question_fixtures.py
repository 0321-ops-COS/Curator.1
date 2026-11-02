"""Every question box shape Melisa has to read, parsed from an exact pane.

There was no live Freebuff session and no capture under mac-builder/logs in this
clone, so the four files in tests/fixtures/ are built from the field report: a
titled frame with Close, numbered and unnumbered question rows, circles with
description lines, a Custom row, Submit, an ad banner above and a status footer
below. None of them is a raw capture, and each is asserted question by question
so a parser change cannot quietly reshape a box.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import questions  # noqa: E402
from freebuff_runner import FreebuffRunner  # noqa: E402
from questions import Option, Question  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(name: str) -> questions.QuestionBox:
    box = questions.parse_pane(load(name))
    assert box is not None, f"{name} must read as a box"
    return box


def runner_settings() -> SimpleNamespace:
    return SimpleNamespace(
        repo_dir=Path("/nonexistent/repo"),
        worktree_root=Path("/nonexistent/worktrees"),
        freebuff_timeout_seconds=30.0,
        freebuff_idle_seconds=0.15,
        freebuff_poll_seconds=0.05,
        freebuff_ready_seconds=1.0,
        question_wait_seconds=10.0,
        question_auto_answer=False,
        question_debug=False,
    )


# (fixture, [(number, text, labels, expanded, custom index), ...])
BOXES = {
    "box_numbered_two.txt": [
        (
            1,
            "Which tone should the project description use? "
            "(the answer goes to project.yml)",
            ("Formal", "Casual", "Technical", "Custom"),
            True,
            3,
        ),
        (
            2,
            "What name should go in the title of docs/question-test.md?",
            (),
            False,
            None,
        ),
    ],
    "box_unnumbered.txt": [
        (
            1,
            'What should "1" start?',
            ("A blank screen", "The list of saved items", "Custom"),
            True,
            2,
        )
    ],
    "box_three_questions.txt": [
        (
            1,
            "Which screen should open first?",
            ("Home", "Settings", "Custom"),
            True,
            2,
        ),
        (
            2,
            "Should the tab bar keep its labels?",
            ("Yes", "No"),
            False,
            None,
        ),
        (
            3,
            "What colour should the accent be?",
            (),
            False,
            None,
        ),
    ],
    "box_custom_answer.txt": [
        (
            1,
            "What should the empty state say?",
            ("No items yet", "Custom"),
            True,
            1,
        )
    ],
}


class FixtureParsingTests(unittest.TestCase):
    def test_every_fixture_reads_as_a_box(self) -> None:
        for name, expected in BOXES.items():
            with self.subTest(fixture=name):
                box = parse(name)
                self.assertFalse(box.survey)
                self.assertEqual(box.total, len(expected))
                self.assertEqual(
                    [question.number for question in box.questions],
                    [row[0] for row in expected],
                )
                # The Submit row is drawn and not focused in every fixture.
                self.assertIs(box.submit_focused, False)

    def test_question_text_options_and_custom_row(self) -> None:
        for name, expected in BOXES.items():
            box = parse(name)
            for index, (number, text, labels, expanded, custom) in enumerate(expected):
                question = box.questions[index]
                with self.subTest(fixture=name, question=number):
                    self.assertEqual(question.number, number)
                    self.assertEqual(question.text, text)
                    self.assertEqual(question.labels, labels)
                    self.assertEqual(len(question.options), len(labels))
                    self.assertEqual(question.expanded, expanded)
                    self.assertEqual(question.custom_index, custom)

    def test_a_description_is_never_an_option(self) -> None:
        for name, expected in BOXES.items():
            box = parse(name)
            for (_, _, labels, _, _), question in zip(expected, box.questions):
                with self.subTest(fixture=name, question=question.number):
                    for option in question.options:
                        self.assertEqual(option.label, option.label.strip())
                        self.assertNotIn("\n", option.label)
                        for description in (
                            "Type your own answer",
                            "Just the tab bar and nothing on it",
                            "The plain fallback copy",
                        ):
                            self.assertNotEqual(option.label, description)
                    self.assertEqual(tuple(option.label for option in question.options), labels)

    def test_wrapped_descriptions_are_joined_onto_their_option(self) -> None:
        first = parse("box_numbered_two.txt").questions[0]
        self.assertEqual(
            first.options[0].description,
            "Polished, professional prose suitable for project documentation",
        )
        self.assertEqual(first.options[3].description, "Type your own answer")
        self.assertEqual(first.options[1].description, "")

        unnumbered = parse("box_unnumbered.txt").questions[0]
        self.assertEqual(
            unnumbered.options[0].description, "Just the tab bar and nothing on it"
        )
        self.assertEqual(
            unnumbered.options[1].description,
            "Opens with today's saves already loaded",
        )

    def test_the_footer_and_the_ad_banner_are_never_content(self) -> None:
        everything = ""
        for name in BOXES:
            box = parse(name)
            for question in box.questions:
                everything += "\n" + question.text
                for option in question.options:
                    everything += f"\n{option.label}\n{option.description}"
        for junk in (
            "Freebuff •",
            "navigate",
            "Enter select",
            "Close",
            "Submit",
            "esc to cancel",
            "tab to switch",
            "[Report",
            "wisprflow.ai",
            "Try for Free",
        ):
            self.assertNotIn(junk, everything)

    def test_the_last_option_never_takes_the_footer(self) -> None:
        last = parse("box_custom_answer.txt").questions[0].options[-1]
        self.assertEqual(last.label, "Custom")
        self.assertEqual(last.description, "", "the ↳ answer line is a hint")
        text = parse("box_custom_answer.txt").questions[0].answer_hint
        self.assertEqual(text, '"Nothing here yet — add your first item"')

    def test_box_borders_do_not_break_a_line(self) -> None:
        several = parse("box_three_questions.txt")
        for question in several.questions:
            with self.subTest(question=question.number):
                self.assertNotIn("┃", question.text)
                self.assertNotIn("│", question.text)
                self.assertFalse(question.text.startswith("Close"))
                for option in question.options:
                    self.assertNotIn("│", option.label)
                    self.assertNotIn("┃", option.description)

    def test_an_answered_question_is_marked_and_one_is_active(self) -> None:
        several = parse("box_three_questions.txt")
        self.assertEqual(several.questions[0].chosen_index, 0)
        self.assertEqual(
            several.questions[0].options[0].description,
            "The dashboard with today's items",
        )
        self.assertIsNotNone(several.active)
        assert several.active is not None
        self.assertEqual(several.active.number, 2)
        self.assertFalse(several.all_answered)

        committed = parse("box_custom_answer.txt")
        self.assertEqual(committed.questions[0].chosen_index, 1)
        self.assertTrue(committed.questions[0].answered)
        self.assertTrue(committed.all_answered)

    def test_the_unnumbered_header_gets_the_next_number(self) -> None:
        # Numbering is what /answer talks about, so an unnumbered row still
        # has to be addressable.
        box = parse("box_unnumbered.txt")
        self.assertEqual(box.questions[0].number, 1)
        self.assertEqual(box.questions[0].total, 1)
        self.assertEqual(box.questions[0].text, 'What should "1" start?')

    def test_a_pane_without_the_title_still_reads_when_the_box_is_there(self) -> None:
        pane = "\n".join(load("box_unnumbered.txt").splitlines()[2:])
        box = questions.parse_pane(pane)
        self.assertIsNotNone(box, "the questions are still on screen")
        assert box is not None
        self.assertEqual(box.total, 1)
        self.assertEqual(
            box.questions[0].labels,
            ("A blank screen", "The list of saved items", "Custom"),
        )

    def test_no_fixture_carries_a_secret(self) -> None:
        for name in BOXES:
            with self.subTest(fixture=name):
                self.assertNotIn("ghp_", load(name))
                self.assertNotIn("sk-", load(name))


class RoutingRunner(FreebuffRunner):
    """A runner whose box is a fixture and whose keys are only recorded."""

    def __init__(self, box: questions.QuestionBox) -> None:
        super().__init__(runner_settings())
        self.box = box
        self.chosen: int | None = None
        self.typed: list[str] = []

    async def _read_box(self, session: str) -> questions.QuestionBox | None:
        return self.box

    async def _choose(
        self, session: str, question: Question, target: int, log_path: Path
    ) -> str:
        self.chosen = target
        return "ok"

    async def _type_text(
        self, session: str, question: Question, text: str, log_path: Path
    ) -> str:
        self.typed.append(text)
        return "ok"


class AnswerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "job.log"

    async def test_an_option_number_chooses_that_circle(self) -> None:
        box = parse("box_numbered_two.txt")
        runner = RoutingRunner(box)
        outcome = await asyncio.wait_for(
            runner._apply("s", box.questions[0], "2", self.log_path), timeout=10
        )
        self.assertEqual(outcome, "ok")
        self.assertEqual(runner.chosen, 1, "answer 2 is the second circle (Casual)")
        self.assertIn("Casual", box.questions[0].labels[runner.chosen])
        self.assertEqual(runner.typed, [])

    async def test_the_last_number_is_the_custom_circle(self) -> None:
        box = parse("box_numbered_two.txt")
        question = box.questions[0]
        runner = RoutingRunner(box)
        outcome = await asyncio.wait_for(
            runner._apply("s", question, "4", self.log_path), timeout=10
        )
        self.assertEqual(outcome, "ok")
        self.assertEqual(runner.chosen, question.custom_index)

    async def test_free_text_takes_the_custom_row_and_is_typed(self) -> None:
        for name, number in (("box_numbered_two.txt", 1), ("box_unnumbered.txt", 1)):
            with self.subTest(fixture=name):
                box = parse(name)
                question = box.question(number)
                assert question is not None
                runner = RoutingRunner(box)
                outcome = await asyncio.wait_for(
                    runner._apply("s", question, "a friendlier tone", self.log_path),
                    timeout=10,
                )
                self.assertEqual(outcome, "ok")
                self.assertEqual(runner.chosen, question.custom_index)
                self.assertEqual(runner.typed, ["a friendlier tone"])

    async def test_free_text_on_a_committed_custom_row_is_still_typed(self) -> None:
        box = parse("box_custom_answer.txt")
        question = box.questions[0]
        runner = RoutingRunner(box)
        outcome = await asyncio.wait_for(
            runner._apply("s", question, "Nothing here yet", self.log_path),
            timeout=10,
        )
        self.assertEqual(outcome, "ok")
        self.assertEqual(runner.chosen, question.custom_index)
        self.assertEqual(runner.typed, ["Nothing here yet"])

    def test_a_number_that_names_no_circle_is_refused(self) -> None:
        labels = parse("box_numbered_two.txt").questions[0].labels
        self.assertEqual(questions.validate_answer("3", labels), "3")
        self.assertIsNone(questions.validate_answer("5", labels))
        self.assertIsNone(questions.validate_answer("0", labels))
        self.assertEqual(questions.validate_answer("keep it short", labels), "keep it short")


if __name__ == "__main__":
    unittest.main()
