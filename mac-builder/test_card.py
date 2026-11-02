"""The test card: the task's own steps, quoted and never invented.

No model is asked. A step is a line the task put under a heading containing
"Accept" or "Test", or one starting "Test:". At most five are shown, in order.
"""

from __future__ import annotations

import re

MAX_STEPS = 5
HEADINGS = ("accept", "test")
# A leading bullet or "1." marker, which is the task's formatting, not a step.
MARKER = re.compile(r"^(?:[-*\u2022\u00b7]|\d+[.)])\s+")


def steps(task_text: str) -> list[str]:
    """The task's own steps, in order, capped: sections and "Test:" lines."""
    found: list[str] = []
    section = False
    for raw in (task_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            section = any(word in line.lower() for word in HEADINGS)
            continue
        if line.lower().startswith("test:"):
            found.append(line[len("test:"):].strip())
            continue
        if section:
            found.append(MARKER.sub("", line).strip())
    return [step[:200] for step in found if step][:MAX_STEPS]


def card(task_text: str) -> str:
    """The message that asks the owner to test, or the line that says none."""
    items = steps(task_text)
    if not items:
        return "No test steps in the task"
    lines = ["**For you to test:**"]
    lines += [f"{number}. {item}" for number, item in enumerate(items, start=1)]
    return "\n".join(lines)
