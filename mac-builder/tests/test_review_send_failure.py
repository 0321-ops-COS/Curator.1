"""A review Discord rejects must not take the job's result down with it."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402
from freebuff_runner import ChangeSet, TaskEnvironment  # noqa: E402

BRANCH = "freebuff/deadbeef"


class FlakyChannel:
    """Discord rejects the review, then accepts the fallback result."""

    def __init__(self, failures: int = 1) -> None:
        self.sent: list[dict[str, object]] = []
        self.remaining = failures

    async def send(
        self, content: str | None = None, embed: object = None, view: object = None
    ) -> None:
        if self.remaining:
            self.remaining -= 1
            raise RuntimeError("500 Internal Server Error")
        self.sent.append({"content": content, "embed": embed, "view": view})


def make_job(tmp: str = "/tmp") -> jobs.Job:
    return jobs.Job(
        id="20260101-000000-deadbeef",
        kind="task",
        title="add a button",
        channel_id=1,
        requester_id=2,
        log_path=Path(tmp) / "20260101-000000-deadbeef.log",
        text="do the thing",
    )


def outcome(job: jobs.Job, verdict: str = "approve") -> jobs.ReviewOutcome:
    return jobs.ReviewOutcome(
        job=job,
        verdict=verdict,
        notes="Fix the label." if verdict != "blocked" else "- Protected path changed: x",
        environment=SimpleNamespace(branch=BRANCH),
        check_state="passed",
        test_state="unavailable (scheme has no test action)",
        diff_lines=4,
        changed_files=("ios-app/RootView.swift",),
    )


NOTE = (
    "Review could not be posted to Discord (RuntimeError). "
    f"Branch {BRANCH} and its worktree are kept."
)


class ReviewSendFailureTests(unittest.IsolatedAsyncioTestCase):
    def bot_with(self, channel: FlakyChannel) -> "bot.MacBuilderBot":
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(notify_channel_id=None)
        instance.get_channel = lambda channel_id: channel  # type: ignore[method-assign]
        return instance

    async def test_a_failed_review_send_still_reports_the_job(self) -> None:
        channel = FlakyChannel()
        instance = self.bot_with(channel)
        job = make_job()

        await instance._post_review(outcome(job))
        self.assertNotEqual(job.status, "awaiting_review", "the review never landed")

        await instance._post_job_result(job, False, "review: approve")
        self.assertEqual(len(channel.sent), 1, "the owner still gets a result")
        text = channel.sent[0]["embed"].description  # type: ignore[attr-defined]
        self.assertIn("Review could not be posted", text)
        self.assertIn(BRANCH, text)

    async def test_a_failed_blocked_send_still_reports_the_job(self) -> None:
        channel = FlakyChannel()
        instance = self.bot_with(channel)
        job = make_job()

        await instance._post_review(outcome(job, verdict="blocked"))
        self.assertNotEqual(job.status, "blocked", "the blocked review never landed")

        await instance._post_job_result(job, False, "Blocked by rules")
        self.assertEqual(len(channel.sent), 1)
        text = channel.sent[0]["embed"].description  # type: ignore[attr-defined]
        self.assertIn("Review could not be posted", text)
        self.assertIn(BRANCH, text)


class _Freebuff:
    async def start_task(
        self, *, job_id: str, task_text: str, log_path: Path, kind: str = "task"
    ) -> TaskEnvironment:
        return TaskEnvironment(
            branch=BRANCH,
            worktree=Path("/nonexistent/worktree"),
            session="fake",
            base_branch="freebuff/base",
        )

    async def wait_for_changes(
        self, env: TaskEnvironment, log_path: Path, **_kwargs: object
    ) -> str:
        return "idle"

    async def collect(self, env: TaskEnvironment, max_diff_lines: int) -> ChangeSet:
        return ChangeSet(
            diff="+let x = 1\n",
            diff_lines=1,
            changed_paths=["ios-app/RootView.swift"],
        )

    async def abort(self, env: TaskEnvironment | None = None) -> None:
        return None


class _ApprovingManager:
    async def review(self, brief: object) -> object:
        return SimpleNamespace(verdict="approve", notes="Fine.")


class RunKeepsTheNoteTests(unittest.TestCase):
    """The test that fails if JobRunner.run overwrites job.summary."""

    def test_a_note_set_by_post_review_reaches_post_job(self) -> None:
        posted: list[tuple[bool, str]] = []

        async def post_review(result: jobs.ReviewOutcome) -> None:
            result.job.summary = NOTE

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            posted.append((ok, summary))

        runner = jobs.JobRunner(
            SimpleNamespace(max_diff_lines=400, max_revise_rounds=0),
            _ApprovingManager(),
            _Freebuff(),
            post_job=post_job,
            post_review=post_review,
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(tmp)
            asyncio.run(runner.run(job))

        self.assertEqual(len(posted), 1)
        summary = posted[0][1]
        self.assertIn("Review could not be posted", summary)
        self.assertIn(BRANCH, summary)
        self.assertTrue(
            summary.rstrip().endswith(NOTE.rstrip()),
            "the note must be last, or tail_text cuts it",
        )
        self.assertNotEqual(job.status, "awaiting_review")


if __name__ == "__main__":
    unittest.main()
