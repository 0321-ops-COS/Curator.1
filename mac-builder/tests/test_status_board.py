"""The status board: real stages, real time, and pings only when they matter.

The board is a function of its inputs and an injected clock; the publisher takes
an injected transport, so throttling, a deleted message and every ping rule are
testable with no Discord, no network and no job running.
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
import jobs  # noqa: E402
import screens  # noqa: E402
import status_board  # noqa: E402

JOB_ID = "20261102-101500-deadbeef"
WAITING_ID = "20261102-101600-cafebabe"
OWNER = 7


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTransport(status_board.BoardTransport):
    """Records messages instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, str, str]] = []
        self.attempts: list[tuple[int, str, str]] = []
        self.next_id = 0
        self.send_ok = True
        self.edit_ok = True
        self.raise_on_send = False

    async def send(self, channel_id: int, text: str) -> str | None:
        if self.raise_on_send:
            raise RuntimeError("discord is down")
        if not self.send_ok:
            return None
        self.next_id += 1
        self.sent.append((channel_id, text))
        return f"m{self.next_id}"

    async def edit(self, channel_id: int, message_id: str, text: str) -> bool:
        self.attempts.append((channel_id, message_id, text))
        if not self.edit_ok:
            return False
        self.edits.append((channel_id, message_id, text))
        return True

    @property
    def pings(self) -> list[str]:
        return [text for _channel, text in self.sent if text.startswith("<@")]

    @property
    def boards(self) -> list[str]:
        return [text for _channel, text in self.sent if not text.startswith("<@")]


def make_board(**overrides: object) -> status_board.Board:
    values: dict[str, object] = dict(
        job_id=JOB_ID,
        kind="task",
        project="Hustler",
        stage=status_board.FREEBUFF_WORKING,
        started_at=0.0,
    )
    values.update(overrides)
    return status_board.Board(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------------- model


class StageTests(unittest.TestCase):
    def test_the_stage_order_is_the_pipeline(self) -> None:
        self.assertEqual(
            status_board.STAGES,
            (
                "queued",
                "branch_ready",
                "freebuff_working",
                "waiting_on_you",
                "checks",
                "tests",
                "manager_review",
                "awaiting_approve",
                "pushed",
                "rejected",
                "failed",
                "cancelled",
                "timed_out",
            ),
        )
        self.assertEqual(len(status_board.PROGRESS_STAGES), 7)

    def test_the_bar_fill_is_the_stage_number(self) -> None:
        self.assertEqual(status_board.step(status_board.QUEUED), 1)
        self.assertEqual(status_board.step(status_board.FREEBUFF_WORKING), 3)
        self.assertEqual(status_board.step(status_board.AWAITING_APPROVE), 7)
        self.assertEqual(status_board.step(status_board.PUSHED), 0)
        self.assertEqual(
            status_board.bar(3), "▰▰▰" + "▱" * 4
        )
        self.assertEqual(status_board.bar(0), "▱" * 7)

    def test_rendering_names_the_job_stage_and_bar(self) -> None:
        text = status_board.render(make_board(started_at=0.0), now=724.0)
        self.assertIn(JOB_ID, text)
        self.assertIn("task", text)
        self.assertIn("Hustler", text)
        self.assertIn("3/7 Freebuff working", text)
        self.assertIn("▰▰▰", text)
        self.assertIn("12m 04s", text)

    def test_elapsed_time_is_real_and_from_the_injected_now(self) -> None:
        self.assertEqual(status_board.format_elapsed(8), "8s")
        self.assertEqual(status_board.format_elapsed(724), "12m 04s")
        self.assertEqual(status_board.format_elapsed(3725), "1h 02m")
        self.assertEqual(status_board.format_elapsed(None), "")
        queued = status_board.render(make_board(stage=status_board.QUEUED, started_at=None))
        self.assertNotIn("s", queued.splitlines()[1].replace("queued", ""))

    def test_a_terminal_stage_keeps_the_bar_where_it_stopped(self) -> None:
        board = make_board(stage=status_board.FAILED, progress=4)
        text = status_board.render(board, now=10.0)
        self.assertIn("4/7 failed", text)

    def test_queue_position_is_shown_for_a_waiting_job(self) -> None:
        text = status_board.render(
            make_board(stage=status_board.QUEUED, started_at=None, queue_position=2)
        )
        self.assertIn("queued #2", text)

    def test_waiting_lines_are_position_kind_and_preview(self) -> None:
        text = status_board.render_waiting(
            [(1, JOB_ID, "task", "make the card bigger"), (2, WAITING_ID, "dan", "fix tests")]
        )
        self.assertIn(f"1. `{JOB_ID}` · task · make the card bigger", text)
        self.assertIn(f"2. `{WAITING_ID}` · dan · fix tests", text)
        self.assertEqual(status_board.render_waiting([]), "nothing is waiting.")

    def test_a_script_job_has_no_bar(self) -> None:
        text = status_board.render(make_board(kind="check", stage=status_board.CHECKS))
        self.assertNotIn("▰", text)

    def test_the_board_idles_are_never_invented(self) -> None:
        for kind in screens.KINDS:
            self.assertIn(kind, status_board.WAITING_COMMANDS, kind)
        self.assertIn("`/screen`", status_board.waiting_for("not-a-kind"))


class WaitingReasonTests(unittest.TestCase):
    """Every screen kind names the exact command, and no kind is invented."""

    def test_question_box_names_answer(self) -> None:
        self.assertIn("/answer", status_board.waiting_for(screens.QUESTION_BOX))

    def test_sign_in_names_screen_key_send(self) -> None:
        reason = status_board.waiting_for(screens.SIGN_IN)
        for command in ("/screen", "/key", "/send"):
            self.assertIn(command, reason)

    def test_rate_limit_names_screen_or_cancel(self) -> None:
        reason = status_board.waiting_for(screens.RATE_LIMIT)
        self.assertIn("/screen", reason)

    def test_update_prompt_names_a_key(self) -> None:
        self.assertIn("/key", status_board.waiting_for(screens.UPDATE_PROMPT))

    def test_confirm_prompt_names_y_and_n(self) -> None:
        reason = status_board.waiting_for(screens.CONFIRM_PROMPT)
        self.assertIn("/key y", reason)
        self.assertIn("/key n", reason)

    def test_crash_and_unknown_name_screen(self) -> None:
        self.assertIn("/screen", status_board.waiting_for(screens.ERROR_OR_CRASH))
        self.assertIn("/screen", status_board.waiting_for(screens.UNKNOWN))
        self.assertIn("/screen", status_board.waiting_for(""))

    def test_the_kind_is_always_shown_as_looks_like(self) -> None:
        text = status_board.render(make_board(screen_kind=screens.SIGN_IN))
        self.assertIn("looks like: sign_in", text)


# --------------------------------------------------------------- publisher


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.wall = Clock(0.0)
        self.transport = FakeTransport()
        self.board = status_board.BoardPublisher(
            self.transport,
            edit_min_seconds=15.0,
            stall_ping_seconds=600.0,
            pings=True,
            now=self.clock,
            wall=self.wall,
        )
        self.board.track(
            JOB_ID,
            channel_id=99,
            requester_id=OWNER,
            kind="task",
            project="Hustler",
        )

    async def test_the_first_report_posts_one_message(self) -> None:
        await self.board.report(JOB_ID, status_board.BRANCH_READY, "freebuff/abc")
        self.assertEqual(len(self.transport.boards), 1)
        self.assertIn("branch ready", self.transport.boards[0])
        self.assertEqual(self.transport.edits, [])

    async def test_edits_are_throttled_and_reuse_the_same_message(self) -> None:
        await self.board.report(JOB_ID, status_board.BRANCH_READY)
        self.clock.advance(5)
        await self.board.report(JOB_ID, status_board.FREEBUFF_WORKING)
        self.assertEqual(self.transport.edits, [], "too soon to edit again")
        self.clock.advance(20)
        await self.board.report(JOB_ID, status_board.CHECKS)
        self.assertEqual(len(self.transport.edits), 1)
        self.assertEqual(self.transport.edits[0][1], "m1", "the same message id")
        self.assertIn("checks", self.transport.edits[0][2])

    async def test_a_deleted_message_is_replaced_with_one_new_post(self) -> None:
        await self.board.report(JOB_ID, status_board.BRANCH_READY)
        self.transport.edit_ok = False
        self.clock.advance(20)
        await self.board.report(JOB_ID, status_board.CHECKS)
        self.assertEqual(len(self.transport.boards), 2, "one fresh board")
        self.transport.edit_ok = True
        self.clock.advance(20)
        await self.board.report(JOB_ID, status_board.TESTS)
        self.assertEqual(self.transport.edits[-1][1], "m2", "the new message id")

    async def test_a_failed_post_is_not_retried_in_a_busy_loop(self) -> None:
        self.transport.send_ok = False
        await self.board.report(JOB_ID, status_board.BRANCH_READY)
        self.clock.advance(2)
        await self.board.report(JOB_ID, status_board.FREEBUFF_WORKING)
        self.assertEqual(self.transport.boards, [])
        self.transport.send_ok = True
        self.clock.advance(20)
        await self.board.report(JOB_ID, status_board.CHECKS)
        self.assertEqual(len(self.transport.boards), 1)

    async def test_a_transport_that_raises_never_reaches_the_caller(self) -> None:
        self.transport.raise_on_send = True
        await self.board.report(JOB_ID, status_board.BRANCH_READY)
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="sign_in")
        await self.board.tick(queue_ids=[JOB_ID])
        self.assertEqual(self.transport.boards, [])

    async def test_repost_posts_a_fresh_message(self) -> None:
        await self.board.report(JOB_ID, status_board.BRANCH_READY)
        self.assertTrue(await self.board.repost(JOB_ID))
        self.assertEqual(len(self.transport.boards), 2)
        self.assertFalse(await self.board.repost("not-tracked"))

    async def test_an_untracked_job_is_ignored(self) -> None:
        await self.board.report("other-job", status_board.FAILED)
        self.assertEqual(self.transport.sent, [])


class PingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.wall = Clock(0.0)
        self.transport = FakeTransport()
        self.board = status_board.BoardPublisher(
            self.transport,
            edit_min_seconds=15.0,
            stall_ping_seconds=600.0,
            pings=True,
            now=self.clock,
            wall=self.wall,
        )
        self.board.track(
            JOB_ID, channel_id=99, requester_id=OWNER, kind="task"
        )

    async def test_routine_stages_never_ping(self) -> None:
        for stage in (
            status_board.BRANCH_READY,
            status_board.FREEBUFF_WORKING,
            status_board.CHECKS,
            status_board.TESTS,
            status_board.MANAGER_REVIEW,
        ):
            await self.board.report(JOB_ID, stage)
        self.assertEqual(self.transport.pings, [])
        self.assertTrue(self.transport.boards, "the board is still posted")

    async def test_waiting_on_you_pings_once_with_the_instruction(self) -> None:
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="sign_in")
        self.assertEqual(len(self.transport.pings), 1)
        ping = self.transport.pings[0]
        self.assertTrue(ping.startswith(f"<@{OWNER}>"), ping)
        self.assertIn("sign_in", ping)
        self.assertIn("/screen", ping)

    async def test_the_same_screen_kind_does_not_ping_twice(self) -> None:
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="sign_in")
        self.clock.advance(20)
        await self.board.report(
            JOB_ID, status_board.WAITING_ON_YOU, detail="again", screen_kind="sign_in"
        )
        self.assertEqual(len(self.transport.pings), 1)
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="rate_limit")
        self.assertEqual(len(self.transport.pings), 2, "a different kind is a new task")

    async def test_a_different_screen_kind_pings(self) -> None:
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="question_box")
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="confirm_prompt")
        self.assertEqual(len(self.transport.pings), 2)
        self.assertIn("/key y", self.transport.pings[1])

    async def test_approve_finish_failure_and_timeout_ping(self) -> None:
        for stage, word in (
            (status_board.AWAITING_APPROVE, "Approve"),
            (status_board.PUSHED, "finished"),
            (status_board.FAILED, "failed"),
            (status_board.TIMED_OUT, "timed out"),
            (status_board.REJECTED, "rejected"),
        ):
            self.board.forget(JOB_ID)
            self.board.track(JOB_ID, channel_id=99, requester_id=OWNER, kind="task")
            await self.board.report(JOB_ID, stage, "detail")
            self.assertIn(word, self.transport.pings[-1], stage)

    async def test_status_pings_off_keeps_the_board_and_sends_nothing(self) -> None:
        board = status_board.BoardPublisher(
            self.transport,
            edit_min_seconds=15.0,
            stall_ping_seconds=600.0,
            pings=False,
            now=self.clock,
            wall=self.wall,
        )
        board.track(JOB_ID, channel_id=99, requester_id=OWNER, kind="task")
        await board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="sign_in")
        await board.report(JOB_ID, status_board.FAILED, "boom")
        self.assertEqual(self.transport.pings, [])
        self.assertTrue(self.transport.boards)

    async def test_a_stall_pings_once_per_stall_and_again_after_progress(self) -> None:
        await self.board.report(JOB_ID, status_board.FREEBUFF_WORKING)
        self.clock.advance(601)
        await self.board.tick()
        self.assertEqual(len(self.transport.pings), 1)
        self.assertIn("has not moved", self.transport.pings[0])
        self.clock.advance(601)
        await self.board.tick()
        self.assertEqual(len(self.transport.pings), 1, "once per stall")

        self.clock.advance(20)
        await self.board.report(JOB_ID, status_board.CHECKS)
        self.clock.advance(601)
        await self.board.tick()
        self.assertEqual(len(self.transport.pings), 2, "a new stall after progress")

    async def test_a_waiting_job_is_not_stall_pinged(self) -> None:
        await self.board.report(JOB_ID, status_board.WAITING_ON_YOU, screen_kind="sign_in")
        self.clock.advance(3600)
        await self.board.tick()
        self.assertEqual(len(self.transport.pings), 1, "the owner already has it")

    async def test_queue_positions_follow_the_queue(self) -> None:
        self.board.forget(JOB_ID)
        self.board.track(
            JOB_ID, channel_id=99, requester_id=OWNER, kind="task", queue_position=1
        )
        await self.board.report(JOB_ID, status_board.QUEUED)
        self.clock.advance(20)
        await self.board.tick(queue_ids=[WAITING_ID, JOB_ID])
        board = self.board.board_for(JOB_ID)
        assert board is not None
        self.assertEqual(board.queue_position, 2)
        self.assertIn("queued #2", self.transport.edits[-1][2])


# --------------------------------------------------------------- the hook


class _StageFreebuff:
    """A runner stand-in that stops on a timeout, with no session at all."""

    def __init__(self, reason: str = "timeout") -> None:
        self.reason = reason
        self.screen_reporter = None
        self.status_reporter = None

    async def start_task(
        self, *, job_id: str, task_text: str, log_path: Path, kind: str = "task"
    ) -> jobs.TaskEnvironment:
        return jobs.TaskEnvironment(
            branch="freebuff/deadbeef",
            worktree=Path("/nonexistent/worktree"),
            session="macbuilder-test",
            base_branch="freebuff/base",
            signature_at_start="a",
        )

    async def wait_for_changes(self, env: object, log_path: Path, **kwargs: object) -> str:
        return self.reason

    async def collect(self, env: object, max_diff_lines: int) -> object:
        return SimpleNamespace(empty=True, diff="", diff_lines=0)

    async def abort(self, env: object | None = None) -> None:
        return None


class _NoManager:
    enabled = False


def make_job(tmp: str) -> jobs.Job:
    return jobs.Job(
        id=JOB_ID,
        kind="task",
        title="make the card bigger",
        channel_id=1,
        requester_id=OWNER,
        log_path=Path(tmp) / "job.log",
        text="make the card bigger",
    )


class StageHookTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, *, hook) -> tuple[jobs.Job, list[str]]:
        posted: list[str] = []

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            posted.append(summary)

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            raise AssertionError("a timed-out job has no review")

        runner = jobs.JobRunner(
            SimpleNamespace(
                max_diff_lines=400,
                max_revise_rounds=0,
                question_wait_seconds=1800,
                question_auto_answer=False,
            ),
            _NoManager(),
            _StageFreebuff(),
            post_job=post_job,
            post_review=post_review,
            on_stage=hook,
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(tmp)
            await runner.run(job)
        return job, posted

    async def test_a_failing_status_hook_never_touches_the_job(self) -> None:
        def explode(job: jobs.Job, stage: str, detail: str) -> None:
            raise RuntimeError("status is broken")

        job, posted = await self.run_job(hook=explode)

        self.assertEqual(job.status, "failed")
        self.assertIn("timed out", posted[0])

    async def test_the_hook_receives_the_real_stages(self) -> None:
        stages: list[str] = []

        def record(job: jobs.Job, stage: str, detail: str) -> None:
            stages.append(stage)

        await self.run_job(hook=record)

        self.assertIn(status_board.TIMED_OUT, stages)
        self.assertIn(status_board.FAILED, stages)


# --------------------------------------------------------------------- bot


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = 0

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)

    async def defer(self, **_kwargs: object) -> None:
        self.deferred += 1


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=OWNER)
        self.channel_id = 99
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def job_ref(job_id: str, kind: str = "task", title: str = "make the card bigger"):
    return SimpleNamespace(
        id=job_id,
        kind=kind,
        title=title,
        started=None,
        requester_id=OWNER,
        channel_id=99,
        duration=None,
    )


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    def instance(
        self,
        *,
        allowed: bool = True,
        running: object | None = None,
        waiting: tuple[object, ...] = (),
        board: object | None = None,
    ) -> bot.MacBuilderBot:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            log_dir=Path("/nonexistent/logs"),
            notify_channel_id=None,
            authorized_branch="freebuff/base",
            project=SimpleNamespace(project_name="Hustler", target_remote="origin"),
            question_wait_seconds=1800.0,
            question_auto_answer=False,
            question_auto_answer_max=3,
            question_suggest=False,
            question_debug=False,
        )
        instance.manager = SimpleNamespace(enabled=False)
        instance.queue = SimpleNamespace(
            running=running,
            waiting=lambda: list(waiting),
            snapshot=lambda: {"running": running, "pending": len(waiting)},
        )
        instance._board = board or status_board.BoardPublisher(
            FakeTransport(), edit_min_seconds=0
        )

        async def check(_interaction: object) -> bool:
            return allowed

        instance.require_allowed = check
        return instance

    async def reported_board(self) -> tuple[status_board.BoardPublisher, FakeTransport]:
        transport = FakeTransport()
        publisher = status_board.BoardPublisher(
            transport, edit_min_seconds=0, now=Clock(), wall=Clock(0.0)
        )
        publisher.track(
            JOB_ID, channel_id=99, requester_id=OWNER, kind="task", project="Hustler"
        )
        await publisher.report(JOB_ID, status_board.FREEBUFF_WORKING, "task sent")
        return publisher, transport

    async def test_status_shows_the_board_and_the_waiting_lines(self) -> None:
        publisher, _transport = await self.reported_board()
        instance = self.instance(
            running=job_ref(JOB_ID),
            waiting=(job_ref(WAITING_ID, "dan", "fix the tests"),),
            board=publisher,
        )
        interaction = _FakeInteraction()

        probe = mock.AsyncMock(return_value=(0, "status output"))
        with mock.patch.object(bot.jobs, "probe_status", probe):
            await instance.cmd_status(interaction)

        text = interaction.followup.messages[0]
        self.assertIn(JOB_ID, text)
        self.assertIn("3/7 Freebuff working", text)
        self.assertIn(f"1. `{WAITING_ID}` · dan · fix the tests", text)
        self.assertIn("project: Hustler", text)

    async def test_status_with_no_job_says_so(self) -> None:
        instance = self.instance()
        interaction = _FakeInteraction()

        probe = mock.AsyncMock(return_value=(0, "status output"))
        with mock.patch.object(bot.jobs, "probe_status", probe):
            await instance.cmd_status(interaction)

        self.assertIn("No job is running", interaction.followup.messages[0])

    async def test_board_reposts_the_running_job(self) -> None:
        publisher, transport = await self.reported_board()
        instance = self.instance(running=job_ref(JOB_ID), board=publisher)
        interaction = _FakeInteraction()

        await instance.cmd_board(interaction)

        self.assertIn("Posted a fresh board", interaction.response.messages[0])
        self.assertEqual(len(transport.boards), 2)

    async def test_board_with_no_running_job_says_so(self) -> None:
        instance = self.instance()
        interaction = _FakeInteraction()

        await instance.cmd_board(interaction)

        self.assertIn("No job is running", interaction.response.messages[0])

    async def test_an_unallowlisted_user_gets_nothing(self) -> None:
        publisher, transport = await self.reported_board()
        for command in ("status", "board"):
            instance = self.instance(
                allowed=False, running=job_ref(JOB_ID), board=publisher
            )
            interaction = _FakeInteraction()
            if command == "status":
                await instance.cmd_status(interaction)
            else:
                await instance.cmd_board(interaction)
            self.assertEqual(interaction.response.messages, [], command)
            self.assertEqual(interaction.followup.messages, [], command)
        self.assertEqual(len(transport.boards), 1, "no board was posted for a stranger")


if __name__ == "__main__":
    unittest.main()
