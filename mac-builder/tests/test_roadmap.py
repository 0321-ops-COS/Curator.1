"""The /roadmap sheet: one job at a time, stepped by the job's own status."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

SHEET = """# Build sheet
## Job
Add a button.
kind: task
## Job 2: the parser
Refactor the parser.
kind: dan
"""


class FakeQueue:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def submit(self, job: object) -> int:
        self.submitted.append(job)
        return len(self.submitted)


class RoadmapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.channel = mock.AsyncMock()
        self.bot = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.bot.settings = SimpleNamespace(log_dir=Path(self._tmp.name), notify_channel_id=None,
                                            allowed_user_ids=frozenset({42}))
        self.bot.queue = FakeQueue()
        self.bot._board = None
        self.bot._roadmap_steps, self.bot._roadmap_jobs = [], {}
        self.bot._roadmap_job, self.bot._roadmap_channel_id = None, 0
        self.bot._roadmap_requester_id = 0
        self.bot._roadmap_reminder = {}
        self.bot._target_channel = lambda channel_id: self.channel  # type: ignore[method-assign]

    async def start(self, sheet: str = SHEET, user_id: int = 42,
                    action: str | None = None) -> SimpleNamespace:
        async def read() -> bytes:
            return sheet.encode("utf-8")

        fake = SimpleNamespace(
            user=SimpleNamespace(id=user_id), channel_id=1,
            response=SimpleNamespace(is_done=lambda: False, send_message=mock.AsyncMock()))
        await self.bot.cmd_roadmap(fake, sheet=SimpleNamespace(filename="sheet.md", read=read),
                                   action=action)
        return fake

    async def test_the_next_job_queues_only_after_the_first_is_approved(self) -> None:
        await self.start()
        first = self.bot.queue.submitted[0]
        self.assertEqual((first.kind, first.text), ("task", "Add a button."))
        first.status = "awaiting_review"
        await self.bot._roadmap_tick()
        self.assertEqual(len(self.bot.queue.submitted), 1, "a review is not an approval")
        first.status = "approved"
        await self.bot._roadmap_tick()
        second = self.bot.queue.submitted[1]
        self.assertEqual((second.kind, second.text), ("dan", "Refactor the parser."))
        saved = json.loads((Path(self._tmp.name) / "roadmap.json").read_text("utf-8"))
        self.assertEqual([item["kind"] for item in saved], ["task", "dan"])
        second.status = "approved"
        await self.bot._roadmap_tick()
        self.assertTrue(self.channel.send.await_args.args[0].endswith("roadmap done"))

    async def test_a_failure_stops_the_roadmap_and_stop_drops_the_rest(self) -> None:
        await self.start()
        first = self.bot.queue.submitted[0]
        first.status = "failed"
        await self.bot._roadmap_tick()
        ping = self.channel.send.await_args.args[0]
        self.assertEqual((len(self.bot.queue.submitted), first.id in ping,
                          "failed" in ping, self.bot._roadmap_steps), (1, True, True, []))
        await self.start(SHEET)
        self.assertIn("1 job", self.bot._roadmap_stop())
        self.assertEqual(json.loads((Path(self._tmp.name) / "roadmap.json").read_text()), [])
        self.bot.queue.submitted[1].status = "approved"
        await self.bot._roadmap_tick()
        self.assertEqual(len(self.bot.queue.submitted), 2)

    async def test_preview_lists_the_plan_and_queues_nothing(self) -> None:
        fake = await self.start(action="preview")
        text = fake.response.send_message.await_args.args[0]
        self.assertEqual(self.bot.queue.submitted, [])
        self.assertFalse((Path(self._tmp.name) / "roadmap.json").exists(), "nothing was saved")
        self.assertEqual(text.splitlines()[:2],
                         ["Preview: 2 job(s), nothing queued or saved", "1. `task` Add a button."])
        self.assertIn("2. `dan` Refactor the parser.", text)

    async def test_preview_reports_a_bad_sheet(self) -> None:
        fake = await self.start("# Empty\nNo jobs here.\n", action="preview")
        text = fake.response.send_message.await_args.args[0]
        self.assertEqual(self.bot.queue.submitted, [])
        self.assertIn('no "## Job" heading', text)
        self.assertFalse((Path(self._tmp.name) / "roadmap.json").exists())

    async def test_a_waiting_review_is_pinged_every_six_hours_until_approved(self) -> None:
        await self.start()
        first = self.bot.queue.submitted[0]
        first.status = "awaiting_review"
        clock = [1_000_000.0]
        with mock.patch("bot.time.time", lambda: clock[0]):
            await self.bot._roadmap_tick()  # schedules the first reminder
            self.assertEqual(self.channel.send.await_count, 0)
            clock[0] += 6 * 3600 - 1
            await self.bot._roadmap_tick()
            self.assertEqual(self.channel.send.await_count, 0, "not yet six hours")
            clock[0] += 1
            await self.bot._roadmap_tick()
            self.assertEqual(self.channel.send.await_args.args[0],
                             f"Roadmap waiting on your Approve: `{first.id}`")
            clock[0] += 6 * 3600
            await self.bot._roadmap_tick()
            self.assertEqual(self.channel.send.await_count, 2, "one ping per six hours")
            first.status = "approved"
            await self.bot._roadmap_tick()  # the reminder stops; the next job queues
            self.assertEqual((self.channel.send.await_count, len(self.bot.queue.submitted)),
                             (2, 2))

    async def test_a_ninth_job_and_a_denied_user_queue_nothing(self) -> None:
        fake = await self.start("".join(f"## Job\nTask {n}.\n" for n in range(9)))
        denied = await self.start(user_id=7)
        self.assertIn("limit is 8", fake.response.send_message.await_args.args[0])
        self.assertIn("allowlist", denied.response.send_message.await_args.args[0])
        self.assertEqual(self.bot.queue.submitted, [])


if __name__ == "__main__":
    unittest.main()
