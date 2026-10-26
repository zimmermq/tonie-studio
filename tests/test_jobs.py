"""Tests for the background job runner."""

import gc
import threading
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from web_helpers import eventually

from tonie_studio import jobs as jobs_module
from tonie_studio.jobs import JobRunner, JobState
from tonie_studio.teddycloud import TeddyCloudClient, TeddyCloudError


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


class _KeepAlive(BaseHTTPRequestHandler):
    """Answers every request with an empty listing and keeps the connection open.

    ``open`` counts the connections a client still holds; teddyCloud serves
    only a few dozen, and each one a client forgets is one fewer for everyone.
    """

    protocol_version = "HTTP/1.1"
    # Long enough for any test, short enough that a leak cannot hang the run.
    timeout = 10
    open = 0
    lock = threading.Lock()

    def handle(self):
        with self.lock:
            type(self).open += 1
        try:
            super().handle()
        finally:
            with self.lock:
                type(self).open -= 1

    def do_GET(self):
        body = b'{"files": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def keep_alive_server():
    _KeepAlive.open = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def unlogged(monkeypatch):
    """The runner's failure log switched off.

    The test's log capture keeps every record, and a failure's record holds
    the traceback; that would keep alive exactly what these tests prove the
    job itself lets go of. A real log handler writes the record and drops it.
    """
    monkeypatch.setattr(jobs_module.log, "disabled", True)


class TestAFailedJobLetsGo:
    """A failed job is kept for its page; what the work used must not be kept with it."""

    def test_keeps_no_frame_of_the_work(self, runner, unlogged):
        made = []

        def work(progress):
            client = TeddyCloudClient("http://teddycloud.test")
            made.append(weakref.ref(client))
            made.append(weakref.ref(client.session))
            raise TeddyCloudError("teddyCloud answered 500", status=500)

        job = _wait(runner, runner.submit("encode", "a", work))
        gc.collect()

        assert job.state is JobState.FAILED
        assert isinstance(job.exception, TeddyCloudError)
        assert [ref() for ref in made] == [None, None]

    def test_keeps_no_frame_of_a_cause_either(self, runner, unlogged):
        made = []

        def work(progress):
            client = TeddyCloudClient("http://teddycloud.test")
            made.append(weakref.ref(client))
            try:
                raise OSError("connection reset")
            except OSError as error:
                raise RuntimeError("upload failed") from error

        job = _wait(runner, runner.submit("encode", "a", work))
        gc.collect()

        assert isinstance(job.exception.__cause__, OSError)
        assert made[0]() is None

    def test_leaves_no_connection_open(self, runner, unlogged, keep_alive_server):
        # Each failed job holding one keep-alive connection is how a few
        # dozen failures took teddyCloud's every connection.
        def work(progress):
            client = TeddyCloudClient(keep_alive_server)
            client.list_dir("/")
            raise TeddyCloudError("teddyCloud refused /api/fileDelete", refused=True)

        failed = [_wait(runner, runner.submit("encode", f"a{n}", work)) for n in range(3)]
        gc.collect()

        assert all(job.state is JobState.FAILED for job in failed)
        eventually(lambda: _KeepAlive.open == 0)


class TestProgress:
    def test_shows_what_the_work_reports(self, runner):
        def work(progress):
            progress("upload", 2, 5)

        job = _wait(runner, runner.submit("encode", "a", work))

        assert (job.step, job.done_count, job.total) == ("upload", 2, 5)

    def test_keeps_what_the_work_is_at_when_it_says(self, runner):
        def work(progress):
            progress("archive", 0, 10, current="Jan und Henry: Der Affe")

        job = _wait(runner, runner.submit("archive", "a", work))

        assert job.current == "Jan und Henry: Der Affe"

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
        eventually(lambda: runner.get(job_id).step == "upload")

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
        eventually(lambda: runner.get(job_id).step == "encode")

        assert runner.get(job_id).percent == 99
        release.set()
        _wait(runner, job_id)



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
        started, release = threading.Event(), threading.Event()
        runner.submit("encode", "running",
                      lambda progress: started.set() or release.wait(5))
        # The worker has taken the first job before the others are queued:
        # otherwise which of them counts as "not started" is up to the scheduler.
        assert started.wait(5)
        queued = [runner.submit("encode", f"waiting {n}", lambda progress: None) for n in range(2)]

        dropped = runner.shutdown()
        release.set()

        assert dropped == 2
        assert all(runner.get(job_id).state is JobState.FAILED for job_id in queued)

    def test_a_job_submitted_while_shutting_down_is_never_left_waiting(self):
        # Its place in the pool is known before anyone can ask the runner
        # which jobs never ran, or it would stay "queued" for good.
        runner = JobRunner()
        started, release = threading.Event(), threading.Event()
        runner.submit("encode", "running", lambda progress: started.set() or release.wait(5))
        assert started.wait(5)
        late = []
        stop = threading.Thread(target=lambda: late.append(runner.shutdown()))
        job_id = runner.submit("encode", "late", lambda progress: None)
        stop.start()
        stop.join(5)
        release.set()

        assert runner.get(job_id).state in (JobState.FAILED, JobState.DONE)


    def test_a_running_job_is_given_time_to_finish(self):
        # A pod stop ends the process right after the app shut down; the job
        # a person is watching gets that time rather than being cut off.
        runner = JobRunner()
        started, release = threading.Event(), threading.Event()
        job_id = runner.submit("encode", "running",
                               lambda progress: started.set() or release.wait(5))
        assert started.wait(5)
        runner.shutdown()
        threading.Timer(0.1, release.set).start()

        cut_off = runner.finish(5)

        assert cut_off == []
        assert runner.get(job_id).state is JobState.DONE

    def test_a_job_that_runs_on_is_named(self):
        runner = JobRunner()
        started, release = threading.Event(), threading.Event()
        job_id = runner.submit("encode", "Gemeinsam.taf",
                               lambda progress: started.set() or release.wait(5))
        assert started.wait(5)
        runner.shutdown()

        cut_off = runner.finish(0.05)
        release.set()

        assert [job.id for job in cut_off] == [job_id]

    def test_nothing_running_needs_no_wait(self):
        runner = JobRunner()
        _wait(runner, runner.submit("encode", "done", lambda progress: None))
        runner.shutdown()

        assert runner.finish(5) == []


class TestTheAppStopping:
    """What a pod stop does to the job a person is watching."""

    @staticmethod
    def started_app(settings, teddycloud, box, runner, **options):
        from fastapi.testclient import TestClient

        from tonie_studio.web import create_app

        site = TestClient(create_app(settings, client_factory=lambda: teddycloud, runner=runner,
                                     watcher=box, background=False, **options))
        return site.__enter__()

    def test_waits_for_the_running_job(self, settings, teddycloud, box):
        runner = JobRunner()
        site = self.started_app(settings, teddycloud, box, runner, shutdown_grace=5)
        started, release = threading.Event(), threading.Event()
        job_id = runner.submit("encode", "running",
                               lambda progress: started.set() or release.wait(5))
        assert started.wait(5)
        threading.Timer(0.1, release.set).start()

        site.__exit__(None, None, None)

        assert runner.get(job_id).state is JobState.DONE

    def test_says_which_job_it_cut_off(self, settings, teddycloud, box, caplog):
        runner = JobRunner()
        site = self.started_app(settings, teddycloud, box, runner, shutdown_grace=0.05)
        started, release = threading.Event(), threading.Event()
        job_id = runner.submit("encode", "/Lichterkinder/Halloween.taf",
                               lambda progress: started.set() or release.wait(5))
        assert started.wait(5)

        with caplog.at_level("WARNING", logger="tonie_studio.web"):
            site.__exit__(None, None, None)
        release.set()

        assert any(job_id in record.getMessage()
                   and "/Lichterkinder/Halloween.taf" in record.getMessage()
                   for record in caplog.records)
