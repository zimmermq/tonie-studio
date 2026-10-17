"""Tests for where a tonie's picture comes from: custom_img, teddyCloud's image cache or elsewhere."""

import hashlib

from tonie_studio.models import cache_name, cover_url

CDN = "https://images.example.com/tonies/10000001-cover.png"


def digest(url):
    return hashlib.sha256(url.encode()).hexdigest().upper()


class TestCacheName:
    """teddyCloud names a cached picture after the SHA-256 of its address."""

    def test_a_picture_hosted_elsewhere_is_named_like_teddycloud_names_it(self):
        assert cache_name(CDN) == f"{digest(CDN)}.png"

    def test_query_parameters_are_left_out_of_the_extension(self):
        url = "https://images.example.com/a/b.webp?w=400"

        assert cache_name(url) == f"{digest(url)}.webp"

    def test_without_an_extension_teddycloud_assumes_jpg(self):
        url = "https://images.example.com/a/b"

        assert cache_name(url) == f"{digest(url)}.jpg"

    def test_a_cache_address_is_its_own_name(self):
        name = f"{digest(CDN)}.png"

        assert cache_name(f"/cache/{name}") == name

    def test_anything_else_has_none(self):
        assert cache_name("/custom_img/TS-0001.jpg") is None
        assert cache_name("/cache/../../etc/passwd") is None
        assert cache_name("/img_unknown.png") is None
        assert cache_name("") is None


class TestCoverUrl:
    """Where the browser asks for a picture: always this app, unless there is no other way."""

    def test_a_cover_of_this_app_comes_through_the_custom_route(self):
        assert cover_url("/custom_img/TS-0001.jpg") == "/cover/custom/TS-0001.jpg"

    def test_a_cached_picture_comes_through_the_cache_route(self):
        name = f"{digest(CDN)}.png"

        assert cover_url(f"/cache/{name}") == f"/cover/cache/{name}"

    def test_a_picture_hosted_elsewhere_comes_through_the_cache_when_teddycloud_keeps_it(self):
        assert cover_url(CDN, cached=True) == f"/cover/cache/{digest(CDN)}.png"

    def test_otherwise_the_browser_fetches_it_itself(self):
        assert cover_url(CDN, cached=False) == CDN

    def test_teddycloud_s_placeholder_is_no_cover(self):
        assert cover_url("/img_unknown.png") is None
        assert cover_url("") is None
