from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs  # noqa: E402
from freebuff_runner import ChangeSet, TaskEnvironment  # noqa: E402


class FakeFreebuff:
    def __init__(self, changes: ChangeSet, reason: str = "idle") -> None:
        self.changes = changes
        self.reason = reason
        self.aborted = False

    async def start_task(self, *, job_id: str, task_text: str, log_path: Path) -> TaskEnvironment:
        return TaskEnvironment(
            branch="freebuff/deadbeef",
            worktree=Path("/nonexistent/worktree"),
            session="fake",
            base_branch="freebuff/base",
        )

    async def wait_for_changes(self, env: TaskEnvironment, log_path: Path) -> str:
        return self.reason

    async def collect(self, env: TaskEnvironment, max_diff_lines: int) -> ChangeSet:
        return self.changes

    async def abort(self, env: TaskEnvironment | None = None) -> None:
        self.aborted = True


class FakeManager:
    def __init__(self) -> None:
        self.called = False

    async def review(self, brief: object) -> object:
        self.called = True
        raise AssertionError("the manager must not be called for a blocked diff")


class BlockedDiffTests(unittest.TestCase):
    def run_blocked(
        self, changes: ChangeSet, reason: str = "idle"
    ) -> tuple[bool, str, list[jobs.ReviewOutcome], FakeManager]:
        manager = FakeManager()
        freebuff = FakeFreebuff(changes, reason)
        posted: list[jobs.ReviewOutcome] = []

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            posted.append(outcome)

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            return None

        settings = SimpleNamespace(max_diff_lines=400, max_revise_rounds=2)
        runner = jobs.JobRunner(
            settings, manager, freebuff, post_job=post_job, post_review=post_review
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = jobs.Job(
                id="20260101-000000-deadbeef",
                kind="task",
                title="task",
                channel_id=1,
                requester_id=2,
                log_path=Path(tmp) / "job.log",
                text="do the thing",
            )
            ok, summary = asyncio.run(runner._run_task(job))
        return ok, summary, posted, manager

    def test_oversized_diff_never_reaches_the_manager(self) -> None:
        ok, summary, posted, manager = self.run_blocked(
            ChangeSet(
                diff="",
                diff_lines=401,
                changed_paths=["ios-app/Hustler/App/RootView.swift"],
                status=" M RootView.swift",
            )
        )
        self.assertFalse(ok)
        self.assertFalse(manager.called)
        self.assertIn("manager was not called", summary)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].verdict, "blocked")
        self.assertEqual(posted[0].violations[0].code, "diff_too_large")
        self.assertIn("MAX_DIFF_LINES", posted[0].notes)

    def test_protected_path_never_reaches_the_manager(self) -> None:
        ok, _, posted, manager = self.run_blocked(
            ChangeSet(
                diff="",
                diff_lines=3,
                changed_paths=["ios-app/project.yml"],
                status=" M project.yml",
            )
        )
        self.assertFalse(ok)
        self.assertFalse(manager.called)
        self.assertEqual(posted[0].verdict, "blocked")
        self.assertIn("ios-app/project.yml", posted[0].notes)


class ReviewStatusTests(unittest.TestCase):
    """The queue must not overwrite the state the owner still has to act on."""

    class ReviewingManager:
        async def review(self, brief: object) -> object:
            return SimpleNamespace(verdict="approve", notes="Fine.")

    def test_a_job_awaiting_review_keeps_that_status(self) -> None:
        posted: list[jobs.ReviewOutcome] = []

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            outcome.job.status = "awaiting_review"
            posted.append(outcome)

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            return None

        freebuff = FakeFreebuff(
            ChangeSet(
                diff="+let x = 1\n",
                diff_lines=1,
                changed_paths=["ios-app/RootView.swift"],
            )
        )
        # No revision rounds: the fake stages cannot pass, so the revise path
        # would need a followup; this test only cares about the final status.
        runner = jobs.JobRunner(
            SimpleNamespace(max_diff_lines=400, max_revise_rounds=0),
            self.ReviewingManager(),
            freebuff,
            post_job=post_job,
            post_review=post_review,
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = jobs.Job(
                id="20260101-000000-deadbeef",
                kind="task",
                title="task",
                channel_id=1,
                requester_id=2,
                log_path=Path(tmp) / "job.log",
                text="do the thing",
            )
            asyncio.run(runner.run(job))
            self.assertEqual(len(posted), 1)
        self.assertEqual(job.status, "awaiting_review")


class OutcomeWordingTests(unittest.TestCase):
    """A timeout and an empty run must not read as the same thing."""

    # Reuse the harness without re-running the blocked-diff tests.
    run_blocked = BlockedDiffTests.run_blocked

    def test_a_timeout_says_so_and_counts_the_unreviewed_lines(self) -> None:
        ok, summary, posted, manager = self.run_blocked(
            ChangeSet(
                diff="+let x = 1\n",
                diff_lines=1,
                changed_paths=["ios-app/RootView.swift"],
            ),
            reason="timeout",
        )
        self.assertFalse(ok)
        self.assertIn("timed out", summary)
        self.assertIn("1 changed lines, not reviewed", summary)
        self.assertNotIn("without changes", summary)
        self.assertFalse(manager.called)
        self.assertEqual(posted, [])

    def test_an_empty_run_is_not_reported_as_a_timeout(self) -> None:
        ok, summary, _, _ = self.run_blocked(
            ChangeSet(diff="", diff_lines=0, changed_paths=[]), reason="idle"
        )
        self.assertFalse(ok)
        self.assertIn("without changes", summary)
        self.assertIn("idle", summary)
        self.assertNotIn("timed out", summary)


if __name__ == "__main__":
    unittest.main()
