"""The /selftest: every stage on fakes, and a broken stage reported as one."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import roadmap  # noqa: E402
import selftest  # noqa: E402

STAGES = ["Intake", "Report and test card", "Board and pings", "Gates", "Guards"]


async def _no_queue(job: object) -> int:
    """The real queue must never be touched, so any submit fails the test."""
    raise AssertionError("the selftest must never use the real queue")


class SelftestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bot = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.bot.settings = SimpleNamespace(log_dir=Path(self._tmp.name), notify_channel_id=None,
                                            allowed_user_ids=frozenset({42}))
        self.bot.queue = SimpleNamespace(submit=_no_queue)

    async def cmd(self, user_id: int = 42) -> list[str]:
        sent: list[str] = []

        async def send(text: str, **_kwargs: object) -> None:
            sent.append(text)

        fake = SimpleNamespace(user=SimpleNamespace(id=user_id), followup=SimpleNamespace(send=send),
                               response=SimpleNamespace(is_done=lambda: False, send_message=send))
        await self.bot.cmd_selftest(fake)
        return sent

    async def test_every_stage_passes_and_names_what_is_not_proven(self) -> None:
        stages = await selftest.run(self.bot, bot.dan_report_text)
        self.assertEqual([stage.name for stage in stages], STAGES)
        self.assertEqual([stage.ok for stage in stages], [True] * len(STAGES))
        text = selftest.report(stages)
        self.assertEqual((text.count(": pass"), "Proven: " in text), (len(STAGES), True))
        self.assertIn("Not proven: live Freebuff, live Discord delivery, live GitHub," , text)

    async def test_a_broken_stage_is_reported_as_a_failure(self) -> None:
        with mock.patch.object(selftest.roadmap, "parse",
                               return_value=[roadmap.Step("one", "task", "")]):
            stages = await selftest.run(self.bot, bot.dan_report_text)
        text = selftest.report(stages)
        self.assertEqual((stages[0].ok, "❌ Intake: FAIL" in text), (False, True))

    async def test_a_stage_that_raises_is_a_failure_not_a_crash(self) -> None:
        with mock.patch.object(selftest.roadmap, "parse", side_effect=ValueError("bad sheet")):
            stages = await selftest.run(self.bot, bot.dan_report_text)
        self.assertEqual((len(stages), stages[0].ok, stages[0].evidence),
                         (len(STAGES), False, "raised ValueError: bad sheet"))

    async def test_a_denied_user_gets_one_line_and_runs_no_stage(self) -> None:
        with mock.patch.object(selftest, "run", mock.AsyncMock()) as run:
            sent = await self.cmd(user_id=7)
        self.assertEqual((len(sent), "allowlist" in sent[0], run.await_count), (1, True, 0))


if __name__ == "__main__":
    unittest.main()
