"""An interrupted job is named once, from the marker the queue writes."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs  # noqa: E402
import recovery  # noqa: E402

JOB_ID = "20261104-140735-435f989c"


def make_job(job_id: str = JOB_ID, **extra: object) -> jobs.Job:
    payload = dict(
        id=job_id, kind="task", title="Fix the parser", channel_id=7,
        requester_id=42, log_path=Path("/tmp") / f"{job_id}.log",
    )
    payload.update(extra)
    return jobs.Job(**payload)


class MarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)

    def test_mark_records_the_job_that_is_running(self) -> None:
        job = make_job()
        job.branch = "dan/20261104-140735-435f989c"
        recovery.mark(self.log_dir, job, now=1000.0)

        state = recovery.read(self.log_dir)
        assert state is not None
        self.assertEqual(state["id"], JOB_ID)
        self.assertEqual(state["kind"], "task")
        self.assertEqual(state["title"], "Fix the parser")
        self.assertEqual(state["branch"], "dan/20261104-140735-435f989c")
        self.assertEqual(state["channel_id"], 7)
        self.assertEqual(state["started"], 1000.0)

    def test_take_reports_once_and_removes_the_marker(self) -> None:
        recovery.mark(self.log_dir, make_job())
        self.assertIsNotNone(recovery.take(self.log_dir))
        self.assertIsNone(recovery.take(self.log_dir), "a job is never announced twice")
        self.assertFalse(recovery.path_for(self.log_dir).exists())

    def test_clear_and_a_missing_marker_are_both_quiet(self) -> None:
        recovery.clear(self.log_dir)
        self.assertIsNone(recovery.read(self.log_dir))

    def test_a_broken_marker_is_no_marker(self) -> None:
        recovery.path_for(self.log_dir).write_text("{not json", encoding="utf-8")
        self.assertIsNone(recovery.read(self.log_dir))
        recovery.path_for(self.log_dir).write_text(json.dumps({"id": ""}), encoding="utf-8")
        self.assertIsNone(recovery.read(self.log_dir))

    def test_a_job_without_an_id_writes_nothing(self) -> None:
        recovery.mark(self.log_dir, make_job(job_id=""))
        self.assertFalse(recovery.path_for(self.log_dir).exists())

    def test_the_note_names_the_job_what_it_left_and_where_to_look(self) -> None:
        state = {
            "id": JOB_ID, "kind": "task", "title": "Fix the parser",
            "branch": "dan/x", "started": 1793833091,
        }
        text = recovery.note(state, bot_name="Melisa", worktree_root="/tmp/wt")

        self.assertIn(JOB_ID, text)
        self.assertIn("task", text)
        self.assertIn("not queued again", text)
        self.assertIn("Fix the parser", text)
        self.assertIn("dan/x", text)
        self.assertIn(f"/tmp/wt/{JOB_ID}", text)
        self.assertIn(f"logs/{JOB_ID}.log", text)


class QueueMarkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)

    async def test_a_running_job_is_on_disk_while_it_runs_and_gone_after(self) -> None:
        seen: list[dict | None] = []
        ran = asyncio.Event()

        async def handler(job: jobs.Job) -> None:
            seen.append(recovery.read(self.log_dir))
            ran.set()

        queue = jobs.JobQueue(handler, log_dir=self.log_dir)
        queue.start()
        await queue.submit(make_job())
        await asyncio.wait_for(ran.wait(), timeout=5)
        for _ in range(200):
            if recovery.read(self.log_dir) is None and queue.running is None:
                break
            await asyncio.sleep(0.01)
        await queue.stop()

        assert seen and seen[0] is not None, "the marker names the running job"
        self.assertEqual(seen[0]["id"], JOB_ID)
        self.assertIsNone(recovery.read(self.log_dir), "a finished job leaves no marker")

    async def test_a_queue_without_a_log_dir_writes_nothing(self) -> None:
        async def handler(job: jobs.Job) -> None:
            return None

        queue = jobs.JobQueue(handler)
        queue.start()
        await queue.submit(make_job())
        await asyncio.sleep(0.05)
        await queue.stop()
        self.assertEqual(list(self.log_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
