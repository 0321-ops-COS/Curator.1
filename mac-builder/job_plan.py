"""The intent a job was queued with, so a review and a question can read it.

A build sheet's job text already says what the job is for, how to tell it worked
and what it must not touch; the manager still answered every question against
"(no plan)", because nothing carried those lines onto the ``Job``. This module
derives that short plan from the job's own words, with no model and no guessing:
it only quotes, trims and labels text a person or a sheet already wrote.

A job whose text states no acceptance and no out-of-scope line has no plan, and
says so, rather than inventing one from the goal.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_GOAL = 240
MAX_ITEM = 180
MAX_ITEMS = 6
MAX_RENDER = 1500

# The headings a job text uses for "how I know it worked" and "keep out".
ACCEPTANCE_HEADS = (
    "acceptance", "acceptance criteria", "accept", "accepted when",
    "done when", "definition of done", "test", "tests", "testing", "verify",
    "expected result", "success criteria",
)
OUT_OF_SCOPE_HEADS = (
    "out of scope", "not in scope", "not included", "excluded",
    "do not", "don't", "must not", "leave alone",
)
BULLETS = "-*•+–0123456789.) \t"


@dataclass(frozen=True)
class PlanBrief:
    """A job's own goal, its acceptance lines, and what it must leave alone."""

    goal: str = ""
    acceptance: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.goal or self.acceptance or self.out_of_scope)


def _clean(line: str) -> str:
    """One bullet or heading line as plain text, with its marker removed."""
    text = " ".join(line.strip().lstrip(BULLETS).strip().split())
    return text[:MAX_ITEM] if len(text) > MAX_ITEM else text


def _heading(line: str) -> str:
    """'acceptance' | 'out' | '' for a line that is one of the two headings."""
    text = " ".join(line.strip().strip("#*_` ").split()).strip().lower()
    head = text.split(":", 1)[0].strip()
    if not head or len(head) > 40:
        return ""
    text = head
    if text in ACCEPTANCE_HEADS or any(text.startswith(head + " ") for head in ACCEPTANCE_HEADS):
        return "acceptance"
    if text in OUT_OF_SCOPE_HEADS or any(text.startswith(head + " ") for head in OUT_OF_SCOPE_HEADS):
        return "out"
    return ""


def parse(text: str) -> PlanBrief:
    """The plan a job's text states, or an empty brief when it states none.

    Only labelled sections count. A goal is the first line that is neither a
    heading nor a bullet, so a sheet job's first sentence becomes the goal and
    nothing is invented when it has no such sentence.
    """
    goal = ""
    acceptance: list[str] = []
    out_of_scope: list[str] = []
    section = ""

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            section = "" if not line and section else section
            continue
        head = _heading(line)
        if head:
            section = head
            # "Acceptance: the test passes" carries the item on the same line.
            rest = " ".join(line.split(":", 1)[1].split()) if ":" in line else ""
            rest = _clean(rest)
            if rest:
                (acceptance if head == "acceptance" else out_of_scope).append(rest)
            continue
        item = _clean(line)
        if not item:
            continue
        if section == "acceptance":
            if len(acceptance) < MAX_ITEMS:
                acceptance.append(item)
        elif section == "out":
            if len(out_of_scope) < MAX_ITEMS:
                out_of_scope.append(item)
        elif not goal and not line.startswith(("-", "*", "•")):
            goal = item[:MAX_GOAL]

    return PlanBrief(goal=goal, acceptance=tuple(acceptance),
                     out_of_scope=tuple(out_of_scope))


def render(brief: PlanBrief) -> str:
    """The plan as the prompt reads it, capped; empty when there is nothing."""
    if not brief.acceptance and not brief.out_of_scope:
        return ""
    lines: list[str] = []
    if brief.goal:
        lines.append(f"goal: {brief.goal}")
    if brief.acceptance:
        lines.append("acceptance: " + "; ".join(brief.acceptance))
    if brief.out_of_scope:
        lines.append("out of scope: " + "; ".join(brief.out_of_scope))
    text = "\n".join(lines)
    return text if len(text) <= MAX_RENDER else text[: MAX_RENDER - 1] + "…"


def for_text(text: str) -> str:
    """The plan to store on a job made from this text; "" when it has none."""
    return render(parse(text))


def describe(brief: PlanBrief) -> str:
    """One line for /jobs: the acceptance count, never the whole plan."""
    if not (brief.acceptance or brief.out_of_scope):
        return "no plan"
    parts = []
    if brief.acceptance:
        parts.append(f"{len(brief.acceptance)} acceptance line(s)")
    if brief.out_of_scope:
        parts.append(f"{len(brief.out_of_scope)} out-of-scope line(s)")
    return ", ".join(parts) or "goal only"
