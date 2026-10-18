"""The package on the import path, and the fixtures the page tests share.

CI sets PYTHONPATH=src; putting src on the path here keeps a bare local run
working too. The imports below it need that path, so they come after it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fake_teddycloud import FakeTeddyCloud  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from web_helpers import write_jpeg  # noqa: E402

from tonie_studio.album import TrackTags  # noqa: E402
from tonie_studio.boxstate import BoxWatcher  # noqa: E402
from tonie_studio.jobs import JobRunner  # noqa: E402
from tonie_studio.settings import Settings  # noqa: E402
from tonie_studio.web import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_shop():
    """tonies.com is never asked from a test: a page would wait on it, or pass because of it.

    A test that wants an answer sets ``side_effect`` on what this yields.
    """
    def unreachable(url, params):
        raise OSError("tests do not reach tonies.com")

    with patch("tonie_studio.shop._get", side_effect=unreachable) as shop:
        yield shop


@pytest.fixture
def tags():
    """No tags anywhere; names come from the file names."""
    with patch("tonie_studio.album._read_tags", return_value=TrackTags()):
        yield


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / "archive" / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
    folder.mkdir(parents=True)
    for index, name in enumerate(["01-kleine_monster.flac", "02-suesses.flac", "03-geister.flac"], 1):
        (folder / name).write_bytes(bytes([index]) * 8)
    # A real image: the cover is cropped and rendered on its way to a sticker,
    # so a three byte stand-in would only prove that nothing reads it.
    write_jpeg(folder / "cover.jpg")
    return tmp_path / "archive"


@pytest.fixture
def settings(tmp_path, archive):
    return Settings(
        teddycloud_url="http://teddycloud.test",
        archive_dir=archive,
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def teddycloud():
    cloud = FakeTeddyCloud()
    # Two tags the box has already read; nothing is assigned yet.
    cloud.add_tag("7ab4ed0e500304e0")
    cloud.add_tag("deb4ed0e500304e0")
    yield cloud
    # The real server drops the connection for these; see dir_misses.
    assert cloud.dir_misses == [], f"listed folders that do not exist: {cloud.dir_misses}"


@pytest.fixture
def runner():
    runner = JobRunner()
    yield runner
    runner.shutdown()


@pytest.fixture
def box():
    """A watcher that never opens a connection; tests feed it events."""
    watcher = BoxWatcher("http://teddycloud.test")
    watcher.start = lambda: None
    return watcher


@pytest.fixture
def make_site(settings, teddycloud, runner, tags, box):
    """Start the app and hand out a client of it; ``overrides`` go to create_app.

    Called rather than requested, so a test can set things up first that the
    app reads as it starts. Every app started is shut down after the test.
    The loops that work on their own stay off unless asked for with
    ``background=True``: they would race the test's changes to teddyCloud.
    """
    started = []

    def make(**overrides):
        options = {"client_factory": lambda: teddycloud, "runner": runner, "watcher": box,
                   "background": False, **overrides}
        site = TestClient(create_app(options.pop("settings", settings), **options))
        started.append(site)
        return site.__enter__()

    yield make
    for site in reversed(started):
        site.__exit__(None, None, None)


@pytest.fixture
def client(make_site):
    return make_site()
