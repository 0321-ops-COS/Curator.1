"""The multi-question box: parsing it, driving it, and failing safely.

No Discord and no tmux: the pane is rendered from a small model of the real
box, so the runner's closed loop is exercised against state that changes the
way a real one does, and a box that ignores its keys is exercised too.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import freebuff_runner  # noqa: E402
import jobs  # noqa: E402
import questions  # noqa: E402
from freebuff_runner import CommandResult, FreebuffRunner, TaskEnvironment  # noqa: E402
from manager import AnswerResult, ManagerUnavailable  # noqa: E402
from questions import Option, Question  # noqa: E402

BOX = """\
Keep coding from your phone   Ad [Report 1]
Freebuff for Android  Get the app
╭──── Some questions for you ────╮
│                        Close ✕ │
│ ▼ 1. Which tone should the project description use? (text may wrap onto
│      a second line)
│     ○ Formal
│       Polished, professional prose suitable for project documentation
│     ○ Casual
│     ○ Technical
│     ○ Custom
│ ▶ 2. What name should go in the title of docs/question-test.md? (Type your
│      own in the custom field, or pick one below.)
│     ↳ (click to answer)
│ ╭────────╮
│ │ Submit │   ↑↓ navigate • Enter select
│ ╰────────╯
╰────────────────────────────────╯
"""

SURVEY = """\
╭──────────────────────────────────────────────────╮
│  quick question 1/11                             │
│  How did you hear about Freebuff?                │
│  ✕ esc not now                                   │
╰──────────────────────────────────────────────────╯
"""


# Shaped like a GitHub token: anything this obvious must never leave the process.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"


def runner_settings(**overrides: object) -> SimpleNamespace:
    values = dict(
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
    values.update(overrides)
    return SimpleNamespace(**values)


def task_env() -> TaskEnvironment:
    return TaskEnvironment(
        branch="freebuff/deadbeef",
        worktree=Path("/nonexistent/worktree"),
        session="macbuilder-test",
        base_branch="freebuff/base",
        signature_at_start="a",
    )


class _SignatureRepo:
    """One change, then nothing: the idle detector's normal success signal."""

    def __init__(self, *values: str) -> None:
        self.values = list(values) or [""]
        self.calls = 0

    async def signature(self, cwd: Path) -> str:
        self.calls += 1
        return self.values[min(self.calls - 1, len(self.values) - 1)]


# --------------------------------------------------------------------- parser


class ParserTests(unittest.TestCase):
    def test_the_two_question_box_is_parsed(self) -> None:
        box = questions.parse_pane(BOX)
        self.assertIsNotNone(box)
        assert box is not None
        self.assertFalse(box.survey)
        self.assertEqual(box.total, 2)
        first, second = box.questions
        self.assertEqual((first.number, second.number), (1, 2))

        # Wrapped header text is rejoined onto one line.
        self.assertTrue(
            first.text.startswith("Which tone should the project description use?")
        )
        self.assertIn("a second line", first.text)
        self.assertEqual(
            first.labels, ("Formal", "Casual", "Technical", "Custom")
        )
        self.assertEqual(first.custom_index, 3)
        self.assertTrue(first.expanded)
        self.assertFalse(first.free_text)

        # The description belongs to Formal and is not an option of its own.
        self.assertEqual(
            first.options[0].description,
            "Polished, professional prose suitable for project documentation",
        )
        self.assertEqual(first.options[1].description, "")

    def test_the_collapsed_question_is_free_text(self) -> None:
        box = questions.parse_pane(BOX)
        assert box is not None
        second = box.questions[1]
        self.assertFalse(second.expanded)
        self.assertTrue(second.free_text)
        self.assertEqual(second.options, ())
        self.assertIn("docs/question-test.md", second.text)
        self.assertIn("pick one below", second.text)
        self.assertNotIn("Close", second.text)

    def test_the_ad_banner_and_close_button_are_not_content(self) -> None:
        box = questions.parse_pane(BOX)
        assert box is not None
        everything = "\n".join(q.text for q in box.questions)
        for label in box.questions[0].labels:
            everything += "\n" + label
        self.assertNotIn("Ad", everything)
        self.assertNotIn("for Android", everything)
        self.assertNotIn("[Report", everything)
        self.assertNotIn("✕", everything)
        self.assertNotIn("Enter select", everything)
        self.assertNotIn("Submit", everything)

    def test_the_survey_popup_is_never_a_task_question(self) -> None:
        box = questions.parse_pane(SURVEY)
        self.assertIsNotNone(box)
        assert box is not None
        self.assertTrue(box.survey)
        self.assertEqual(box.questions, ())

    def test_a_title_with_no_questions_comes_back_empty(self) -> None:
        box = questions.parse_pane("Some questions for you\n│ Close ✕ │\nthinking…\n")
        self.assertIsNotNone(box)
        assert box is not None
        self.assertEqual(box.questions, (), "the caller must fall back to the pane")
        self.assertFalse(box.survey)

    def test_a_pane_without_a_box_has_nothing(self) -> None:
        self.assertIsNone(questions.parse_pane(""))
        self.assertIsNone(questions.parse_pane("Freebuff is thinking...\n> "))

    def test_a_chosen_row_is_marked(self) -> None:
        box = questions.parse_pane(
            "Some questions for you\n"
            "▼ 1. Which tone?\n"
            "◉ Formal\n"
            "  ○ Casual\n"
        )
        assert box is not None
        self.assertEqual(box.questions[0].chosen_index, 0)
        self.assertTrue(box.all_answered)
        self.assertEqual(box.questions[0].chosen_index, 0)

    def test_a_focused_row_is_marked(self) -> None:
        box = questions.parse_pane(
            "Some questions for you\n▼ 1. Which tone?\n❯ ◯ Formal\n  ◯ Casual\n"
        )
        assert box is not None
        question = box.questions[0]
        self.assertEqual(question.cursor_index, 0)
        self.assertIsNone(question.chosen_index)

    def test_submit_focus_is_read_from_the_pane(self) -> None:
        unfocused = questions.parse_pane(BOX)
        assert unfocused is not None
        self.assertIs(unfocused.submit_focused, False)

        focused = questions.parse_pane(BOX.replace("│ │ Submit │", "│ ❯ Submit │"))
        assert focused is not None
        self.assertIs(focused.submit_focused, True)

        without = questions.parse_pane(
            "Some questions for you\n▼ 1. Which tone?\n○ Formal\n"
        )
        assert without is not None
        self.assertIsNone(without.submit_focused, "no row: the focus is unknowable")

    def test_redact_masks_a_token_shaped_secret(self) -> None:
        self.assertNotIn(TOKEN, questions.redact(f"deploy with {TOKEN} now"))
        self.assertIn("[redacted]", questions.redact(f"deploy with {TOKEN} now"))

    def test_active_prefers_the_expanded_unanswered_question(self) -> None:
        box = questions.parse_pane(BOX)
        assert box is not None
        assert box.active is not None
        self.assertEqual(box.active.number, 1)
        self.assertFalse(box.all_answered)

    def test_the_pane_tail_is_sanitized_and_truncated(self) -> None:
        box = questions.parse_pane(
            "\x1b[31mSome questions for you\x1b[0m\n"
            "Keep coding from your phone   Ad [Report 1]\n"
            + "x" * 5000
        )
        assert box is not None
        tail = box.sanitized_tail(limit=100)
        self.assertNotIn("\x1b", tail)
        self.assertNotIn("[Report", tail)
        self.assertLessEqual(len(tail), 120)
        self.assertTrue(tail.startswith("[...]"))

    def test_option_numbers_and_free_text(self) -> None:
        self.assertEqual(questions.option_number("2", 4), 2)
        self.assertIsNone(questions.option_number("9", 4))
        self.assertIsNone(questions.option_number("formal", 4))
        self.assertEqual(questions.flatten_answer("a\nb"), "a b")

    def test_validate_answer_rejects_a_number_that_names_no_option(self) -> None:
        self.assertEqual(questions.validate_answer("2", ("a", "b")), "2")
        self.assertIsNone(questions.validate_answer("9", ("a", "b")))
        self.assertEqual(
            questions.validate_answer("the home screen", ("a", "b")), "the home screen"
        )


# ------------------------------------------------------- a model of the box


class BoxQuestion:
    def __init__(self, number, text, options=(), *, expanded=True, free_text=False):
        self.number = number
        self.text = text
        self.options = [
            option if isinstance(option, tuple) else (option, "") for option in options
        ]
        self.expanded = expanded
        self.free_text = free_text
        self.chosen = None
        self.cursor = 0 if self.options else None
        self.hint = ""
        self.committed = False
        self.advanced = False

    @property
    def answered(self) -> bool:
        if self.chosen is not None:
            return True
        return bool(self.free_text and self.committed)


class FakeBox:
    """A tiny terminal that renders itself like the real box and applies keys."""

    def __init__(
        self,
        questions: list[BoxQuestion],
        *,
        obey: bool = True,
        draw_cursor: bool = True,
        advance_on_choose: bool = True,
        submit_focus: str = "after_answer",
        collapse_to_hint: bool = False,
        colour_focus: bool = False,
    ) -> None:
        self.collapse_to_hint = collapse_to_hint
        # Real Freebuff: the plain text of Submit is identical focused or not.
        self.colour_focus = colour_focus
        self.questions = questions
        self.obey = obey
        self.draw_cursor = draw_cursor
        self.advance_on_choose = advance_on_choose
        # after_answer | after_down | never
        self.submit_focus = submit_focus
        self.submit_focused = False
        self.stray_enters = 0
        self.closed = False

    @property
    def active(self) -> BoxQuestion | None:
        """The question the box is on. None means the focus reached Submit."""
        for question in self.questions:
            if question.expanded and not question.answered:
                return question
        for question in self.questions:
            if not question.answered:
                return question  # collapsed: Enter opens it first
        for question in self.questions:
            if question.expanded:
                return question
        return None

    def render(self) -> str:
        if self.closed:
            return "> done\n"
        lines = [
            "Keep coding from your phone   Ad [Report 1]",
            "Freebuff for Android  Get the app",
            "╭──── Some questions for you ────╮",
            "│                        Close ✕ │",
        ]
        for question in self.questions:
            mark = "▼" if question.expanded else "▶"
            lines.append(f"│ {mark} {question.number}. {question.text}")
            if (
                self.collapse_to_hint
                and not question.expanded
                and question.chosen is not None
            ):
                # Real Freebuff: a committed question collapses to the value
                # it took and no longer lists its rows.
                label = question.options[question.chosen][0]
                lines.append(f'│   ↳ "{label}"')
                continue
            for index, (label, description) in enumerate(question.options):
                if question.chosen == index:
                    circle = "◉"
                elif question.expanded and question.cursor == index and self.draw_cursor:
                    circle = "❯ ◯"
                else:
                    circle = "◯"
                lines.append(f"│     {circle} {label}")
                if description:
                    lines.append(f"│       {description}")
            if question.hint:
                lines.append(f"│     ↳ {question.hint}")
            elif question.free_text and not question.options:
                lines.append("│     ↳ (click to answer)")
        if self.submit_focus == "no_row":
            # A box whose Submit row never made it into the capture.
            lines.append("│      ↑↓ navigate • Enter select")
        elif self.submit_focused and not self.colour_focus:
            lines.append("│ ❯ Submit │   ↑↓ navigate • Enter select")
        else:
            lines.append("│ │ Submit │   ↑↓ navigate • Enter select")
        lines += [
            "│ ╭────────╮",
            "│ ╰────────╯",
            "╰────────────────────────────────╯",
        ]
        return "\n".join(lines)

    def press(self, key: str) -> None:
        if self.closed or not self.obey:
            return
        question = self.active
        if question is None:
            if key == "Down" and not self.submit_focused:
                # Down never selects: it is how the focus reaches Submit.
                self.submit_focused = self.submit_focus == "after_down"
            elif key == "Enter":
                if self.submit_focused:
                    self.closed = True
                else:
                    # Enter with the focus anywhere else is exactly the stray
                    # Enter that must never reach the box.
                    self.stray_enters += 1
            return
        if key in ("Down", "Up"):
            if question.options and question.expanded:
                step = 1 if key == "Down" else -1
                question.cursor = (question.cursor + step) % len(question.options)
            elif not self.submit_focused:
                # Nothing left to choose: the next Down reaches Submit.
                self.submit_focused = self.submit_focus == "after_down"
        elif key == "Enter":
            if not question.expanded:
                # Enter opens a collapsed question before anything can be typed.
                question.expanded = True
            elif question.options:
                # A row can be chosen again: that is how a wrong pick is fixed.
                question.chosen = question.cursor
                if self.advance_on_choose and not question.advanced:
                    question.advanced = True
                    self._advance(question)
            elif question.free_text and question.hint and not question.committed:
                question.committed = True
                self._advance(question)

    def type_text(self, text: str) -> None:
        if self.closed or not self.obey:
            return
        question = self.active
        if question is None or question.options:
            return
        question.hint = text

    def _advance(self, answered: BoxQuestion) -> None:
        answered.expanded = False
        for question in self.questions:
            if not question.answered:
                question.expanded = True
                question.cursor = 0
                return
        self.submit_focused = self.submit_focus == "after_answer"


class FakeTerminal(FreebuffRunner):
    """A runner whose pane is that model, so no tmux is involved."""

    def __init__(self, settings: object, box: FakeBox) -> None:
        super().__init__(settings)
        self.box = box
        self.keys: list[str] = []
        self.pasted: list[str] = []
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    async def _tmux(self, *args: str, input_bytes: bytes | None = None) -> CommandResult:
        self.calls.append((args, input_bytes))
        if args and args[0] == "send-keys":
            self.keys.append(args[-1])
            self.box.press(args[-1])
        return CommandResult(0, "", "")

    async def _session_alive(self, session: str) -> bool:
        return True

    async def _pane(self, session: str) -> str:
        return self.box.render()

    async def _send_text(self, session: str, text: str) -> None:
        self.pasted.append(text)
        self.box.type_text(text)
        self.box.press("Enter")


class StaticTerminal(FreebuffRunner):
    """A runner whose pane never changes: for boxes that will not parse."""

    def __init__(self, settings: object, pane: str) -> None:
        super().__init__(settings)
        self.pane = pane
        self.keys: list[str] = []

    async def _tmux(self, *args: str, input_bytes: bytes | None = None) -> CommandResult:
        if args and args[0] == "send-keys":
            self.keys.append(args[-1])
        return CommandResult(0, "", "")

    async def _session_alive(self, session: str) -> bool:
        return True

    async def _pane(self, session: str) -> str:
        return self.pane


class BoxLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "job.log"
        patcher = mock.patch.object(freebuff_runner, "KEY_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def two_questions(self) -> FakeBox:
        return FakeBox(
            [
                BoxQuestion(
                    1,
                    "Which tone should the project description use?",
                    ["Formal", ("Casual", ""), "Technical", "Custom"],
                ),
                BoxQuestion(
                    2,
                    "What name should go in the title of docs/question-test.md?",
                    expanded=False,
                    free_text=True,
                ),
            ]
        )

    async def test_a_two_question_box_is_answered_one_at_a_time(self) -> None:
        runner = FakeTerminal(runner_settings(), self.two_questions())
        runner.repo = _SignatureRepo("b")
        asked: list[tuple[int, int, tuple[str, ...]]] = []

        async def on_question(question: Question) -> str:
            asked.append((question.number, question.total, question.labels))
            return "2" if question.number == 1 else "question-test.md"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )

        self.assertEqual(reason, "idle")
        self.assertEqual([entry[0] for entry in asked], [1, 2])
        self.assertEqual(asked[0][1], 2)
        self.assertEqual(asked[0][2], ("Formal", "Casual", "Technical", "Custom"))
        # The option the answer named is the one the box recorded.
        self.assertEqual(runner.box.questions[0].chosen, 1)
        self.assertEqual(runner.box.questions[1].hint, "question-test.md")
        self.assertTrue(runner.box.closed, "Submit must close the box")
        self.assertIn("Down", runner.keys)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[question 1 of 2]", log)
        self.assertIn("[question 2 of 2]", log)
        self.assertIn("[Submit closed the box]", log)
        self.assertEqual(runner.box.stray_enters, 0)

    async def test_free_text_goes_through_the_paste_path(self) -> None:
        box = FakeBox(
            [BoxQuestion(1, "What name?", expanded=False, free_text=True)]
        )
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "docs/question-test.md"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        self.assertEqual(runner.pasted, ["docs/question-test.md"])
        self.assertEqual(box.questions[0].hint, "docs/question-test.md")
        self.assertTrue(box.closed)

    async def test_a_box_that_ignores_its_keys_fails_the_job(self) -> None:
        runner = FakeTerminal(runner_settings(), self.two_questions_with_keys_ignored())
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "2"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-failed")
        self.assertIn("Escape", runner.keys, "a failed answer must be dismissed")
        self.assertIn(
            "was not confirmed",
            self.log_path.read_text(encoding="utf-8"),
        )

    def two_questions_with_keys_ignored(self) -> FakeBox:
        box = self.two_questions()
        box.obey = False
        return box

    async def test_an_unanswered_question_expires_with_esc(self) -> None:
        runner = FakeTerminal(
            runner_settings(question_wait_seconds=0.2), self.two_questions()
        )
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> None:
            return None

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-timeout")
        self.assertIn("Escape", runner.keys)
        self.assertIsNone(runner.pending_question)
        self.assertFalse(await runner.submit_answer("1"))

    async def test_the_idle_timer_pauses_while_a_question_is_pending(self) -> None:
        runner = FakeTerminal(
            runner_settings(freebuff_idle_seconds=0.1), self.two_questions()
        )
        runner.repo = _SignatureRepo("b")
        started = asyncio.Event()

        async def on_question(question: Question) -> str | None:
            started.set()
            # Question 1 is the one the owner answers by hand; question 2 comes
            # back answered so the loop can finish once /answer arrives.
            return None if question.number == 1 else "x.md"

        task = asyncio.create_task(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question)
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.6)  # several idle windows with the question open
        self.assertFalse(task.done(), "a waiting job is not a finished job")
        self.assertTrue(await runner.submit_answer("1"))
        self.assertEqual(await asyncio.wait_for(task, timeout=10), "idle")

    async def test_a_box_that_does_not_parse_is_posted_and_never_idles(self) -> None:
        pane = "Some questions for you\n│ Close ✕ │\nthinking…\n"
        runner = StaticTerminal(
            runner_settings(question_wait_seconds=0.3, freebuff_idle_seconds=0.05),
            pane,
        )
        runner.repo = _SignatureRepo("b")
        posted: list[str] = []

        async def on_question(question: Question) -> None:
            raise AssertionError("nothing here can be answered")

        async def on_pane(tail: str) -> None:
            posted.append(tail)

        reason = await asyncio.wait_for(
            runner.wait_for_changes(
                task_env(), self.log_path, on_question=on_question, on_pane=on_pane
            ),
            timeout=15,
        )
        self.assertEqual(reason, "question-timeout")
        self.assertEqual(len(posted), 1, "the owner must see the pane exactly once")
        self.assertIn("Some questions for you", posted[0])
        self.assertIn("Escape", runner.keys)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("no questions parsed", log)
        self.assertIn("Some questions for you", log)

    async def test_the_survey_popup_is_dismissed_and_never_asked(self) -> None:
        runner = StaticTerminal(runner_settings(freebuff_idle_seconds=0.1), SURVEY)
        runner.repo = _SignatureRepo("b")
        asked: list[Question] = []

        async def on_question(question: Question) -> str | None:
            asked.append(question)
            return None

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        self.assertEqual(asked, [])
        self.assertEqual(runner.keys.count("Escape"), 1)

    async def test_question_debug_writes_the_pane_after_every_key(self) -> None:
        runner = FakeTerminal(runner_settings(question_debug=True), self.two_questions())
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "2" if question.number == 1 else "x.md"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[pane after Down]", log)
        self.assertIn("[pane after Enter]", log)


    # The two Submit rows exactly as `capture-pane -e` returned them on the
    # Mac (job 20261031-113115-13391ac2): ESC[1m in front of the word is focus.
    UNFOCUSED_SUBMIT = (
        "│\x1b[38;2;172;179;191m│\x1b[38;2;255;255;255m  "
        "\x1b[38;2;172;179;191mSubmit\x1b[38;2;255;255;255m  "
        "\x1b[38;2;172;179;191m│\x1b[38;2;255;255;255m    ↑↓ navigate"
    )
    FOCUSED_SUBMIT = (
        "││\x1b[38;2;255;255;255m  \x1b[1m\x1b[38;2;158;252;98mSubmit"
        "\x1b[0m\x1b[38;2;255;255;255m  \x1b[38;2;158;252;98m│"
        "\x1b[38;2;255;255;255m    ↑↓ navigate"
    )

    def test_the_real_colour_codes_tell_focused_from_unfocused_submit(self) -> None:
        from freebuff_runner import styled_submit_focus

        self.assertIs(styled_submit_focus(self.UNFOCUSED_SUBMIT), False)
        self.assertIs(styled_submit_focus(self.FOCUSED_SUBMIT), True)
        self.assertIsNone(styled_submit_focus("no button on this pane"))
        self.assertIsNone(styled_submit_focus(""))

    def test_bold_ended_before_the_word_is_not_focus(self) -> None:
        from freebuff_runner import styled_submit_focus

        reset = "\x1b[1mx\x1b[0m \x1b[38;2;1;2;3mSubmit"
        self.assertIs(styled_submit_focus(reset), False)

    def colour_terminal(self, box: FakeBox) -> FakeTerminal:
        spec = self

        class Coloured(FakeTerminal):
            async def _styled_pane(self, session: str) -> str | None:
                return (
                    spec.FOCUSED_SUBMIT
                    if self.box.submit_focused
                    else spec.UNFOCUSED_SUBMIT
                )

        runner = Coloured(runner_settings(), box)
        runner.repo = _SignatureRepo("b")
        return runner

    async def test_submit_already_focused_by_colour_gets_one_enter_and_no_down(self) -> None:
        # Freebuff moves the focus onto Submit itself after the last answer, and
        # the plain text cannot show it: only the colour does.
        box = self.single_question(submit_focus="after_answer", colour_focus=True)
        runner = self.colour_terminal(box)

        async def on_question(question: Question) -> str:
            return "1"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        self.assertTrue(box.closed)
        self.assertEqual(box.stray_enters, 0)
        self.assertEqual(runner.keys[-1:], ["Enter"])
        self.assertNotIn("Down", runner.keys[-2:], "no Down once colour shows focus")

    async def test_colour_focus_that_never_arrives_sends_no_enter(self) -> None:
        box = self.single_question(submit_focus="never", colour_focus=True)
        runner = self.colour_terminal(box)

        async def on_question(question: Question) -> str:
            return "1"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-failed")
        self.assertEqual(box.stray_enters, 0)

    def single_question(self, **overrides: object) -> FakeBox:
        return FakeBox(
            [BoxQuestion(1, "Which screen?", ["Home", "Settings", "Profile"])],
            **overrides,
        )

    async def test_submit_is_reached_with_one_down_and_no_stray_enter(self) -> None:
        box = self.single_question(submit_focus="after_down")
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "1"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        self.assertTrue(box.closed)
        self.assertEqual(box.stray_enters, 0)
        # Down (never a selection), then Enter only because Submit then had it.
        self.assertEqual(runner.keys[-2:], ["Down", "Enter"])

    async def test_submit_that_is_not_on_the_pane_fails_without_enter(self) -> None:
        box = self.single_question(submit_focus="no_row")
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "1"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-failed")
        self.assertEqual(box.stray_enters, 0)
        self.assertIn("Escape", runner.keys)
        self.assertIn("no Submit row", self.log_path.read_text(encoding="utf-8"))

    async def test_submit_that_never_takes_the_focus_fails_without_enter(self) -> None:
        box = self.single_question(submit_focus="never")
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "1"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-failed")
        self.assertEqual(
            box.stray_enters, 0, "Enter must never be sent while an option could be focused"
        )
        self.assertIn("Escape", runner.keys)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("Submit never took the focus", log)
        self.assertNotIn("Enter on Submit", log)

    def real_box(self, cursor: int = 0) -> FakeBox:
        # What the live capture showed: no focus drawn, Enter commits the
        # focused row, the question collapses to `↳ "<label>"`.
        box = FakeBox(
            [
                BoxQuestion(1, "Which tone?", ["Formal", "Casual", "Technical"]),
                BoxQuestion(2, "Which name?", ["A", "B"], expanded=False),
            ],
            draw_cursor=False,
            collapse_to_hint=True,
        )
        box.questions[0].cursor = cursor
        return box

    async def _choose_on(self, box: FakeBox, number: int) -> tuple[str, FakeTerminal]:
        runner = FakeTerminal(runner_settings(), box)
        parsed = questions.parse_pane(box.render())
        assert parsed is not None and parsed.active is not None
        outcome = await runner._choose("s", parsed.active, number - 1, self.log_path)
        return outcome, runner

    async def test_row_one_is_picked_with_a_single_enter(self) -> None:
        box = self.real_box()
        outcome, runner = await self._choose_on(box, 1)
        self.assertEqual(outcome, "ok")
        self.assertEqual(box.questions[0].chosen, 0)
        self.assertEqual(runner.keys, ["Enter"])

    async def test_a_later_row_is_reached_with_down_before_the_only_enter(self) -> None:
        box = self.real_box()
        outcome, runner = await self._choose_on(box, 3)
        self.assertEqual(outcome, "ok")
        self.assertEqual(box.questions[0].chosen, 2)
        self.assertEqual(runner.keys, ["Down", "Down", "Enter"])

    async def test_a_focus_that_does_not_start_on_row_one_fails_safely(self) -> None:
        box = self.real_box(cursor=2)  # invisible; Down wraps to row 1
        outcome, runner = await self._choose_on(box, 2)
        self.assertEqual(outcome, "mismatch")
        self.assertEqual(runner.keys, ["Down", "Enter"], "no second Enter, no guessing")
        self.assertIn("instead of", self.log_path.read_text(encoding="utf-8"))

    async def test_a_probe_that_moves_the_box_on_fails_instead_of_typing(self) -> None:
        box = self.two_questions()
        box.draw_cursor = False
        box.questions[0].cursor = 2  # the probe will land on row 3
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "2"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "question-failed")
        self.assertEqual(box.questions[1].hint, "", "question 2 must not be touched")
        self.assertIn("Escape", runner.keys)
        self.assertIn(
            "the box moved on", self.log_path.read_text(encoding="utf-8")
        )

    async def test_a_stopped_job_dismisses_the_box_and_forgets_the_question(self) -> None:
        runner = FakeTerminal(runner_settings(), self.two_questions())
        runner.repo = _SignatureRepo("b")
        env = task_env()
        waiting = asyncio.Event()

        async def on_question(question: Question) -> str | None:
            waiting.set()
            return None

        task = asyncio.create_task(
            runner.wait_for_changes(env, self.log_path, on_question=on_question)
        )
        self.addCleanup(task.cancel)
        await asyncio.wait_for(waiting.wait(), timeout=5)
        self.assertIsNotNone(runner.pending_question)

        await runner.abort(env)

        self.assertIsNone(
            runner.pending_question, "a late /answer must find nothing pending"
        )
        self.assertFalse(await runner.submit_answer("1"))
        self.assertIn("Escape", runner.keys)
        self.assertEqual(await asyncio.wait_for(task, timeout=10), "question-timeout")
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("the job stopped while question 1 was open", log)
        names = [args[0] for args, _data in runner.calls]
        self.assertLess(
            names.index("send-keys"), names.index("kill-session"), "Esc before the kill"
        )

    async def test_a_second_question_is_refused_while_one_is_open(self) -> None:
        runner = FakeTerminal(runner_settings(), self.two_questions())
        runner.repo = _SignatureRepo("b")
        live = Question(
            number=1, total=2, text="First?", options=(Option("a"), Option("b"))
        )
        runner._question = live
        box = questions.parse_pane(BOX)
        assert box is not None

        answer = await runner._ask(
            task_env(), box, box.questions[1], self.log_path, None
        )

        self.assertIsNone(answer)
        self.assertIs(runner.pending_question, live, "the open question keeps its owner")
        self.assertIn(
            "was still open; refusing it", self.log_path.read_text(encoding="utf-8")
        )

    async def test_free_text_is_only_ever_an_argument_list(self) -> None:
        box = FakeBox(
            [BoxQuestion(1, "What name?", expanded=False, free_text=True)]
        )
        runner = FakeTerminal(runner_settings(), box)
        runner.repo = _SignatureRepo("b")
        answer = 'He said "hi"; $(rm -rf ~); `id`; echo pwned\nsecond line'

        async def on_question(question: Question) -> str:
            return answer

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        flattened = 'He said "hi"; $(rm -rf ~); `id`; echo pwned second line'
        self.assertEqual(runner.pasted, [flattened])
        for args, data in runner.calls:
            self.assertTrue(all(isinstance(part, str) for part in args))
            for part in args:
                self.assertNotIn("rm -rf", part, "the answer must never reach an argv")
                self.assertNotIn("pwned", part)
            if data is not None:
                self.assertEqual(args[0], "load-buffer")
        sent = [args for args, _data in runner.calls if args[0] == "send-keys"]
        self.assertTrue(sent)
        for args in sent:
            self.assertIn(args[-1], {"Enter", "Down", "Up", "Escape"})

    async def test_credentials_never_reach_the_job_log(self) -> None:
        box = self.two_questions()
        box.questions[0].text = f"Which key does the deploy use? {TOKEN}"
        box.questions[0].options[0] = (f"Use {TOKEN}", "")
        runner = FakeTerminal(runner_settings(question_debug=True), box)
        runner.repo = _SignatureRepo("b")

        async def on_question(question: Question) -> str:
            return "2" if question.number == 1 else "x.md"

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_question=on_question),
            timeout=15,
        )
        self.assertEqual(reason, "idle")
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[pane after Down]", log, "debug must still dump the pane")
        self.assertIn("[question 1 of 2]", log)
        self.assertNotIn(TOKEN, log)
        self.assertIn("[redacted]", log)

    async def test_the_pane_fallback_post_is_redacted(self) -> None:
        pane = f"Some questions for you\n│ Close ✕ │\nleaked {TOKEN}\n"
        runner = StaticTerminal(
            runner_settings(question_wait_seconds=0.3, freebuff_idle_seconds=0.05), pane
        )
        runner.repo = _SignatureRepo("b")
        posted: list[str] = []

        async def on_pane(tail: str) -> None:
            posted.append(tail)

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path, on_pane=on_pane),
            timeout=15,
        )
        self.assertEqual(reason, "question-timeout")
        self.assertEqual(len(posted), 1)
        self.assertNotIn(TOKEN, posted[0])
        self.assertNotIn(TOKEN, self.log_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- job runner


class _QuestionFreebuff:
    """A runner stand-in that stops on a question box."""

    def __init__(self, reason: str = "question-timeout") -> None:
        self.reason = reason
        self.aborted = False
        self.env: TaskEnvironment | None = None
        self.kwargs: dict[str, object] = {}

    async def start_task(
        self, *, job_id: str, task_text: str, log_path: Path, kind: str = "task"
    ) -> TaskEnvironment:
        return task_env()

    async def wait_for_changes(
        self, env: TaskEnvironment, log_path: Path, **kwargs: object
    ) -> str:
        self.kwargs = kwargs
        return self.reason

    async def collect(self, env: TaskEnvironment, max_diff_lines: int) -> object:
        raise AssertionError("nothing is collected after a question box")

    async def abort(self, env: TaskEnvironment | None = None) -> None:
        self.aborted = True
        self.env = env


class _NoManager:
    enabled = False


def make_job(tmp: str) -> jobs.Job:
    return jobs.Job(
        id="20261030-000000-deadbeef",
        kind="task",
        title="make the card bigger",
        channel_id=1,
        requester_id=2,
        log_path=Path(tmp) / "job.log",
        text="make the card bigger",
    )


class JobStoppedOnBoxTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, freebuff: _QuestionFreebuff) -> tuple[jobs.Job, list[str]]:
        posted: list[str] = []

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            posted.append(summary)

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            raise AssertionError("a job stopped on a box has no review")

        runner = jobs.JobRunner(
            SimpleNamespace(
                max_diff_lines=400,
                max_revise_rounds=0,
                question_wait_seconds=1800,
                question_auto_answer=False,
            ),
            _NoManager(),
            freebuff,
            post_job=post_job,
            post_review=post_review,
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(tmp)
            await runner.run(job)
        return job, posted

    async def test_an_expired_question_fails_the_job(self) -> None:
        freebuff = _QuestionFreebuff("question-timeout")
        job, posted = await self.run_job(freebuff)
        self.assertEqual(job.status, "failed")
        self.assertTrue(freebuff.aborted)
        self.assertIn("QUESTION_WAIT_SECONDS", posted[0])
        self.assertIn("1800s", posted[0])
        self.assertIn("Esc", posted[0])

    async def test_a_mismatched_box_fails_the_job(self) -> None:
        freebuff = _QuestionFreebuff("question-failed")
        job, posted = await self.run_job(freebuff)
        self.assertEqual(job.status, "failed")
        self.assertTrue(freebuff.aborted)
        self.assertIn("did not show the answer", posted[0])
        self.assertIn("QUESTION_DEBUG", posted[0])

    async def test_bot_shutdown_dismisses_an_open_box(self) -> None:
        freebuff = _QuestionFreebuff("question-timeout")
        runner = jobs.JobRunner(
            SimpleNamespace(
                max_diff_lines=400,
                max_revise_rounds=0,
                question_wait_seconds=1800,
                question_auto_answer=False,
            ),
            _NoManager(),
            freebuff,
            post_job=lambda *a: asyncio.sleep(0),
            post_review=lambda *a: asyncio.sleep(0),
        )
        env = task_env()
        runner._running_env = env

        await runner.shutdown()

        self.assertTrue(freebuff.aborted, "shutdown must stop the session")
        self.assertIs(freebuff.env, env)

    async def test_the_runner_is_given_the_question_and_pane_callbacks(self) -> None:
        freebuff = _QuestionFreebuff("question-timeout")
        with tempfile.TemporaryDirectory() as tmp:
            runner = jobs.JobRunner(
                SimpleNamespace(
                    max_diff_lines=400,
                    max_revise_rounds=0,
                    question_wait_seconds=1800,
                ),
                _NoManager(),
                freebuff,
                post_job=lambda *a: asyncio.sleep(0),
                post_review=lambda *a: asyncio.sleep(0),
                post_pane=lambda *a: asyncio.sleep(0),
            )
            await runner.run(make_job(tmp))
        self.assertIn("on_question", freebuff.kwargs)
        self.assertIn("on_pane", freebuff.kwargs)
        self.assertTrue(callable(freebuff.kwargs["on_pane"]))


class AutoAnswerTests(unittest.IsolatedAsyncioTestCase):
    class _Manager:
        enabled = True

        def __init__(self, result: object) -> None:
            self.result = result
            self.calls = 0
            self.options: object = None

        async def answer_question(self, **kwargs: object) -> object:
            self.calls += 1
            self.options = kwargs.get("options")
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    async def run_question(self, *, enabled: bool, result: object):
        manager = self._Manager(result)
        posted: list[Question] = []

        async def post_question(job: jobs.Job, question: Question) -> bool:
            posted.append(question)
            return True

        runner = jobs.JobRunner(
            SimpleNamespace(
                question_auto_answer=enabled, max_diff_lines=400, max_revise_rounds=2
            ),
            manager,
            _QuestionFreebuff(),
            post_job=lambda *a: asyncio.sleep(0),
            post_review=lambda *a: asyncio.sleep(0),
            post_question=post_question,
        )
        question = Question(
            number=1,
            total=2,
            text="Which screen?",
            options=(Option("Home"), Option("Settings", "The settings tab")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            answer = await runner._on_question(make_job(tmp), question)
        return answer, posted, manager

    async def test_auto_answer_is_off_by_default(self) -> None:
        answer, posted, manager = await self.run_question(
            enabled=False, result=AnswerResult(answer="1", confidence="high")
        )
        self.assertIsNone(answer)
        self.assertEqual(len(posted), 1)
        self.assertEqual(manager.calls, 0)

    async def test_a_high_confidence_answer_is_used(self) -> None:
        answer, posted, manager = await self.run_question(
            enabled=True, result=AnswerResult(answer="2", confidence="high")
        )
        self.assertEqual(answer, "2")
        self.assertEqual(posted, [])
        self.assertEqual(
            manager.options, ["Home", "Settings — The settings tab"]
        )

    async def test_a_medium_confidence_answer_goes_to_the_owner(self) -> None:
        answer, posted, _manager = await self.run_question(
            enabled=True, result=AnswerResult(answer="2", confidence="medium")
        )
        self.assertIsNone(answer)
        self.assertEqual(len(posted), 1)

    async def test_an_answer_that_names_no_option_goes_to_the_owner(self) -> None:
        answer, posted, _manager = await self.run_question(
            enabled=True, result=AnswerResult(answer="9", confidence="high")
        )
        self.assertIsNone(answer)
        self.assertEqual(len(posted), 1)

    async def test_a_rate_limited_manager_falls_back_to_the_owner(self) -> None:
        answer, posted, _manager = await self.run_question(
            enabled=True, result=ManagerUnavailable("manager request failed: HTTP 429")
        )
        self.assertIsNone(answer)
        self.assertEqual(len(posted), 1)


# ------------------------------------------------------------------- Discord


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=7)
        self.channel_id = 99
        self.response = _FakeResponse()


class _FakeChannel:
    def __init__(self) -> None:
        self.embeds: list[object] = []

    async def send(self, *, embed: object, **kwargs: object) -> None:
        self.embeds.append(embed)


class AnswerCommandTests(unittest.IsolatedAsyncioTestCase):
    def instance(self, freebuff: object, *, allowed: bool = True) -> bot.MacBuilderBot:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
        instance.freebuff = freebuff

        async def check(_interaction: object) -> bool:
            return allowed

        instance.require_allowed = check
        return instance

    async def test_answer_with_no_pending_question_says_so(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        instance = self.instance(
            SimpleNamespace(pending_question=None, submit_answer=submit)
        )
        interaction = _FakeInteraction()

        await instance.cmd_answer(interaction, "2")

        self.assertEqual(submitted, [])
        self.assertIn("no pending question", interaction.response.messages[0].lower())

    async def test_answer_sends_an_option_number(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        pending = Question(
            number=1,
            total=2,
            text="Which screen?",
            options=(Option("Home"), Option("Settings")),
        )
        instance = self.instance(
            SimpleNamespace(pending_question=pending, submit_answer=submit)
        )
        interaction = _FakeInteraction()

        await instance.cmd_answer(interaction, " 2 ")

        self.assertEqual(submitted, ["2"])
        self.assertIn("option 2", interaction.response.messages[0])

    async def test_a_number_outside_the_options_is_refused(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        pending = Question(
            number=2, total=2, text="Which?", options=(Option("A"), Option("B"))
        )
        instance = self.instance(
            SimpleNamespace(pending_question=pending, submit_answer=submit)
        )
        interaction = _FakeInteraction()

        await instance.cmd_answer(interaction, "7")

        self.assertEqual(submitted, [])
        self.assertIn("has 2 options", interaction.response.messages[0])

    async def test_free_text_with_a_custom_row_says_custom(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        pending = Question(
            number=1,
            total=1,
            text="Which name?",
            options=(Option("A"), Option("Custom")),
        )
        instance = self.instance(
            SimpleNamespace(pending_question=pending, submit_answer=submit)
        )
        interaction = _FakeInteraction()

        await instance.cmd_answer(interaction, "docs/x.md")

        self.assertEqual(submitted, ["docs/x.md"])
        self.assertIn("Custom field", interaction.response.messages[0])

    async def test_a_denied_user_learns_nothing_about_a_pending_question(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        pending = Question(number=1, total=1, text="Which?", options=(Option("A"),))
        sent: list[list[str]] = []
        for state in (
            SimpleNamespace(pending_question=pending, submit_answer=submit),
            SimpleNamespace(pending_question=None, submit_answer=submit),
        ):
            instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
            instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
            instance.freebuff = state

            async def deny(interaction: _FakeInteraction) -> bool:
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return False

            instance.require_allowed = deny
            interaction = _FakeInteraction()
            await instance.cmd_answer(interaction, "1")
            sent.append(interaction.response.messages)

        self.assertEqual(sent[0], ["Not allowed."])
        self.assertEqual(
            sent[0], sent[1], "a denied user must not be able to tell if one is pending"
        )
        self.assertEqual(submitted, [])

    async def test_a_user_off_the_allowlist_cannot_answer(self) -> None:
        submitted: list[str] = []

        async def submit(text: str) -> bool:
            submitted.append(text)
            return True

        pending = Question(number=1, total=1, text="Which?", options=(Option("A"),))
        instance = self.instance(
            SimpleNamespace(pending_question=pending, submit_answer=submit),
            allowed=False,
        )
        interaction = _FakeInteraction()

        await instance.cmd_answer(interaction, "1")

        self.assertEqual(submitted, [])
        self.assertEqual(interaction.response.messages, [])


class QuestionPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_post_names_the_question_of_the_total(self) -> None:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
        channel = _FakeChannel()

        def target(_channel_id: int) -> _FakeChannel:
            return channel

        instance._target_channel = target  # type: ignore[method-assign]
        question = Question(
            number=1,
            total=2,
            text="Which tone?",
            options=(
                Option("Formal", "Polished, professional prose"),
                Option("Casual"),
                Option("Custom"),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(await instance._post_question(make_job(tmp), question))

        embed = channel.embeds[0]
        self.assertIn("question 1 of 2", embed.title)
        self.assertIn("`1` Formal", embed.description)
        self.assertIn("Polished, professional prose", embed.description)
        self.assertIn("/answer <number>", embed.description)

    async def test_an_unreadable_box_is_posted_as_a_pane(self) -> None:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
        channel = _FakeChannel()
        instance._target_channel = lambda _id: channel  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(
                await instance._post_pane(make_job(tmp), "Some questions for you\n```")
            )
        embed = channel.embeds[0]
        self.assertIn("could not read", embed.title)
        self.assertIn("Some questions for you", embed.description)


class OneBoxAtATimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_job_runs_so_only_one_box_can_be_open(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def handler(job: jobs.Job) -> None:
            order.append(job.id)
            if job.id == "one":
                started.set()
                await release.wait()

        queue = jobs.JobQueue(handler)
        queue.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                first = make_job(tmp)
                first.id = "one"
                second = make_job(tmp)
                second.id = "two"
                await queue.submit(first)
                await asyncio.wait_for(started.wait(), timeout=5)
                await queue.submit(second)
                await asyncio.sleep(0.1)
                self.assertEqual(queue.running.id, "one")
                self.assertEqual(
                    second.status, "queued", "a second box waits its turn"
                )
                release.set()
                for _ in range(100):
                    if second.status == "done":
                        break
                    await asyncio.sleep(0.05)
        finally:
            await queue.stop()
        self.assertEqual(order, ["one", "two"])


class RedactionSinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_discord_question_post_redacts_credentials(self) -> None:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
        channel = _FakeChannel()
        instance._target_channel = lambda _id: channel  # type: ignore[method-assign]
        question = Question(
            number=1,
            total=1,
            text=f"Deploy with {TOKEN}?",
            options=(Option(f"Use {TOKEN}", f"the value is {TOKEN}"),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(await instance._post_question(make_job(tmp), question))
        embed = channel.embeds[0]
        self.assertNotIn(TOKEN, embed.description)
        self.assertIn("[redacted]", embed.description)


if __name__ == "__main__":
    unittest.main()
