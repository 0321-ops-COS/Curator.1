"""A reviewed task posts exactly one embed, and it says a decision is pending."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(
        self,
        content: str | None = None,
        embed: object = None,
        view: object = None,
    ) -> None:
        self.sent.append({"content": content, "embed": embed, "view": view})


def make_job(kind: str = "task") -> jobs.Job:
    return jobs.Job(
        id="20260101-000000-deadbeef",
        kind=kind,
        title="add a button",
        channel_id=1,
        requester_id=2,
        log_path=Path("/tmp/20260101-000000-deadbeef.log"),
    )


def outcome(job: jobs.Job, verdict: str = "approve") -> jobs.ReviewOutcome:
    return jobs.ReviewOutcome(
        job=job,
        verdict=verdict,
        notes="Fix the label." if verdict != "blocked" else "- Protected path changed: x",
        environment=SimpleNamespace(branch="freebuff/deadbeef"),
        check_state="passed",
        test_state="unavailable (scheme has no test action)",
        diff_lines=4,
        changed_files=("ios-app/RootView.swift",),
    )


class SingleEmbedTests(unittest.IsolatedAsyncioTestCase):
    def bot_with(self, channel: FakeChannel) -> "bot.MacBuilderBot":
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(notify_channel_id=None)
        instance.get_channel = lambda channel_id: channel  # type: ignore[method-assign]
        return instance

    async def test_a_reviewed_task_posts_one_embed_and_awaits_a_decision(self) -> None:
        channel = FakeChannel()
        instance = self.bot_with(channel)
        job = make_job()

        await instance._post_review(outcome(job))
        self.assertEqual(job.status, "awaiting_review")

        await instance._post_job_result(job, True, "review: approve")

        self.assertEqual(len(channel.sent), 1, "the review embed is the result")
        embed = channel.sent[0]["embed"]
        self.assertIsNotNone(embed)
        self.assertIn("waiting for your decision", embed.title)  # type: ignore[attr-defined]

    async def test_a_blocked_change_does_not_add_a_second_embed(self) -> None:
        channel = FakeChannel()
        instance = self.bot_with(channel)
        job = make_job()

        await instance._post_review(outcome(job, verdict="blocked"))
        self.assertEqual(job.status, "blocked")
        await instance._post_job_result(job, False, "Blocked by rules")

        self.assertEqual(len(channel.sent), 1)

    async def test_a_task_without_a_review_still_reports_its_result(self) -> None:
        channel = FakeChannel()
        instance = self.bot_with(channel)

        await instance._post_job_result(make_job(), False, "Freebuff timed out")

        self.assertEqual(len(channel.sent), 1)
        embed = channel.sent[0]["embed"]
        self.assertIn("timed out", embed.description)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
