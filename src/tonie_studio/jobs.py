"""Runs long work off the request thread and lets a page watch it.

An encode takes seconds for two tracks and minutes for a full album, which is
far too long for a form POST to wait on. The work therefore runs in its own
thread and the browser polls a job, which also means a reload or a closed tab
cannot lose it.

Jobs live in memory only. They describe work in flight, not history worth
keeping, and the staging cleanup at startup already assumes that a restart
abandons whatever was running.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# One encode at a time. They compete for the same ffmpeg and the same disk, and
# running two only makes both slower while confusing the progress display.
MAX_PARALLEL = 1

# How many finished jobs to keep before dropping the oldest.
KEEP_FINISHED = 50


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    """One piece of background work, as a page needs to render it."""

    id: str
    kind: str
    label: str
    state: JobState = JobState.QUEUED
    step: Optional[str] = None
    done_count: int = 0
    total: int = 0
    result: Any = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    # Set once the job stops, so a test or a caller can wait instead of poll.
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def percent(self) -> Optional[int]:
        """Completion as a whole number, or None while nothing is measurable."""
        if self.state is JobState.DONE:
            return 100
        if not self.total:
            return None
        return min(100, int(self.done_count * 100 / self.total))


class JobRunner:
    """Starts jobs, tracks them, and hands them out by id."""

    def __init__(self, max_parallel: int = MAX_PARALLEL):
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="job")

    def submit(self, kind: str, label: str, work: Callable[[Callable], Any]) -> str:
        """Queue ``work``, which is called with a progress callback, and return the job id."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old()

        self._pool.submit(self._run, job, work)
        return job.id

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> List[Job]:
        """Every job still held, newest first."""
        with self._lock:
            return [self._jobs[job_id] for job_id in reversed(self._order)]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- internals --------------------------------------------------------

    def _run(self, job: Job, work: Callable[[Callable], Any]) -> None:
        job.state = JobState.RUNNING
        try:
            job.result = work(lambda step, done, total: self._progress(job, step, done, total))
            job.state = JobState.DONE
        except Exception as error:
            # The thread is the only place this exception exists, so losing it
            # would leave the page with a job that stopped for no stated reason.
            log.exception("job %s (%s) failed", job.id, job.label)
            job.error = str(error) or error.__class__.__name__
            job.state = JobState.FAILED
        finally:
            job.finished_at = datetime.now(timezone.utc)
            job.done.set()

    @staticmethod
    def _progress(job: Job, step: str, done: int, total: int) -> None:
        job.step = step
        job.done_count = done
        job.total = total

    def _forget_old(self) -> None:
        """Drop finished jobs beyond the cap, oldest first. Called under the lock."""
        finished = [
            job_id for job_id in self._order
            if self._jobs[job_id].state in (JobState.DONE, JobState.FAILED)
        ]
        for job_id in finished[:max(0, len(finished) - KEEP_FINISHED)]:
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)
