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


def attachment(text: str, filename: str = "sheet.md") -> SimpleNamespace:
    """A fake attachment whose read() hands back the sheet text."""
    async def read() -> bytes:
        return text.encode("utf-8")

    return SimpleNamespace(filename=filename, read=read)


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
        self.bot._roadmap_waiting = {}
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

    async def command(self, user_id: int = 42, action: str | None = None) -> SimpleNamespace:
        """Run /roadmap with no sheet attached, the way a phone sends it."""
        fake = SimpleNamespace(
            user=SimpleNamespace(id=user_id), channel_id=1,
            response=SimpleNamespace(is_done=lambda: False, send_message=mock.AsyncMock()))
        await self.bot.cmd_roadmap(fake, sheet=None, action=action)
        return fake

    def message(self, sheet: str | None = None, filename: str = "sheet.md",
                user_id: int = 42, channel_id: int = 1) -> SimpleNamespace:
        """A message from a person, optionally carrying one attachment."""
        attachments = [attachment(sheet, filename)] if sheet is not None else []
        return SimpleNamespace(
            author=SimpleNamespace(id=user_id, bot=False), guild=SimpleNamespace(id=9),
            channel=SimpleNamespace(id=channel_id, name="general"), content="",
            attachments=attachments, reply=mock.AsyncMock())

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

    async def test_a_denied_user_queues_nothing(self) -> None:
        denied = await self.start(user_id=7)
        self.assertIn("allowlist", denied.response.send_message.await_args.args[0])
        self.assertEqual(self.bot.queue.submitted, [])

    async def test_a_sheet_of_nine_jobs_parses_in_order(self) -> None:
        await self.start("".join(f"## Job\nTask {n}.\n" for n in range(9)))
        self.assertEqual([step.text for step in self.bot._roadmap_steps],
                         [f"Task {n}." for n in range(9)])
        self.assertEqual(len(self.bot.queue.submitted), 1, "still one job at a time")

    async def test_a_sheet_of_thirty_jobs_parses_in_order(self) -> None:
        await self.start("".join(f"## Job\nTask {n}.\n" for n in range(30)))
        self.assertEqual([step.text for step in self.bot._roadmap_steps],
                         [f"Task {n}." for n in range(30)])
        self.assertEqual(len(self.bot.queue.submitted), 1, "still one job at a time")

    async def test_an_empty_sheet_is_still_rejected(self) -> None:
        fake = await self.start("# Build sheet\nNothing under a heading.\n")
        self.assertIn('no "## Job" heading', fake.response.send_message.await_args.args[0])
        self.assertEqual((self.bot.queue.submitted, self.bot._roadmap_steps), ([], []))

    async def test_the_sheet_helper_previews_queues_and_reports(self) -> None:
        preview = await self.bot._roadmap_apply(attachment(SHEET), "preview", 1, 42)
        self.assertTrue(preview.startswith("Preview: 2 job(s), nothing queued or saved"))
        self.assertEqual((self.bot.queue.submitted, self.bot._roadmap_steps), ([], []))
        bad = await self.bot._roadmap_apply(attachment("# Empty\nNo jobs.\n"), "preview", 1, 42)
        self.assertEqual(bad, 'Not previewed: no "## Job" heading with a task under it.')
        queued = await self.bot._roadmap_apply(attachment(SHEET), "start", 1, 42)
        self.assertIn("Roadmap queued: 2 job(s)", queued)
        self.assertEqual(len(self.bot.queue.submitted), 1, "start queues only the first job")

    async def test_a_roadmap_without_a_sheet_waits_for_the_next_message(self) -> None:
        fake = await self.command(action="preview")
        self.assertEqual(fake.response.send_message.await_args.args[0],
                         "Send the .md sheet as your next message in this channel "
                         "within 10 minutes.")
        self.assertEqual(self.bot._roadmap_waiting[42][:2], (1, "preview"))
        message = self.message(SHEET)
        await self.bot.on_message(message)
        self.assertTrue(message.reply.await_args.args[0].startswith("Preview: 2 job(s)"))
        self.assertEqual((self.bot.queue.submitted, self.bot._roadmap_waiting), ([], {}))
        self.assertFalse((Path(self._tmp.name) / "roadmap.json").exists())

    async def test_the_next_message_starts_the_roadmap(self) -> None:
        await self.command()  # no action, no sheet: start
        message = self.message(SHEET)
        await self.bot.on_message(message)
        self.assertIn("Roadmap queued: 2 job(s)", message.reply.await_args.args[0])
        self.assertEqual(len(self.bot.queue.submitted), 1, "one job at a time")
        saved = json.loads((Path(self._tmp.name) / "roadmap.json").read_text("utf-8"))
        self.assertEqual([item["text"] for item in saved], ["Add a button.",
                                                            "Refactor the parser."])
        self.assertEqual(self.bot._roadmap_waiting, {})

    async def test_a_bad_sheet_in_the_next_message_reports_and_stops_waiting(self) -> None:
        await self.command()
        message = self.message("# Empty\nNo jobs.\n")
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0],
                         'Not queued: no "## Job" heading with a task under it.')
        self.assertEqual((self.bot.queue.submitted, self.bot._roadmap_waiting), ([], {}))

    async def test_a_next_message_without_a_md_file_keeps_waiting(self) -> None:
        await self.command()
        self.assertFalse(await self.bot._take_roadmap_sheet(self.message()))
        self.assertFalse(await self.bot._take_roadmap_sheet(
            self.message("# notes", filename="notes.txt")))
        self.assertFalse(await self.bot._take_roadmap_sheet(self.message(SHEET, channel_id=2)))
        self.assertFalse(await self.bot._take_roadmap_sheet(self.message(SHEET, user_id=7)))
        self.assertIn(42, self.bot._roadmap_waiting, "still waiting for the sheet")
        self.assertEqual(self.bot.queue.submitted, [])

    async def test_the_waiting_sheet_expires_after_ten_minutes(self) -> None:
        clock = [1_000_000.0]
        with mock.patch("bot.time.time", lambda: clock[0]):
            await self.command()
            clock[0] += 601
            message = self.message(SHEET)
            self.assertFalse(await self.bot._take_roadmap_sheet(message))
        self.assertEqual(message.reply.await_count, 0)
        self.assertEqual((self.bot.queue.submitted, self.bot._roadmap_waiting), ([], {}))

    async def test_a_new_roadmap_replaces_the_waiting_one_and_stop_clears_it(self) -> None:
        await self.command(action="preview")
        await self.command()
        self.assertEqual(self.bot._roadmap_waiting[42][1], "start")
        await self.command(action="stop")
        self.assertEqual(self.bot._roadmap_waiting, {})


if __name__ == "__main__":
    unittest.main()
