"""Read an interactive Freebuff question box out of a captured tmux pane.

Freebuff sometimes stops on a box of numbered questions instead of finishing:

    ╭──── Some questions for you ────╮
    │                        Close ✕ │
    │ ▼ 1. Which tone should the project description use? (text may wrap onto
    │      a second line)
    │     ○ Formal
    │       Polished, professional prose suitable for project documentation
    │     ○ Casual
    │     ○ Technical
    │     ○ Custom
    │ ▶ 2. What name should go in the title of docs/question-test.md?
    │     ↳ (click to answer)
    │ ╭────────╮
    │ │ Submit │   ↑↓ navigate • Enter select
    │ ╰────────╯
    ╰────────────────────────────────╯

A box can hold several questions: one is expanded (``▼``) and focused, the rest
are collapsed (``▶``). An option may carry a description line underneath it,
which is not itself an option. A question with no options and ``(click to
answer)`` is free text. A ``Custom`` option is a free-text answer behind a row.

The same terminal also shows Freebuff's own survey: a box titled
"quick question 1/11" with "esc not now". That is not a task question, so the
runner dismisses it with Esc and never forwards it to Discord.

Everything here is a pure function on text: no tmux, no Discord, no model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Sequence

import project_context

QUESTION_TITLE = "Some questions for you"
# Freebuff's own survey popup, which is never a question for the job.
SURVEY_TITLE_RE = re.compile(r"quick\s+question\b", re.IGNORECASE)
SURVEY_DISMISS_RE = re.compile(r"\bnot\s+now\b", re.IGNORECASE)

CIRCLE_OPEN = "○◯〇◌"
CIRCLE_FILLED = "◉●⬤⭕◍◎"
CHECK_MARKS = "✓✔"
ARROWS = "❯›➤►➜"
EXPANDED_MARKERS = "▼▾▽"
COLLAPSED_MARKERS = "▶▸▹"

BOX_BORDERS = "│┃║|┆┇"
RULE_CHARS = "─━═╌╍┄┅╭╮╰╯├┤┬┴┼┌┐└┘╔╗╚╝"
RULE_RE = re.compile(r"^[\s" + re.escape(RULE_CHARS) + r"]+$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# "▼ 1. Which tone ..." or a bare "1. Which tone ...".
QUESTION_HEADER_RE = re.compile(
    r"^(?P<mark>[" + re.escape(EXPANDED_MARKERS + COLLAPSED_MARKERS) + r"])?\s*"
    r"(?P<number>\d{1,2})\s*[.)]\s*(?P<text>\S.*)$"
)
# "○ Formal", "◉ Casual", "❯ ◯ Home": a circle, an optional focus arrow.
OPTION_RE = re.compile(
    r"^(?P<arrow>[" + re.escape(ARROWS) + r"]\s*)?"
    r"(?P<circle>[" + re.escape(CIRCLE_OPEN + CIRCLE_FILLED) + r"])\s*"
    r"(?P<label>\S.*)$"
)
CLICK_HINT_RE = re.compile(r"click to answer", re.IGNORECASE)
HINT_LINE_RE = re.compile(r"^[↳»→]\s*(?P<value>.*)$")
# The Submit button. Only a focus arrow in front of it says it is focused; a
# frame or brackets around the word are just how the button is drawn.
SUBMIT_ROW_RE = re.compile(
    r"^(?P<mark>[" + re.escape(ARROWS + CHECK_MARKS + CIRCLE_FILLED) + r"])?\s*"
    r"\[?\s*Submit\b",
    re.IGNORECASE,
)
# The ad strip above the box: an app banner and a report link, never content.
AD_LINE_RE = re.compile(r"\[Report\b|\bGet the app\b\s*$|\bfor Android\b", re.IGNORECASE)
CLOSE_LINE_RE = re.compile(r"^close\b", re.IGNORECASE)

MAX_QUESTION_CHARS = 1500
MAX_OPTION_CHARS = 200
MAX_DESCRIPTION_CHARS = 400
MAX_OPTIONS = 12
MAX_ANSWER_CHARS = 500
MAX_TAIL_CHARS = 2500


@dataclass(frozen=True)
class Option:
    """One row of a question: its label, the description under it, its state."""

    label: str
    description: str = ""
    chosen: bool = False
    cursor: bool = False


@dataclass(frozen=True)
class Question:
    """One numbered question in the box."""

    number: int = 1
    text: str = ""
    options: tuple[Option, ...] = ()
    expanded: bool = True
    free_text: bool = False
    answer_hint: str = ""
    total: int = 1

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(option.label for option in self.options)

    @property
    def custom_index(self) -> int | None:
        """The row that opens a free-text field, if the box offers one."""
        for index, option in enumerate(self.options):
            word = option.label.strip().lower()
            if word == "custom" or word.startswith("custom ") or word.startswith("custom:"):
                return index
        return None

    @property
    def chosen_index(self) -> int | None:
        """The option the box marks as chosen, or None."""
        for index, option in enumerate(self.options):
            if option.chosen:
                return index
        return None

    @property
    def cursor_index(self) -> int | None:
        """The option the box draws as focused, when it draws one at all."""
        for index, option in enumerate(self.options):
            if option.cursor:
                return index
        return None

    @property
    def answerable(self) -> bool:
        """Whether an answer can be typed into this question at all."""
        return bool(self.options) or self.free_text

    @property
    def answered(self) -> bool:
        if self.chosen_index is not None:
            return True
        if self.free_text and self.answer_hint and not CLICK_HINT_RE.search(self.answer_hint):
            return True
        return False

    @property
    def key(self) -> str:
        """Stable identity of this question's visible content."""
        body = f"{self.number}\x00{self.text}\x00" + "\x00".join(
            option.label + "\x01" + option.description for option in self.options
        )
        return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]

    def numbered(self) -> str:
        """The question and its numbered options, as Discord shows them."""
        lines = [f"{self.number}. {self.text}" if self.number else self.text]
        for index, option in enumerate(self.options, start=1):
            line = f"   {index}. {option.label}"
            if option.description:
                line += f" — {option.description}"
            lines.append(line)
        return "\n".join(lines)


@dataclass(frozen=True)
class QuestionBox:
    """One box on screen: its questions, or the survey, or nothing readable."""

    questions: tuple[Question, ...] = ()
    survey: bool = False
    raw: str = ""

    @property
    def total(self) -> int:
        return len(self.questions)

    def question(self, number: int) -> Question | None:
        for question in self.questions:
            if question.number == number:
                return question
        return None

    @property
    def submit_focused(self) -> bool | None:
        """Whether Submit is the focused element: True, False, or None.

        None means no Submit row was found at all, so the focus cannot be
        confirmed and Enter must not be sent: on a row that is an option, Enter
        selects it.
        """
        for row in self.raw.splitlines():
            match = SUBMIT_ROW_RE.match(_clean(row))
            if match is not None:
                return match.group("mark") is not None
        return None

    @property
    def active(self) -> Question | None:
        """The question the box is focused on: expanded first, unanswered first."""
        for question in self.questions:
            if question.answerable and not question.answered and question.expanded:
                return question
        for question in self.questions:
            if question.answerable and not question.answered:
                return question
        for question in self.questions:
            if question.expanded:
                return question
        return self.questions[0] if self.questions else None

    @property
    def all_answered(self) -> bool:
        if not self.questions:
            return False
        return all(q.answered or not q.answerable for q in self.questions)

    @property
    def key(self) -> str:
        body = "|".join(q.key for q in self.questions) + f"|{int(self.survey)}"
        return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]

    def sanitized_tail(self, limit: int = MAX_TAIL_CHARS) -> str:
        """The pane without escape codes, control characters or the ad strip.

        This is what /logs and Discord get when a box exists but could not be
        parsed: enough to see the box, never a terminal control sequence.
        """
        rows = [
            row.rstrip()
            for row in strip_ansi(self.raw).splitlines()
            if not AD_LINE_RE.search(row)
        ]
        text = "\n".join(rows).strip()
        text = "".join(char for char in text if char == "\n" or char >= " ")
        text = redact(text)
        if len(text) > limit:
            text = "[...]\n" + text[-limit:]
        return text


def redact(text: str) -> str:
    """Everything read from a pane passes through here before it leaves.

    The Discord post, the job log and the QUESTION_DEBUG dump all carry pane
    text, and a pane can show a token the builder printed. One function, so no
    sink can quietly skip it.
    """
    return project_context.redact_secrets(strip_ansi(text or ""))


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def _clean(raw: str) -> str:
    """One pane line without escape codes, box borders or padding."""
    text = strip_ansi(raw).replace("\r", "").strip()
    while text and (text[0] in BOX_BORDERS or text[0] in RULE_CHARS):
        text = text[1:].lstrip()
    while text and (text[-1] in BOX_BORDERS or text[-1] in RULE_CHARS):
        text = text[:-1].rstrip()
    return text


def _is_footer(line: str) -> bool:
    """The box's key hint, not content: 'Enter select' and the Submit button."""
    lowered = line.lower()
    if "enter" in lowered and ("select" in lowered or "submit" in lowered):
        return True
    if "navigate" in lowered and ("↑" in line or "↓" in line or "enter" in lowered):
        return True
    return lowered.strip("[](){}<>_ •") == "submit"


def _title_index(rows: Sequence[str]) -> int | None:
    wanted = QUESTION_TITLE.lower()
    for index, line in enumerate(rows):
        if wanted in line.lower():
            return index
    return None


def _option(line: str) -> Option | None:
    match = OPTION_RE.match(line)
    if match is None:
        return None
    label = " ".join(match.group("label").split())
    if not label:
        return None
    label = label.rstrip(CHECK_MARKS + " ").strip()[:MAX_OPTION_CHARS]
    return Option(
        label=label,
        chosen=match.group("circle") in CIRCLE_FILLED or label.endswith(tuple(CHECK_MARKS)),
        cursor=bool(match.group("arrow")),
    )


def _header(line: str) -> re.Match[str] | None:
    return QUESTION_HEADER_RE.match(line)


def _questions_from(rows: Sequence[str]) -> list[Question]:
    """Parse the rows below the box title into numbered questions."""
    questions: list[Question] = []
    number = 1
    text: list[str] = []
    options: list[Option] = []
    expanded = True
    free_text = False
    hint = ""
    started = False

    def finish() -> None:
        nonlocal text, options, expanded, free_text, hint, number
        if not started:
            return
        body = " ".join(part for part in text if part).strip()
        if len(body) > MAX_QUESTION_CHARS:
            body = body[:MAX_QUESTION_CHARS].rstrip() + " [...]"
        questions.append(
            Question(
                number=number,
                text=body or "(Freebuff did not show any question text)",
                options=tuple(options[:MAX_OPTIONS]),
                expanded=expanded,
                free_text=free_text or not options,
                answer_hint=hint,
            )
        )
        text, options, expanded, free_text, hint = [], [], True, False, ""

    for line in rows:
        if RULE_RE.match(line) or CLOSE_LINE_RE.match(line):
            continue
        header = _header(line)
        if header is not None:
            finish()
            number = int(header.group("number"))
            started = True
            expanded = (header.group("mark") or "▼") not in COLLAPSED_MARKERS
            free_text = False
            hint = ""
            body = header.group("text").strip()
            if body:
                text.append(body)
            continue
        if _is_footer(line):
            continue
        option = _option(line)
        if option is not None:
            options.append(option)
            continue
        hint_match = HINT_LINE_RE.match(line)
        if hint_match is not None:
            value = " ".join(hint_match.group("value").split())
            if CLICK_HINT_RE.search(value):
                free_text = True
                hint = ""
            else:
                hint = value[:MAX_ANSWER_CHARS]
            continue
        if not started:
            continue
        if CLICK_HINT_RE.search(line):
            free_text = True
            continue
        if options:
            # A line directly under an option, inside its block, is that
            # option's description; it is never an option of its own.
            last = options[-1]
            joined = " ".join(f"{last.description} {line}".split())
            options[-1] = replace(last, description=joined[:MAX_DESCRIPTION_CHARS])
        else:
            text.append(line)
    finish()
    return [question for question in questions if question.answerable or question.options]


def parse_pane(pane: str) -> QuestionBox | None:
    """The box in a pane capture, or None when there is no box at all.

    A titled box that yields no question comes back with ``questions == ()`` so
    the caller can show the pane instead of guessing; the survey popup comes
    back with ``survey=True`` so the caller can dismiss it.
    """
    if not pane or not pane.strip():
        return None
    rows = [_clean(raw) for raw in pane.splitlines()]
    visible = [row for row in rows if row]
    if not visible:
        return None

    for line in visible:
        if SURVEY_TITLE_RE.search(line):
            return QuestionBox(survey=True, raw=pane)
    if any(SURVEY_DISMISS_RE.search(line) for line in visible) and not any(
        _option(line) for line in visible
    ):
        return QuestionBox(survey=True, raw=pane)

    title = _title_index(rows)
    if title is None:
        return None

    questions = _questions_from(rows[title + 1 :])
    total = len(questions)
    return QuestionBox(
        questions=tuple(replace(q, total=total) for q in questions), raw=pane
    )


def option_number(answer: str, count: int) -> int | None:
    """The 1-based option an answer names, or None when it is free text."""
    value = (answer or "").strip()
    if not value.isdigit():
        return None
    number = int(value)
    if 1 <= number <= min(count, MAX_OPTIONS):
        return number
    return None


def flatten_answer(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    """One line without escape codes: a newline would submit the box early."""
    value = strip_ansi(text or "").replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())[:limit]


def validate_answer(answer: str, options: Sequence[str]) -> str | None:
    """A manager answer that can be typed, or None when it cannot be trusted."""
    value = flatten_answer(answer)
    if not value:
        return None
    number = option_number(value, len(options))
    if number is not None:
        return str(number)
    if value.isdigit():
        # A number that names no option is not an answer to this box.
        return None
    return value
