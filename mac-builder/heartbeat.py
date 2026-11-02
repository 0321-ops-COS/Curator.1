"""Whether Freebuff is actually moving: a pane heartbeat, not a stage clock.

A stage can sit still for an hour while Freebuff is busy, so nothing here trusts
it. A captured pane loses the parts that change on their own (timers, spinners,
the ad box) and the rest is hashed, so the hash only moves when Freebuff's own
output does. Nothing here talks to Discord, tmux or the job.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Callable

import status_board

# The stages where Freebuff is meant to be moving on its own.
WORKING_STAGES = (
    status_board.BRANCH_READY,
    status_board.FREEBUFF_WORKING,
    status_board.CHECKS,
    status_board.TESTS,
    status_board.MANAGER_REVIEW,
)

# What a tick asks the caller to do: post a message, or edit the board's line.
NOTIFY = "notify"
ACTIVITY = "activity"

_DECORATION_ONLY = re.compile(r"^[\s─-╿|/\\·•▁▂▃▄▅▆▇█░▒▓]+$")
_VOLATILE = (
    re.compile(r"\b\d+m \d+s\b"),
    re.compile(r"\b\d+h \d+m\b"),
    re.compile(r"\b\d+(?:\.\d+)?s\b"),
    re.compile(r"\b\d+%\b"),
    re.compile(r"(?i)\b(?:working|thinking|esc to interrupt)\b[^\n]*"),
    re.compile(r"(?i)\b(?:upgrade|try pro|freebuff\.com)\b[^\n]*"),
    re.compile(r"^[\s⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+"),
)


def stable_lines(pane: str) -> list[str]:
    """The pane's own output: no timers, no spinner, no decoration, no ad box."""
    lines: list[str] = []
    for raw in (pane or "").splitlines():
        text = raw
        for pattern in _VOLATILE:
            text = pattern.sub(" ", text)
        text = " ".join(text.split())
        if text and not _DECORATION_ONLY.match(text):
            lines.append(text)
    return lines


def signature(pane: str) -> str:
    """A hash of what Freebuff itself printed; equal while the pane only ticks."""
    return hashlib.sha1("\n".join(stable_lines(pane)).encode("utf-8")).hexdigest()


def stage_message(board: status_board.Board, now: float) -> str:
    """One short line for a new stage: id, bar, step, stage, elapsed."""
    text = (
        f"`{board.job_id}` {status_board.bar(board.position)} {board.position}/"
        f"{len(status_board.PROGRESS_STAGES)} {status_board.stage_title(board.stage)}"
    )
    if board.started_at is not None:
        text += f" · {status_board.format_elapsed(now - board.started_at)}"
    return text


def working_message(board: status_board.Board, idle: float) -> str:
    """The while-it-keeps-moving ping: bar and how long since the last change."""
    return (
        f"`{board.job_id}` still working {status_board.bar(board.position)}"
        f" · last activity {int(idle)}s ago"
    )


def stuck_message(board: status_board.Board, idle: float) -> str:
    """Only a pane that has stopped changing earns this word."""
    return (
        f"⏳ `{board.job_id}` possibly stuck at {status_board.stage_title(board.stage)}: "
        f"nothing on the pane for {int(idle)}s — `/screen` to look."
    )


@dataclass(frozen=True)
class Action:
    """One thing a tick decided: a new message, or the board's activity line."""

    kind: str
    text: str


@dataclass
class _State:
    """What one job's pane looked like last time, and what was already said."""

    stage: str
    change_at: float
    digest: str = ""
    stuck_sent: bool = False
    ping_at: float = 0.0


class Heartbeat:
    """Decide what the owner hears about a job from its pane, once per tick."""

    def __init__(
        self,
        *,
        ping_seconds: float = 600.0,
        stall_seconds: float = 300.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.ping_seconds = max(0.0, float(ping_seconds))
        self.stall_seconds = max(0.0, float(stall_seconds))
        self._now = now
        self._states: dict[str, _State] = {}

    def tick(self, board: status_board.Board, pane: str) -> list[Action]:
        """What this tick proves: a stage line, maybe a ping, and the board line.

        A pane unchanged for ``stall_seconds`` is the only thing called stuck,
        once per stall; while it keeps changing, a "still working" message is
        due every ``ping_seconds``.
        """
        when = self._now()
        state = self._states.get(board.job_id)
        if state is None:
            # No stage seen yet, so the first tick announces where the job is.
            state = self._states[board.job_id] = _State("", when, ping_at=when)
        digest = signature(pane)
        changed = bool(digest) and digest != state.digest
        if changed:
            state.digest = digest
            state.change_at = when
            state.stuck_sent = False
        idle = max(0.0, when - state.change_at)
        actions: list[Action] = []
        if board.stage != state.stage:
            state.stage = board.stage
            state.ping_at = when
            actions.append(Action(NOTIFY, stage_message(board, when)))
        actions.append(Action(ACTIVITY, f"last activity {int(idle)}s ago"))
        if self.stall_seconds > 0 and idle >= self.stall_seconds:
            if not state.stuck_sent:
                state.stuck_sent = True
                actions.append(Action(NOTIFY, stuck_message(board, idle)))
        elif (
            changed
            and self.ping_seconds > 0
            and when - state.ping_at >= self.ping_seconds
        ):
            state.ping_at = when
            actions.append(Action(NOTIFY, working_message(board, idle)))
        return actions
