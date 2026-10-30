"""A single-worker asyncio job queue.

Exactly one job runs at a time. Everything else waits in submission order. The
queue owns job state only; it knows nothing about Discord, Xcode or Freebuff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class Job:
    """One queued unit of work."""

    id: str
    kind: str
    title: str
    channel_id: int
    requester_id: int
    log_path: Path
    text: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    # queued | running | awaiting_review | approved | rejected | blocked |
    # done | failed | cancelled
    status: str = "queued"
    summary: str = ""

    @property
    def duration(self) -> float | None:
        if self.started is None:
            return None
        end = self.finished if self.finished is not None else time.time()
        return max(0.0, end - self.started)


class QueueFull(RuntimeError):
    """Raised instead of growing an unbounded queue on a small machine."""


JobHandler = Callable[[Job], Awaitable[None]]
CrashHandler = Callable[[Job, BaseException], Awaitable[None]]


class JobQueue:
    """Runs one async handler at a time, in FIFO order."""

    def __init__(
        self,
        handler: JobHandler,
        *,
        max_pending: int = 10,
        on_crash: CrashHandler | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._handler = handler
        self._on_crash = on_crash
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=max_pending)
        self._worker: asyncio.Task[None] | None = None
        self._current: Job | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._max_pending = max_pending

    @property
    def running(self) -> Job | None:
        return self._current

    @property
    def pending_count(self) -> int:
        # asyncio.Queue counts items not yet taken by the worker, so a running
        # job is not included.
        return self._queue.qsize()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="job-queue")

    async def submit(self, job: Job) -> int:
        """Enqueue a job and return its 1-based position in line."""
        if self._queue.full():
            raise QueueFull(
                f"queue is full ({self._max_pending} waiting); wait for a job to finish"
            )
        self._queue.put_nowait(job)
        return self._queue.qsize()

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("job queue worker raised during shutdown")

    def snapshot(self) -> dict[str, object]:
        running = self._current
        return {
            "running": {
                "id": running.id,
                "kind": running.kind,
                "title": running.title,
                "started": running.started,
                "elapsed": running.duration,
            }
            if running
            else None,
            "pending": self.pending_count,
        }

    async def cancel(self, job_id: str) -> tuple[str, Job | None]:
        """Cancel the running job or a waiting one by id.

        Returns ('running' | 'queued' | 'unknown', job). Cancelling the running
        job waits for its handler to unwind, so the caller can clean up after it
        without racing it.
        """
        running = self._current
        if running is not None and running.id == job_id:
            task = self._current_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=30)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:  # pragma: no cover - defensive
                    log.exception("job %s failed while being cancelled", job_id)
            return "running", running

        # asyncio.Queue exposes no removal, so lift the waiting jobs out, skip
        # the one asked for and put the rest back in the same order.
        waiting: list[Job] = []
        found: Job | None = None
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._queue.task_done()
            if found is None and job.id == job_id:
                found = job
                continue
            waiting.append(job)
        for job in waiting:
            self._queue.put_nowait(job)
        if found is None:
            return "unknown", None
        found.status = "cancelled"
        found.finished = time.time()
        log.info("queued job %s was cancelled", job_id)
        return "queued", found

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            self._current = job
            job.status = "running"
            job.started = time.time()
            log.info("job %s (%s) started", job.id, job.kind)
            worker = asyncio.current_task()
            # The handler runs as its own task so /cancel can stop one job
            # without touching the worker that owns the queue.
            task = asyncio.create_task(self._handler(job), name=f"job-{job.id}")
            self._current_task = task
            try:
                await task
                # The handler is allowed to mark a job failed (a script exited
                # non-zero); only a still-running job becomes done here.
                if job.status == "running":
                    job.status = "done"
            except asyncio.CancelledError:
                # The handler may already own the status (a review waiting on
                # the owner); only a still-running job becomes cancelled here.
                if job.status == "running":
                    job.status = "cancelled"
                job.finished = time.time()
                if worker is not None and worker.cancelling():
                    # The bot is shutting down: stop the job and the loop.
                    task.cancel()
                    raise
                # /cancel stopped this one job; the queue keeps its place.
                log.info("job %s was cancelled", job.id)
            except Exception as exc:
                job.status = "failed"
                log.exception("job %s crashed", job.id)
                if self._on_crash is not None:
                    try:
                        await self._on_crash(job, exc)
                    except Exception:  # pragma: no cover - defensive
                        log.exception("crash handler failed for job %s", job.id)
            else:
                job.finished = time.time()
            finally:
                self._current = None
                self._current_task = None
                self._queue.task_done()
                log.info(
                    "job %s finished with status %s in %.1fs",
                    job.id,
                    job.status,
                    job.duration or 0.0,
                )
