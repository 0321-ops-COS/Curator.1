"""Cancelling a job stops it, and the queue moves on."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs  # noqa: E402
from freebuff_runner import ChangeSet, FreebuffRunner, TaskEnvironment  # noqa: E402

JOB_A = "20260101-000000-aaaaaaaa"
JOB_B = "20260101-000001-eeeeeeee"
JOB_C = "20260101-000002-cccccccc"


def make(job_id: str) -> jobs.Job:
    return jobs.Job(
        id=job_id,
        kind="task",
        title=job_id,
        channel_id=1,
        requester_id=2,
        log_path=Path("/tmp") / f"{job_id}.log",
    )


class QueueCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_the_running_job_lets_the_queue_move_on(self) -> None:
        started = asyncio.Event()
        saw_cancel = asyncio.Event()
        finished: list[str] = []

        async def handler(job: jobs.Job) -> None:
            if job.id == JOB_A:
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    saw_cancel.set()
                    raise
            finished.append(job.id)

        queue = jobs.JobQueue(handler)
        queue.start()
        try:
            first, second = make(JOB_A), make(JOB_B)
            await queue.submit(first)
            await queue.submit(second)
            await asyncio.wait_for(started.wait(), timeout=2)

            state, cancelled = await queue.cancel(JOB_A)

            self.assertEqual(state, "running")
            self.assertIs(cancelled, first)
            self.assertTrue(saw_cancel.is_set(), "the handler must see the cancel")
            self.assertEqual(first.status, "cancelled")
            self.assertIsNotNone(first.finished)

            for _ in range(200):
                if second.status == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(second.status, "done", "the queue must keep working")
            self.assertEqual(finished, [JOB_B])
        finally:
            await queue.stop()

    async def test_cancelling_a_queued_job_skips_it(self) -> None:
        gate = asyncio.Event()
        ran: list[str] = []

        async def handler(job: jobs.Job) -> None:
            ran.append(job.id)
            if job.id == JOB_A:
                await gate.wait()

        queue = jobs.JobQueue(handler)
        queue.start()
        try:
            first, middle, last = make(JOB_A), make(JOB_B), make(JOB_C)
            for job in (first, middle, last):
                await queue.submit(job)
            for _ in range(200):
                if first.status == "running":
                    break
                await asyncio.sleep(0.01)

            state, cancelled = await queue.cancel(JOB_B)

            self.assertEqual(state, "queued")
            self.assertIs(cancelled, middle)
            self.assertEqual(middle.status, "cancelled")
            self.assertEqual(queue.pending_count, 1, "only the last job is still waiting")

            gate.set()
            for _ in range(200):
                if last.status == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(ran, [JOB_A, JOB_C])
            self.assertEqual(last.status, "done")
        finally:
            await queue.stop()

    async def test_an_unknown_id_is_not_cancelled(self) -> None:
        async def handler(job: jobs.Job) -> None:
            return None

        queue = jobs.JobQueue(handler)
        state, job = await queue.cancel(JOB_A)
        self.assertEqual(state, "unknown")
        self.assertIsNone(job)


class ElapsedTests(unittest.TestCase):
    def test_elapsed_is_compact(self) -> None:
        self.assertEqual(jobs.format_elapsed(None), "-")
        self.assertEqual(jobs.format_elapsed(0), "0s")
        self.assertEqual(jobs.format_elapsed(42), "42s")
        self.assertEqual(jobs.format_elapsed(252), "4m12s")
        self.assertEqual(jobs.format_elapsed(3720), "1h02m")


class CancelCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.worktrees = root / "worktrees"
        run(["git", "init", "-q", "-b", "freebuff/base", str(self.repo)])
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "file.txt").write_text("x\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-q",
                "-b",
                "freebuff/job1",
                str(self.worktrees / JOB_A),
                "freebuff/base",
            ]
        )
        self.runner = FreebuffRunner(
            SimpleNamespace(repo_dir=self.repo, worktree_root=self.worktrees)
        )
        self.guard = self.runner._write_push_guard(JOB_A)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(["git", "-C", str(self.repo), *args])

    def test_cancel_removes_the_worktree_and_the_push_guard(self) -> None:
        removed = asyncio.run(self.runner.cancel_job(JOB_A))

        self.assertIn("worktree", removed)
        self.assertIn("push guard", removed)
        self.assertFalse((self.worktrees / JOB_A).exists())
        self.assertFalse(self.guard.exists())
        branches = self._git("branch", "--list", "freebuff/job1").stdout
        self.assertIn("freebuff/job1", branches, "the branch stays as the record")


class _ApprovingManager:
    async def review(self, brief: object) -> object:
        return SimpleNamespace(verdict="approve", notes="Fine.")


class _ReviewingFreebuff:
    """Delivers a change so the run reaches the review step."""

    async def start_task(
        self, *, job_id: str, task_text: str, log_path: Path
    ) -> TaskEnvironment:
        return TaskEnvironment(
            branch="freebuff/job1",
            worktree=Path("/nonexistent/worktree"),
            session="fake",
            base_branch="freebuff/base",
        )

    async def wait_for_changes(self, env: TaskEnvironment, log_path: Path) -> str:
        return "idle"

    async def collect(self, env: TaskEnvironment, max_diff_lines: int) -> ChangeSet:
        return ChangeSet(
            diff="+let x = 1\n", diff_lines=1, changed_paths=["ios-app/RootView.swift"]
        )

    async def abort(self, env: TaskEnvironment | None = None) -> None:
        return None


class CancelResultTests(unittest.IsolatedAsyncioTestCase):
    """A cancel reports itself, and a review keeps the status it set."""

    def runner_with(self, freebuff: object, post_review, posted: list) -> jobs.JobRunner:
        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            posted.append((job.status, summary))

        return jobs.JobRunner(
            SimpleNamespace(max_diff_lines=400, max_revise_rounds=0),
            _ApprovingManager(),
            freebuff,
            post_job=post_job,
            post_review=post_review,
        )

    async def test_a_cancelled_job_still_posts_a_result(self) -> None:
        started = asyncio.Event()

        class Blocking:
            async def start_task(self, *, job_id, task_text, log_path):
                return TaskEnvironment(
                    branch="freebuff/job1",
                    worktree=Path("/nonexistent/worktree"),
                    session="fake",
                    base_branch="freebuff/base",
                )

            async def wait_for_changes(self, env, log_path):
                started.set()
                await asyncio.Event().wait()

            async def collect(self, env, max_diff_lines):
                raise AssertionError("a cancelled job is never collected")

            async def abort(self, env=None):
                return None

        posted: list = []

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            raise AssertionError("a cancelled job is never reviewed")

        runner = self.runner_with(Blocking(), post_review, posted)
        job = make(JOB_A)
        task = asyncio.create_task(runner.run(job))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(job.status, "cancelled")
        self.assertEqual(len(posted), 1, "a cancelled job still owes a result")
        self.assertIn("cancelled", posted[0][1])
        self.assertIn("nothing was pushed", posted[0][1])

    async def test_a_cancel_does_not_overwrite_a_waiting_review(self) -> None:
        reviewing = asyncio.Event()

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            outcome.job.status = "awaiting_review"
            reviewing.set()
            await asyncio.Event().wait()

        posted: list = []
        runner = self.runner_with(_ReviewingFreebuff(), post_review, posted)
        job = make(JOB_A)
        task = asyncio.create_task(runner.run(job))
        await asyncio.wait_for(reviewing.wait(), timeout=30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(job.status, "awaiting_review", "the review still owns it")
        self.assertEqual(posted, [], "the review embed is already the result")


class QueueKeepsTheReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_queue_does_not_overwrite_a_review_status(self) -> None:
        started = asyncio.Event()

        async def handler(job: jobs.Job) -> None:
            job.status = "awaiting_review"
            started.set()
            await asyncio.Event().wait()

        queue = jobs.JobQueue(handler)
        queue.start()
        try:
            job = make(JOB_A)
            await queue.submit(job)
            await asyncio.wait_for(started.wait(), timeout=5)

            state, cancelled = await queue.cancel(JOB_A)

            self.assertEqual(state, "running")
            self.assertIs(cancelled, job)
            self.assertEqual(job.status, "awaiting_review")
        finally:
            await queue.stop()



def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
