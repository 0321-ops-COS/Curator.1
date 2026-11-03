"""The draft fix sheet and its card: edit, remove, replace, discard and start."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit  # noqa: E402
import bot  # noqa: E402
import roadmap  # noqa: E402
import sheet_card  # noqa: E402

SHEET = "## Job\nFix the parser.\n## Job\nkind: dan\nRefresh the docs.\n"


class CardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.channel = mock.AsyncMock()
        self.channel.send.return_value = SimpleNamespace(id=101)
        self.channel.fetch_message.return_value.edit = mock.AsyncMock()
        self.bot = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.bot.settings = SimpleNamespace(log_dir=Path(self._tmp.name),
                                            allowed_user_ids=frozenset({42}),
                                            authorized_branch="freebuff/base",
                                            notify_channel_id=None)
        self.submitted: list[object] = []

        async def submit(job: object) -> int:
            self.submitted.append(job)
            return len(self.submitted)

        self.bot.queue = SimpleNamespace(waiting=lambda: [], submit=submit)
        self.bot._board, self.bot._roadmap_steps, self.bot._roadmap_jobs = None, [], {}
        self.bot._roadmap_job = self.bot._roadmap_channel_id = 0
        self.bot._roadmap_requester_id = 0
        self.bot._roadmap_paused, self.bot._roadmap_waiting = False, {}
        self.bot._sheet_draft = sheet_card.Draft(roadmap.parse(SHEET), 1, 42)
        self.bot._target_channel = lambda channel_id: self.channel

    async def test_edit_remove_replace_and_discard_change_only_the_draft(self) -> None:
        self.bot._sheet_draft.message_id = 100
        self.assertIn("Job 1 saved", await sheet_card.edit_job(self.bot, 0, "Only this."))
        self.assertEqual([s.text for s in self.bot._sheet_draft.steps],
                         ["Only this.", "Refresh the docs."])
        self.assertIn("Not saved", await sheet_card.edit_job(self.bot, 0, "  "))
        self.assertEqual(self.bot._sheet_draft.steps[0].text, "Only this.",
                         "a refused edit changes nothing")
        self.assertIn("Removed job 1", await sheet_card.remove_job(self.bot, 0))

        async def read() -> bytes:
            return b"## Job\nOnly this.\n"

        self.assertIn("Draft sheet replaced: 1 job(s)", await self.bot._roadmap_apply(
            SimpleNamespace(filename="fixes.md", read=read), "draft", 1, 42))
        self.assertEqual([s.text for s in self.bot._sheet_draft.steps], ["Only this."])
        await sheet_card.discard_draft(self.bot)
        self.assertIsNone(self.bot._sheet_draft)
        self.assertFalse((Path(self._tmp.name) / sheet_card.DRAFT_FILE).exists())
        self.assertEqual(self.submitted, [], "no control queues a job")

    async def test_approve_refuses_an_active_roadmap_then_queues_only_the_first(self) -> None:
        self.bot._roadmap_steps = [roadmap.Step("Running one.", "task", "j1")]
        self.assertIn("already active", await sheet_card.start_draft(self.bot, 1, 42))
        self.assertEqual(self.submitted, [])
        self.bot._roadmap_steps = []
        self.assertIn("Roadmap queued: 2 job(s)", await sheet_card.start_draft(self.bot, 1, 42))
        self.assertEqual([job.kind for job in self.submitted], ["task"], "only the first queues")
        self.assertIsNone(self.bot._sheet_draft)
        self.assertTrue((Path(self._tmp.name) / "roadmap.json").exists())

    async def test_a_saved_draft_is_restored_with_its_card_after_a_restart(self) -> None:
        sheet_card.save(self.bot._sheet_draft_path(), self.bot._sheet_draft)
        restarted = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        restarted.settings = self.bot.settings
        restarted._target_channel = lambda channel_id: self.channel
        restarted._sheet_draft = sheet_card.load(restarted._sheet_draft_path())
        await restarted._restore_sheet_draft()
        self.assertIn("Restored after a restart", self.channel.send.await_args.args[0])
        self.assertEqual(restarted._sheet_draft.message_id, 101)


class FixSheetTests(unittest.TestCase):
    def test_failed_stages_become_jobs_under_the_modal_limit(self) -> None:
        stage = SimpleNamespace(name="check", state="failed", code=65, tail=["x.swift:3: error: nope"])
        results = [SimpleNamespace(branch="freebuff/aaaa", error="", stages=[stage]),
                   SimpleNamespace(branch="freebuff/bbbb", error="no worktree", stages=[])]
        steps = roadmap.parse(audit.fix_sheet("s", results))
        self.assertEqual([step.kind for step in steps], ["task", "task"])
        self.assertIn("x.swift:3", steps[0].text)
        self.assertLess(max(len(step.text) for step in steps), sheet_card.FIELD_LIMIT)

if __name__ == "__main__":
    unittest.main()
