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
