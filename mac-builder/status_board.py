"""The status board: real stages, real elapsed time, no invented progress.

Freebuff reports no progress numbers, so this module never invents one. It
reports the stage the job is actually at (from the pipeline itself), how long it
has been running, its place in the queue, and — when a person is needed — the
screen ``screens.py`` says is on the pane and the exact command that deals with
it. The bar position is the stage number, not a guess at completion.

Two pieces live here:

* the pure model — ``Board``, ``render``, ``waiting_for``, ``ping_text`` — which
  is a function of its inputs and an injected clock;
* ``BoardPublisher``, which owns one Discord message per job and edits it in
  place. It has no Discord, tmux or network import: the transport and the clocks
  are injected, so throttling, edit failures and pings are all testable offline.

A screen label is only ever the kind ``screens.py`` returned. When the label is
uncertain it is shown as "looks like: <kind>" and ``/screen`` is always offered,
because the patterns are not verified against the real binary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Sequence

import screens

# ---------------------------------------------------------------- the stages
#
# The order of a builder job. A stage is reported by the code that actually gets
# there, so the board cannot show progress the job has not made.

QUEUED = "queued"
BRANCH_READY = "branch_ready"
FREEBUFF_WORKING = "freebuff_working"
WAITING_ON_YOU = "waiting_on_you"
CHECKS = "checks"
TESTS = "tests"
MANAGER_REVIEW = "manager_review"
AWAITING_APPROVE = "awaiting_approve"
PUSHED = "pushed"
REJECTED = "rejected"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

STAGES = (
    QUEUED,
    BRANCH_READY,
    FREEBUFF_WORKING,
    WAITING_ON_YOU,
    CHECKS,
    TESTS,
    MANAGER_REVIEW,
    AWAITING_APPROVE,
    PUSHED,
    REJECTED,
    FAILED,
    CANCELLED,
    TIMED_OUT,
)

# The seven slots of the bar: one per stage a job can occupy before it ends.
PROGRESS_STAGES = (
    QUEUED,
    BRANCH_READY,
    FREEBUFF_WORKING,
    CHECKS,
    TESTS,
    MANAGER_REVIEW,
    AWAITING_APPROVE,
)

TERMINAL_STAGES = (PUSHED, REJECTED, FAILED, CANCELLED, TIMED_OUT)

STAGE_TITLES = {
    QUEUED: "queued",
    BRANCH_READY: "branch ready",
    FREEBUFF_WORKING: "Freebuff working",
    WAITING_ON_YOU: "waiting on you",
    CHECKS: "checks",
    TESTS: "tests",
    MANAGER_REVIEW: "manager review",
    AWAITING_APPROVE: "awaiting Approve",
    PUSHED: "pushed",
    REJECTED: "rejected",
    FAILED: "failed",
    CANCELLED: "cancelled",
    TIMED_OUT: "timed out",
}

# The commands that deal with each screen, as screens.py names them. A kind is
# never invented here: anything unknown gets the generic line below.
WAITING_COMMANDS = {
    screens.QUESTION_BOX: (
        "answer with `/answer <number or text>`; `/screen` shows the box again"
    ),
    screens.SIGN_IN: (
        "sign in on the Mac, or use `/screen`, `/key`, `/send`"
    ),
    screens.RATE_LIMIT: (
        "`/screen` to look; wait it out, `/send` to retry, or `/cancel`"
    ),
    screens.UPDATE_PROMPT: (
        "`/screen` to look, `/key Enter` to accept the update, or `/cancel`"
    ),
    screens.CONFIRM_PROMPT: (
        "`/key y` or `/key n` (`/screen` first if you need to see it)"
    ),
    screens.ERROR_OR_CRASH: (
        "`/screen` to read it, then `/cancel` or `/send`"
    ),
    screens.SURVEY: "dismissed with Esc automatically; nothing to do",
    screens.READY_PROMPT: "nothing to do: the prompt is ready",
    screens.WORKING: "nothing to do: it is working",
    screens.UNKNOWN: "`/screen` to look at the pane",
}
DEFAULT_WAITING_COMMAND = "`/screen` to look at the pane, then `/key`, `/send` or `/cancel`"


def waiting_for(screen_kind: str) -> str:
    """The command that deals with a screen, for the kind screens.py returned."""
    return WAITING_COMMANDS.get((screen_kind or "").strip(), DEFAULT_WAITING_COMMAND)


def stage_title(stage: str) -> str:
    return STAGE_TITLES.get(stage, stage or "unknown")


def step(stage: str) -> int:
    """The 1-based bar position of a stage, or 0 when it is not on the bar."""
    try:
        return PROGRESS_STAGES.index(stage) + 1
    except ValueError:
        return 0


def bar(position: int, width: int = len(PROGRESS_STAGES)) -> str:
    """'▰▰▰▱▱▱▱' for a stage number. Never derived from time or guesses."""
    filled = max(0, min(int(position), width))
    return "▰" * filled + "▱" * (width - filled)


def format_elapsed(seconds: float | None) -> str:
    """Phone-friendly duration: 8s, 12m 04s, 1h 02m."""
    if seconds is None:
        return ""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, tail = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {tail:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def truncate(text: str, limit: int = 160) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------- the model


@dataclass(frozen=True)
class Board:
    """One job's status, as the board shows it."""

    job_id: str
    kind: str = "task"
    project: str = ""
    stage: str = QUEUED
    # The last event in the job's own words: "tests passed", "pushed freebuff/…".
    detail: str = ""
    # The kind screens.py returned, and only ever that.
    screen_kind: str = ""
    # 1-based place in the queue; 0 once running.
    queue_position: int = 0
    # Wall-clock start, so the elapsed time is real.
    started_at: float | None = None
    # The bar position; the stage's own number unless a terminal stage kept the
    # last stage it reached.
    progress: int = 0

    @property
    def pipeline(self) -> bool:
        """True for the builder jobs the bar describes (task and dan)."""
        return self.kind in ("task", "dan")

    @property
    def terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES

    @property
    def waiting(self) -> bool:
        return self.stage == WAITING_ON_YOU

    @property
    def position(self) -> int:
        if self.progress:
            return self.progress
        return step(self.stage)


def render(board: Board, now: float | None = None) -> str:
    """Short, phone-friendly text for one job. At most five lines.

    The first line names the job, the second is the bar (stage number, never a
    percentage), and the rest say where the queue stands, what the screen looks
    like and what the last event was.
    """
    when = time.time() if now is None else now
    lines = [
        f"`{board.job_id}` · {board.kind}"
        + (f" · {board.project}" if board.project else "")
    ]
    if board.pipeline:
        position = board.position
        line = f"{bar(position)} {position}/{len(PROGRESS_STAGES)} {stage_title(board.stage)}"
        elapsed = format_elapsed(
            (when - board.started_at) if board.started_at is not None else None
        )
        if elapsed:
            line += f" · {elapsed}"
        lines.append(line)
    else:
        lines.append(stage_title(board.stage))
    if board.stage == QUEUED and board.queue_position:
        lines.append(f"queued #{board.queue_position}")
    if board.screen_kind:
        lines.append(
            f"looks like: {board.screen_kind} — {waiting_for(board.screen_kind)}"
        )
    if board.detail:
        lines.append(f"last event: {truncate(board.detail)}")
    return "\n".join(lines)


def render_waiting(entries: Sequence[tuple[int, str, str, str]]) -> str:
    """One compact line per waiting job: position, id, kind, text preview."""
    lines: list[str] = []
    for position, job_id, kind, title in entries:
        preview = truncate(title or "(no text)", 70)
        lines.append(f"{position}. `{job_id}` · {kind} · {preview}")
    if not lines:
        return "nothing is waiting."
    return "\n".join(lines)


# ---------------------------------------------------------------- the pings
#
# Only these moments are worth a phone notification. Everything else edits the
# board silently.

PING_STAGES = (WAITING_ON_YOU, AWAITING_APPROVE, PUSHED, REJECTED, FAILED, TIMED_OUT)


def ping_key(board: Board) -> str:
    """Dedupe a ping by job and kind, not by pane text.

    A screen that alternates between two texts reports the same kind twice; the
    owner is told once. A different kind is a different thing to do, so it pings.
    """
    if board.waiting:
        return f"{board.job_id}:{board.stage}:{board.screen_kind or screens.UNKNOWN}"
    return f"{board.job_id}:{board.stage}"


def ping_text(board: Board) -> str:
    """The short ping for a stage, or '' when the stage does not ping."""
    if board.stage == WAITING_ON_YOU:
        kind = board.screen_kind or screens.UNKNOWN
        return (
            f"⏸ `{board.job_id}` needs you: looks like {kind} — "
            f"{waiting_for(kind)}"
        )
    if board.stage == AWAITING_APPROVE:
        return f"✅ `{board.job_id}` is ready for Approve or Reject."
    if board.stage == PUSHED:
        return f"✅ `{board.job_id}` finished" + (
            f": {truncate(board.detail, 80)}" if board.detail else "."
        )
    if board.stage == REJECTED:
        return f"🚫 `{board.job_id}` was rejected; nothing was pushed."
    if board.stage == FAILED:
        return f"❌ `{board.job_id}` failed" + (
            f": {truncate(board.detail, 120)}" if board.detail else "."
        )
    if board.stage == TIMED_OUT:
        return f"⌛ `{board.job_id}` timed out" + (
            f": {truncate(board.detail, 120)}" if board.detail else "."
        )
    return ""


def stalled_ping(board: Board) -> str:
    """The once-per-stall ping: the stage has not moved for a long time."""
    return (
        f"⏳ `{board.job_id}` has not moved from {stage_title(board.stage)} for a "
        "while — `/status` or `/screen` to look."
    )


# ------------------------------------------------------------- the publisher


class BoardTransport:
    """What the publisher needs from Discord; injected, never imported.

    ``send`` returns a message id (or None when it failed), ``edit`` returns
    True when the edit landed. Nothing here raises into the caller.
    """

    async def send(self, channel_id: int, text: str) -> str | None:  # pragma: no cover
        raise NotImplementedError

    async def edit(  # pragma: no cover
        self, channel_id: int, message_id: str, text: str
    ) -> bool:
        raise NotImplementedError


@dataclass
class _Tracked:
    """One job's board and the Discord message that carries it."""

    board: Board
    channel_id: int
    requester_id: int
    message_id: str | None = None
    last_edit_at: float = 0.0
    last_edit_text: str = ""
    last_change_at: float = 0.0
    pings_sent: set[str] = field(default_factory=set)
    stall_pinged: bool = False
    post_failed_at: float | None = None
    # A stage changed while the edit was throttled: the next tick draws it.
    dirty: bool = False


class BoardPublisher:
    """One live message per job, edited in place, plus the rare ping.

    Everything is guarded: a status update may never fail, delay or alter a job,
    so every transport call is wrapped and a failed edit costs one new message,
    not an exception.
    """

    def __init__(
        self,
        transport: BoardTransport,
        *,
        edit_min_seconds: float = 15.0,
        stall_ping_seconds: float = 600.0,
        pings: bool = True,
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.edit_min_seconds = max(0.0, float(edit_min_seconds))
        self.stall_ping_seconds = float(stall_ping_seconds)
        self.pings = bool(pings)
        self.now = now
        self.wall = wall
        self._jobs: dict[str, _Tracked] = {}

    # -------------------------------------------------------------- tracking

    def track(
        self,
        job_id: str,
        *,
        channel_id: int,
        requester_id: int,
        kind: str,
        project: str = "",
        started_at: float | None = None,
        queue_position: int = 0,
    ) -> Board:
        """Start (or refresh) the board for a job, before its first report."""
        existing = self._jobs.get(job_id)
        when = self.now()
        if existing is not None:
            return existing.board
        board = Board(
            job_id=job_id,
            kind=kind,
            project=project,
            started_at=started_at,
            queue_position=max(0, int(queue_position)),
        )
        self._jobs[job_id] = _Tracked(
            board=board,
            channel_id=channel_id,
            requester_id=requester_id,
            last_change_at=when,
        )
        return board

    def board_for(self, job_id: str) -> Board | None:
        tracked = self._jobs.get(job_id)
        return tracked.board if tracked is not None else None

    def tracked_ids(self) -> list[str]:
        return list(self._jobs)

    def forget(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    # -------------------------------------------------------------- reporting

    async def report(
        self,
        job_id: str,
        stage: str,
        detail: str = "",
        *,
        screen_kind: str = "",
        started_at: float | None = None,
        queue_position: int | None = None,
    ) -> None:
        """Record a real stage change, then edit the board and maybe ping.

        A stage the runner/jobs never reported is still shown as itself: the
        publisher does not decide where a job is, it only draws it.
        """
        tracked = self._jobs.get(job_id)
        if tracked is None:
            return
        when = self.now()
        previous = tracked.board
        pingable = stage in PING_STAGES or previous.stage == WAITING_ON_YOU
        new = replace(
            previous,
            stage=stage,
            detail=detail or "",
            screen_kind=screen_kind or "",
            started_at=started_at if started_at is not None else previous.started_at,
            queue_position=(
                max(0, int(queue_position))
                if queue_position is not None
                else (previous.queue_position if stage == QUEUED else 0)
            ),
            progress=(
                previous.progress
                if stage in TERMINAL_STAGES and previous.progress
                else step(stage)
            ),
        )
        if (
            new.stage != previous.stage
            or new.screen_kind != previous.screen_kind
            or new.detail != previous.detail
            or new.queue_position != previous.queue_position
        ):
            tracked.last_change_at = when
            tracked.stall_pinged = False
        tracked.board = new
        if pingable:
            await self._maybe_ping(tracked, new)
        # Throttled like any other edit: a burst of stages must not rate-limit
        # the bot. The ticker flushes whatever this leaves pending.
        await self._draw(tracked)

    async def _maybe_ping(self, tracked: _Tracked, board: Board) -> None:
        if not self.pings:
            return
        text = ping_text(board)
        if not text:
            return
        key = ping_key(board)
        if key in tracked.pings_sent:
            return
        tracked.pings_sent.add(key)
        mention = f"<@{tracked.requester_id}> " if tracked.requester_id else ""
        try:
            await self.transport.send(tracked.channel_id, mention + text)
        except Exception:  # a ping must never touch the job
            return

    # ----------------------------------------------------------------- drawing

    async def _draw(self, tracked: _Tracked, *, force: bool = False) -> None:
        """Post or edit the board, throttled, and survive a deleted message."""
        when = self.now()
        text = render(tracked.board, now=self.wall())
        if text == tracked.last_edit_text and tracked.message_id is not None:
            tracked.dirty = False
            return
        if tracked.message_id is None:
            await self._post(tracked, text, when)
            return
        if not force and (when - tracked.last_edit_at) < self.edit_min_seconds:
            tracked.dirty = True
            return
        try:
            edited = await self.transport.edit(
                tracked.channel_id, tracked.message_id, text
            )
        except Exception:
            edited = False
        if edited:
            tracked.last_edit_at = when
            tracked.last_edit_text = text
            tracked.dirty = False
            return
        # The message was deleted or cannot be edited: one new post, then carry
        # on with the new id.
        tracked.message_id = None
        await self._post(tracked, text, when)

    async def _post(self, tracked: _Tracked, text: str, when: float) -> None:
        if tracked.post_failed_at is not None and (
            when - tracked.post_failed_at
        ) < max(1.0, self.edit_min_seconds):
            return
        try:
            message_id = await self.transport.send(tracked.channel_id, text)
        except Exception:
            message_id = None
        if message_id is None:
            tracked.post_failed_at = when
            return
        tracked.post_failed_at = None
        tracked.message_id = message_id
        tracked.last_edit_at = when
        tracked.last_edit_text = text

    async def repost(self, job_id: str) -> bool:
        """/board: forget the old message id and post a fresh board now."""
        tracked = self._jobs.get(job_id)
        if tracked is None:
            return False
        tracked.message_id = None
        tracked.post_failed_at = None
        tracked.last_edit_text = ""
        await self._draw(tracked, force=True)
        if tracked.message_id is not None:
            return True
        # Nothing was posted yet (a job with no report yet): draw again so the
        # caller learns the truth instead of assuming success.
        await self._post(tracked, render(tracked.board, now=self.wall()), self.now())
        return tracked.message_id is not None

    # ------------------------------------------------------------------- time

    async def tick(self, *, queue_ids: Sequence[str] = ()) -> None:
        """Update queue positions and ping a stage that has not moved.

        Called on a timer. A waiting job is not re-pinged: the owner already has
        the instruction. Everything else is silent unless the stall threshold is
        crossed, and then only once per stall.
        """
        if not self._jobs:
            return
        when = self.now()
        order = list(queue_ids)
        for job_id, tracked in list(self._jobs.items()):
            board = tracked.board
            changed = False
            if board.stage == QUEUED:
                position = order.index(job_id) + 1 if job_id in order else 0
                if position != board.queue_position:
                    tracked.board = replace(board, queue_position=position)
                    board = tracked.board
                    changed = True
            if (
                board.terminal
                or board.waiting
                or tracked.stall_pinged
                or self.stall_ping_seconds <= 0
            ):
                if changed or tracked.dirty:
                    await self._draw(tracked)
                continue
            if (when - tracked.last_change_at) >= self.stall_ping_seconds:
                tracked.stall_pinged = True
                if self.pings:
                    mention = (
                        f"<@{tracked.requester_id}> " if tracked.requester_id else ""
                    )
                    try:
                        await self.transport.send(
                            tracked.channel_id, mention + stalled_ping(board)
                        )
                    except Exception:
                        pass
            if changed or tracked.dirty:
                await self._draw(tracked)
