"""Tests for compressing what the app answers: pages of several hundred kilobytes."""

import gzip

import brotli
import pytest


@pytest.fixture
def page(client, runner, teddycloud):
    from web_helpers import publish_album

    publish_album(client, runner, teddycloud)
    return "/tonies"


class TestCompression:
    def test_brotli_when_the_browser_takes_it(self, client, page):
        plain = client.get(page, headers={"Accept-Encoding": "identity"})
        response = client.get(page, headers={"Accept-Encoding": "gzip, deflate, br"})

        assert response.headers["content-encoding"] == "br"
        assert "accept-encoding" in response.headers["vary"].lower()
        # The test client decodes it; what came over the wire was smaller.
        assert response.text == plain.text
        assert int(response.headers["content-length"]) < len(plain.content) / 3

    def test_gzip_otherwise(self, client, page):
        response = client.get(page, headers={"Accept-Encoding": "gzip"})

        assert response.headers["content-encoding"] == "gzip"

    def test_nothing_for_a_browser_that_takes_neither(self, client, page):
        response = client.get(page, headers={"Accept-Encoding": "identity"})

        assert "content-encoding" not in response.headers

    def test_pictures_are_left_as_they_are(self, client, teddycloud):
        # JPEG and PNG are compressed already; doing it again costs time for nothing.
        from web_helpers import jpeg_bytes

        teddycloud.images["TS-0001.jpg"] = jpeg_bytes((600, 600))

        response = client.get("/cover/custom/TS-0001.jpg", headers={"Accept-Encoding": "br"})

        assert "content-encoding" not in response.headers

    def test_a_small_answer_is_left_as_it_is(self, client):
        response = client.get("/health", headers={"Accept-Encoding": "br"})

        assert "content-encoding" not in response.headers


class TestTheCodecsThemselves:
    def test_what_is_sent_decodes_to_the_page(self):
        from tonie_studio.compress import encode

        body = b"<p>Hallo</p>" * 500

        assert brotli.decompress(encode(body, "br")) == body
        assert gzip.decompress(encode(body, "gzip")) == body

    @pytest.mark.parametrize("header, coding", [
        ("gzip, deflate, br", "br"),
        ("br;q=0, gzip", "gzip"),
        ("gzip;q=0.5", "gzip"),
        ("*", "br"),
        ("identity", None),
        ("br;q=0, gzip;q=0", None),
        ("", None),
    ])
    def test_reads_what_the_browser_takes(self, header, coding):
        from tonie_studio.compress import chosen_coding

        assert chosen_coding(header) == coding
