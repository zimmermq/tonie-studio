"""Tests for moments kept as seconds since 1970, and what is read from them."""

from datetime import timezone

import pytest

from tonie_studio.models import picture_suffix, release_year
from tonie_studio.times import from_seconds


class TestFromSeconds:
    def test_a_moment_comes_back_in_utc(self):
        moment = from_seconds(1_600_000_000)

        assert moment.tzinfo == timezone.utc and moment.year == 2020

    @pytest.mark.parametrize("value", [None, 0, "", "never", "1e400"])
    def test_nothing_or_nonsense_is_no_moment(self, value):
        assert from_seconds(value) is None

    def test_text_as_the_catalogue_writes_it_is_read(self):
        assert from_seconds("1546300800").year == 2019


class TestReleaseYear:
    def test_the_year_of_a_release(self):
        assert release_year(" 1546300800 ") == 2019

    def test_no_release_is_no_year(self):
        assert release_year("") is None and release_year(None) is None


class TestPictureSuffix:
    @pytest.mark.parametrize("url, suffix", [
        ("https://images.example.com/a.PNG?w=400", "png"),
        ("https://images.example.com/a.webp", "webp"),
        ("https://images.example.com/a", "jpg"),
        ("https://images.example.com/a.gif", "jpg"),
        ("", "jpg"),
    ])
    def test_the_cover_type_of_an_address(self, url, suffix):
        assert picture_suffix(url) == suffix
