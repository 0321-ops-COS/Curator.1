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
from freebuff_runner import FreebuffRunner  # noqa: E402

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


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
