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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from .i18n import Problem

if TYPE_CHECKING:
    from .batch import Placing

log = logging.getLogger(__name__)

# One encode at a time. They compete for the same ffmpeg and the same disk, and
# running two only makes both slower while confusing the progress display.
MAX_PARALLEL = 1

# How many finished jobs to keep before dropping the oldest.
KEEP_FINISHED = 50

# How much of the progress bar the uploads get. Encoding takes far longer than
# pushing the files across, but a bar that barely moves for a minute looks
# stuck, so the uploads get more than their share of the time.
UPLOAD_SHARE = 25


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
    problem: Optional[Problem] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    # Set once the job stops, so a test or a caller can wait instead of poll.
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def percent(self) -> Optional[int]:
        """Completion as a whole number, or None while nothing is measurable.

        An encode has two phases that each count from zero to their own total,
        so a bar fed either one directly fills up during the uploads and then
        sits at 100 while ffmpeg is still working. The phases get a share of
        the bar each instead, and it only ever moves forward.
        """
        if self.state is JobState.DONE:
            return 100
        if not self.total:
            return None

        share = self.done_count / self.total
        if self.step == "upload":
            return min(UPLOAD_SHARE, int(share * UPLOAD_SHARE))
        if self.step == "encode":
            return min(99, UPLOAD_SHARE + int(share * (100 - UPLOAD_SHARE)))
        return min(100, int(share * 100))


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class JobRunner:
    """Starts jobs, tracks them, and hands them out by id."""

    def __init__(self, max_parallel: int = MAX_PARALLEL):
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="job")
        # Jobs a group still needs; see Groups.
        self._kept: set = set()

    def submit(self, kind: str, label: str, work: Callable[[Callable], Any],
               job_id: Optional[str] = None) -> str:
        """Queue ``work``, which is called with a progress callback, and return the job id.

        ``job_id`` is for work that has to know its own id before it starts;
        it must be unique, which ``new_id`` provides.
        """
        job = Job(id=job_id or new_id(), kind=kind, label=label)
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
            # Kept whole when a person is meant to read it, so a page can say
            # it in the reader's language; ``error`` is the log's version.
            job.problem = error if isinstance(error, Problem) else None
            job.state = JobState.FAILED
        finally:
            job.finished_at = datetime.now(timezone.utc)
            job.done.set()

    @staticmethod
    def _progress(job: Job, step: str, done: int, total: int) -> None:
        job.step = step
        job.done_count = done
        job.total = total

    def keep(self, job_ids: Sequence[str]) -> None:
        """Hold on to these jobs past the cap until ``release``; see Groups."""
        with self._lock:
            self._kept.update(job_ids)

    def release(self, job_ids: Sequence[str]) -> None:
        with self._lock:
            self._kept.difference_update(job_ids)
            self._forget_old()

    def _forget_old(self) -> None:
        """Drop finished jobs beyond the cap, oldest first. Called under the lock."""
        finished = [
            job_id for job_id in self._order
            if self._jobs[job_id].state in (JobState.DONE, JobState.FAILED)
            and job_id not in self._kept
        ]
        for job_id in finished[:max(0, len(finished) - KEEP_FINISHED)]:
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)


# How many groups are kept, newest first; older ones let their jobs go.
KEEP_GROUPS = 20


@dataclass
class Group:
    """Encodes that belong together: the parts of one album, or the albums of a batch."""

    jobs: List[str]
    batch: bool = False
    # How a batch hands out its stickers; None for the parts of one album.
    placing: Optional["Placing"] = None


class Groups:
    """Every group still shown, by id, holding its jobs for as long as it is kept.

    A batch can wait for its stickers long after its encodes finished, and
    the runner forgets finished jobs beyond its cap; a group therefore pins
    its jobs until it is itself dropped, which happens to the oldest once
    there are more than ``keep``.
    """

    def __init__(self, runner: JobRunner, keep: int = KEEP_GROUPS):
        self.runner = runner
        self.keep = keep
        self._groups: Dict[str, Group] = {}
        self._lock = threading.Lock()

    def add(self, job_ids: Sequence[str], batch: bool = False,
            placing: Optional["Placing"] = None) -> str:
        group_id = new_id()
        self.runner.keep(job_ids)
        with self._lock:
            self._groups[group_id] = Group(jobs=list(job_ids), batch=batch, placing=placing)
            dropped = list(self._groups)[:max(0, len(self._groups) - self.keep)]
            released = [self._groups.pop(old) for old in dropped]
        for group in released:
            self.runner.release(group.jobs)
        return group_id

    def get(self, group_id: str) -> Optional[Group]:
        with self._lock:
            return self._groups.get(group_id)
