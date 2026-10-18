"""Tests for the background job runner."""

import threading

import pytest

from tonie_studio.jobs import JobRunner, JobState


@pytest.fixture
def runner():
    runner = JobRunner()
    yield runner
    runner.shutdown()


def _wait(runner, job_id, timeout=5.0):
    """Block until the job stops running, the way a test needs to assert on it."""
    job = runner.get(job_id)
    if not job.done.wait(timeout):
        raise AssertionError(f"job {job_id} did not finish within {timeout}s")
    return runner.get(job_id)


class TestSubmit:
    def test_runs_the_work_and_keeps_its_result(self, runner):
        job_id = runner.submit("encode", "Gemeinsam.taf", lambda progress: "the taf")

        assert _wait(runner, job_id).result == "the taf"

    def test_reports_success(self, runner):
        job_id = runner.submit("encode", "a", lambda progress: None)

        assert _wait(runner, job_id).state == JobState.DONE

    def test_starts_out_queued_or_running(self, runner):
        started = threading.Event()
        release = threading.Event()

        def work(progress):
            started.set()
            release.wait(5)

        job_id = runner.submit("encode", "a", work)
        started.wait(5)

        assert runner.get(job_id).state in (JobState.QUEUED, JobState.RUNNING)
        release.set()
        _wait(runner, job_id)

    def test_gives_every_job_its_own_id(self, runner):
        first = runner.submit("encode", "a", lambda progress: None)
        second = runner.submit("encode", "b", lambda progress: None)

        assert first != second


class TestFailure:
    def test_keeps_the_message_instead_of_losing_the_thread(self, runner):
        def work(progress):
            raise RuntimeError("ffmpeg died")

        job = _wait(runner, runner.submit("encode", "a", work))

        assert job.state == JobState.FAILED
        assert "ffmpeg died" in job.error

    def test_has_no_result_when_it_failed(self, runner):
        def work(progress):
            raise RuntimeError("nope")

        assert _wait(runner, runner.submit("encode", "a", work)).result is None


class TestProgress:
    def test_shows_what_the_work_reports(self, runner):
        def work(progress):
            progress("upload", 2, 5)

        job = _wait(runner, runner.submit("encode", "a", work))

        assert (job.step, job.done_count, job.total) == ("upload", 2, 5)

    def test_starts_without_a_step(self, runner):
        release = threading.Event()
        job_id = runner.submit("encode", "a", lambda progress: release.wait(5))

        assert runner.get(job_id).step is None
        release.set()
        _wait(runner, job_id)


class TestListing:
    def test_lists_the_newest_first(self, runner):
        first = runner.submit("encode", "a", lambda progress: None)
        second = runner.submit("encode", "b", lambda progress: None)
        _wait(runner, first)
        _wait(runner, second)

        assert [job.id for job in runner.all()][:2] == [second, first]

    def test_forgets_nothing_that_is_still_wanted(self, runner):
        job_id = runner.submit("encode", "a", lambda progress: None)
        _wait(runner, job_id)

        assert runner.get(job_id) is not None

    def test_answers_none_for_an_unknown_id(self, runner):
        assert runner.get("nope") is None


class TestProgressBar:
    def test_the_uploads_do_not_fill_the_whole_bar(self, runner):
        # Both phases count from zero to their own total. Fed straight to a
        # bar, the uploads fill it and the encode then sits at 100 while ffmpeg
        # is still running.
        from tonie_studio.jobs import UPLOAD_SHARE

        job = _wait(runner, runner.submit("encode", "a", lambda p: p("upload", 5, 5)))

        assert job.percent == 100  # finished jobs are always 100
        assert UPLOAD_SHARE < 100

    def test_a_running_upload_stays_in_its_share(self, runner):
        import threading

        from tonie_studio.jobs import UPLOAD_SHARE

        release = threading.Event()

        def work(progress):
            progress("upload", 5, 5)
            release.wait(5)

        job_id = runner.submit("encode", "a", work)
        _eventually(lambda: runner.get(job_id).step == "upload")

        assert runner.get(job_id).percent == UPLOAD_SHARE
        release.set()
        _wait(runner, job_id)

    def test_a_running_encode_never_reaches_a_hundred(self, runner):
        import threading

        release = threading.Event()

        def work(progress):
            progress("encode", 600, 600)
            release.wait(5)

        job_id = runner.submit("encode", "a", work)
        _eventually(lambda: runner.get(job_id).step == "encode")

        assert runner.get(job_id).percent == 99
        release.set()
        _wait(runner, job_id)


def _eventually(condition, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


class TestGroups:
    class Runner:
        """Records what the groups pin and let go of."""

        def __init__(self):
            self.kept, self.released = [], []

        def keep(self, job_ids):
            self.kept.extend(job_ids)

        def release(self, job_ids):
            self.released.extend(job_ids)

    def test_the_oldest_group_goes_once_there_are_too_many(self):
        from tonie_studio.jobs import Groups

        runner = self.Runner()
        groups = Groups(runner, keep=2)
        first = groups.add(["a1", "a2"])
        second = groups.add(["b1"])
        third = groups.add(["c1"])

        assert groups.get(first) is None
        assert groups.get(second).jobs == ["b1"] and groups.get(third).jobs == ["c1"]
        assert runner.released == ["a1", "a2"]
        assert runner.kept == ["a1", "a2", "b1", "c1"]

    def test_a_group_keeps_what_it_was_given(self):
        from tonie_studio.jobs import Groups

        groups = Groups(self.Runner())
        group = groups.add(["j1"], batch=True, placing="the placing")

        found = groups.get(group)
        assert (found.batch, found.placing) == (True, "the placing")


class TestShutdown:
    def test_says_how_many_waiting_jobs_are_dropped(self):
        # Jobs live in memory; a rollout drops the queued ones, and the log is
        # the only place that can say so.
        runner = JobRunner()
        release = threading.Event()
        runner.submit("encode", "running", lambda progress: release.wait(5))
        queued = [runner.submit("encode", f"waiting {n}", lambda progress: None) for n in range(2)]

        dropped = runner.shutdown()
        release.set()

        assert dropped == 2
        assert all(runner.get(job_id).state is JobState.FAILED for job_id in queued)
