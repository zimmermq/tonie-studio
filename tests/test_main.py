"""Tests for how the app is started: its logging."""

import logging

import pytest

from tonie_studio.main import QuietPolls


def access(path, status=200):
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1", "GET", path, "1.1", status), None)


class TestQuietPolls:
    @pytest.mark.parametrize("path", ["/health", "/api/box", "/api/box?x=1"])
    def test_the_polls_every_page_and_probe_makes_are_left_out(self, path):
        # Every open page asks about the box every three seconds; logged, they
        # bury everything else.
        assert not QuietPolls().filter(access(path))

    def test_other_requests_are_kept(self):
        assert QuietPolls().filter(access("/tonies"))

    def test_a_failing_poll_is_kept(self):
        assert QuietPolls().filter(access("/api/box", 502))


class TestStartingWithAVolumeItCannotWrite:
    def test_starts_all_the_same_and_buffers_uploads_elsewhere(self, tmp_path, monkeypatch,
                                                                caplog):
        # A volume mounted read-only, or owned by root: the pages that only
        # read still work, as they did before uploads were buffered there.
        import tempfile

        from tonie_studio import main

        blocked = tmp_path / "ro"
        blocked.mkdir()
        blocked.chmod(0o500)
        monkeypatch.setenv("TMPDIR", str(blocked / "tmp"))
        monkeypatch.setenv("TS_TEDDYCLOUD_URL", "http://teddycloud.test")
        monkeypatch.setenv("TS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("TS_ARCHIVE_DIR", str(tmp_path / "archive"))
        monkeypatch.setattr(tempfile, "tempdir", None)
        try:
            main.app()
        finally:
            blocked.chmod(0o700)

        assert "TMPDIR" not in __import__("os").environ
        assert any("TMPDIR" in record.getMessage() for record in caplog.records)
