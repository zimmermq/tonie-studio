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
