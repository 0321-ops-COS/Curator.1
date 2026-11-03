"""Supervised updates: the idle wait, its refusals, and the startup report."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

OLD, TIP = "a" * 40, "b" * 40


def make_bot(*, refusal="", verify=(True, "OK"), applied=None, supervised=True):
    sup = mock.Mock(head=lambda: OLD, refusal=lambda tip: refusal, verify=lambda tip: verify,
                    pull=mock.Mock(return_value=(True, "")),
                    subjects=lambda old, new: "Fix the wait",
                    note_applied=lambda *args: None, take_applied=lambda: applied)
    b = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
    b.settings = SimpleNamespace(log_dir=Path("."), notify_channel_id=None,
                                 authorized_branch="freebuff/base")
    b.supervisor = sup
    b._supervised, b._update_waiting, b._roadmap_steps = supervised, None, []
    b.queue = SimpleNamespace(running=None, pending_count=0)
    b.freebuff = SimpleNamespace(pending_question=None)
    b.manager = SimpleNamespace(enabled=False)
    b.exits, b.channel = [], mock.AsyncMock()
    b._target_channel = lambda channel_id: b.channel

    async def exit_now() -> None:
        b.exits.append(1)

    b._exit_for_restart = exit_now
    b.interaction = SimpleNamespace(
        channel_id=7, user=SimpleNamespace(id=42),
        response=SimpleNamespace(send_message=mock.AsyncMock()))
    return b

class UpdateTests(unittest.IsolatedAsyncioTestCase):
    def said(self) -> str:
        return self.bot.interaction.response.send_message.await_args.args[0]

    async def test_an_unsupervised_bot_refuses_and_gives_the_manual_command(self) -> None:
        self.bot = make_bot(supervised=False)
        await self.bot.start_update_wait(self.bot.interaction, TIP)
        self.assertIsNone(self.bot._update_waiting)
        self.assertIn("not supervised", self.said())
        self.assertIn("launchctl kickstart", self.said())
    async def test_update_when_idle_waits_for_work_then_restarts(self) -> None:
        self.bot = make_bot()
        self.bot.queue.running = SimpleNamespace(id="job-1", status="running")
        await self.bot.start_update_wait(self.bot.interaction, TIP)
        self.assertIn("job-1", self.said())
        await self.bot._update_wait_tick()
        self.bot.supervisor.pull.assert_not_called()
        self.assertEqual(self.bot.exits, [])
        self.bot.queue.running = None
        await self.bot._update_wait_tick()
        self.assertIn("Restarting at", self.bot.channel.send.await_args.args[0])
        self.bot.supervisor.pull.assert_called_once()
        self.assertEqual(self.bot.exits, [1])
    async def test_a_dirty_tree_refuses_before_pulling_or_restarting(self) -> None:
        self.bot = make_bot(refusal="the checkout has uncommitted changes")
        await self.bot.start_update_wait(self.bot.interaction, TIP)
        await self.bot._update_wait_tick()
        self.assertIn("uncommitted changes", self.bot.channel.send.await_args.args[0])
        self.bot.supervisor.pull.assert_not_called()
        self.assertEqual(self.bot.exits, [])
    async def test_a_failing_suite_blocks_the_pull_and_the_restart(self) -> None:
        self.bot = make_bot(verify=(False, "FAILED (failures=2)"))
        await self.bot.start_update_wait(self.bot.interaction, TIP)
        await self.bot._update_wait_tick()
        text = self.bot.channel.send.await_args.args[0]
        self.assertIn("Staying on", text)
        self.assertIn("FAILED (failures=2)", text)
        self.bot.supervisor.pull.assert_not_called()
        self.assertEqual(self.bot.exits, [])
    async def test_startup_reports_the_update_reposts_reviews_and_the_roadmap(self) -> None:
        self.bot = make_bot(applied={"old": OLD, "new": TIP,
                                     "subjects": "Fix the wait", "channel_id": 7})
        self.bot._roadmap_steps = [SimpleNamespace(job_id="")]
        self.bot._roadmap_restored = 1
        self.bot._repost_reviews = mock.AsyncMock(return_value=["job-2"])
        with mock.patch.object(bot.MacBuilderBot, "user", new_callable=mock.PropertyMock) as user:
            user.return_value = SimpleNamespace(id=1)
            await self.bot.on_ready()
        text = self.bot.channel.send.await_args_list[0].args[0]
        self.assertIn(f"`{OLD[:7]}` -> `{TIP[:7]}`", text)
        self.assertIn("Roadmap restored: 1 job(s) left", text)
        self.bot._repost_reviews.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
