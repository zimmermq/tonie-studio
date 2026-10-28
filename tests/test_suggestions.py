"""Tests for suggesting an album's names and cover from MusicBrainz and iTunes."""

import io
import json

import pytest
from PIL import Image

from tonie_studio.podcast import FeedError
from tonie_studio.suggestions import (ITUNES, MUSICBRAINZ, Suggestion, keep_pick, picked, suggest,
                                      with_pick)

MB_ANSWER = {"releases": [
    {"id": "611106a7", "score": 100, "title": "Halloween Lieder für Kinder", "date": "2023-09-29",
     "track-count": 3, "artist-credit": [{"name": "Lichterkinder", "joinphrase": ""}],
     "cover-art-archive": {"front": True}},
    {"id": "deadbeef", "score": 60, "title": "Halloween", "date": "1999",
     "track-count": 12, "artist-credit": [{"name": "Jemand", "joinphrase": " & "},
                                          {"name": "Andere", "joinphrase": ""}],
     "cover-art-archive": {"front": False}},
]}
ITUNES_ANSWER = {"results": [
    {"collectionName": "Halloween Lieder für Kinder", "artistName": "Lichterkinder",
     "releaseDate": "2023-09-29T07:00:00Z", "trackCount": 3,
     "artworkUrl100": "https://is1.mzstatic.com/image/thumb/a/cover.jpg/100x100bb.jpg"},
]}


BOOK_ANSWER = {"results": [
    {"collectionName": "Der Astronaut", "artistName": "Andy Weir", "releaseDate": "2021-05-20",
     "trackCount": 1, "artworkUrl100": "https://is1.mzstatic.com/image/thumb/b/book.jpg/100x100bb.jpg"},
]}


class FakeWeb:
    def __init__(self, failing=()):
        self.failing = failing
        self.asked = []

    def __call__(self, url, limit=None, timeout=None):
        self.asked.append(url)
        if any(url.startswith(prefix) for prefix in self.failing):
            raise FeedError("feed.not_found", host="example", detail="gone")
        if url.startswith(MUSICBRAINZ):
            return json.dumps(MB_ANSWER).encode()
        if url.startswith(ITUNES):
            return json.dumps(BOOK_ANSWER if "media=audiobook" in url else ITUNES_ANSWER).encode()
        raise AssertionError(f"unexpected {url}")


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 50, 50)).save(out, "JPEG")
    return out.getvalue()


class TestSuggest:
    def test_asks_both_places_and_puts_the_best_match_first(self):
        found, failed = suggest("Lichterkinder", "Halloween Lieder für Kinder", 3, FakeWeb())

        assert failed == []
        assert found[0].title == "Halloween Lieder für Kinder"
        assert found[0].artist == "Lichterkinder" and found[0].year == "2023"
        titles = [s.title for s in found]
        assert titles.index("Halloween Lieder für Kinder") < titles.index("Halloween")

    def test_a_musicbrainz_cover_is_offered_only_where_it_has_one(self):
        found, _failed = suggest("Lichterkinder", "Halloween", 3, FakeWeb())

        by_title = {(s.where, s.title): s for s in found}
        assert by_title[("musicbrainz", "Halloween Lieder für Kinder")].cover == \
            "https://coverartarchive.org/release/611106a7/front-500"
        assert by_title[("musicbrainz", "Halloween")].cover == ""

    def test_an_itunes_cover_is_asked_for_large(self):
        found, _failed = suggest("Lichterkinder", "Halloween", 3, FakeWeb())

        cover = next(s.cover for s in found if s.where == "itunes")
        assert cover.endswith("/1000x1000bb.jpg")

    def test_every_artist_of_a_release_is_named(self):
        found, _failed = suggest("Jemand", "Halloween", 12, FakeWeb())

        assert any(s.artist == "Jemand & Andere" for s in found)

    def test_the_same_track_count_counts_for_a_match(self):
        same, _ = suggest("", "Halloween", 12, FakeWeb())
        other, _ = suggest("", "Halloween", 3, FakeWeb())

        def score(found, title):
            return next(s.score for s in found if s.title == title and s.where == "musicbrainz")
        assert score(same, "Halloween") > score(other, "Halloween")

    def test_audiobooks_are_found_too(self):
        # iTunes keeps audiobooks apart from music albums.
        found, _failed = suggest("Andy Weir", "Der Astronaut", 1, FakeWeb())

        assert found[0].title == "Der Astronaut" and found[0].cover.endswith("/1000x1000bb.jpg")

    def test_of_two_equal_matches_the_one_with_a_cover_comes_first(self):
        web = FakeWeb()
        plain = {"releases": [dict(MB_ANSWER["releases"][0], **{"cover-art-archive": {}})]}

        def without_cover(url, limit=None, timeout=None):
            if url.startswith(MUSICBRAINZ):
                return json.dumps(plain).encode()
            return web(url, limit, timeout)

        found, _failed = suggest("Lichterkinder", "Halloween Lieder für Kinder", 3, without_cover)

        both = [s for s in found if s.title == "Halloween Lieder für Kinder"]
        assert both[0].score == both[1].score
        assert both[0].where == "itunes"

    def test_itunes_is_named_once_when_both_its_searches_fail(self):
        _found, failed = suggest("A", "B", 1, FakeWeb(failing=(ITUNES,)))

        assert failed == ["itunes"]

    def test_one_place_that_fails_leaves_the_other(self):
        found, failed = suggest("Lichterkinder", "Halloween", 3, FakeWeb(failing=(MUSICBRAINZ,)))

        assert failed == ["musicbrainz"]
        assert {s.where for s in found} == {"itunes"}

    def test_the_query_is_sent_encoded(self):
        web = FakeWeb()

        suggest("Jan & Henry", "Folge 1", 1, web)

        assert all("Jan%20%26%20Henry" in url or "Jan+%26+Henry" in url for url in web.asked)

    def test_nothing_to_ask_asks_nobody(self):
        web = FakeWeb()

        assert suggest(" ", "", 0, web) == ([], [])
        assert web.asked == []


class TestPick:
    def test_a_pick_keeps_its_names_and_cover(self, tmp_path):
        keep_pick(tmp_path, "a1", "Lichterkinder", "Halloween", "https://covers.example/a.jpg",
                  fetch=lambda url, limit=None, timeout=None: jpeg())

        pick = picked(tmp_path, "a1")
        assert (pick.series, pick.episode) == ("Lichterkinder", "Halloween")
        assert pick.cover.is_file()

    def test_a_cover_that_is_no_picture_is_refused(self, tmp_path):
        from tonie_studio.suggestions import SuggestionProblem

        with pytest.raises(SuggestionProblem):
            keep_pick(tmp_path, "a1", "S", "E", "https://covers.example/a.jpg",
                      fetch=lambda url, limit=None, timeout=None: b"<html>no</html>")

        assert picked(tmp_path, "a1") is None

    def test_a_pick_without_a_cover_keeps_the_names(self, tmp_path):
        keep_pick(tmp_path, "a1", "S", "E", "", fetch=None)

        pick = picked(tmp_path, "a1")
        assert (pick.series, pick.episode, pick.cover) == ("S", "E", None)

    def test_the_picked_cover_comes_first(self, tmp_path):
        from tonie_studio.album import Album

        keep_pick(tmp_path, "a1", "S", "E", "https://covers.example/a.jpg",
                  fetch=lambda url, limit=None, timeout=None: jpeg())
        own = tmp_path / "folder.jpg"
        album = Album(path=tmp_path, title="T", artist=None, year=None, tracks=[], cover=own,
                      cover_candidates=[own])

        changed = with_pick(album, tmp_path, "a1")

        assert changed.cover_candidates == [picked(tmp_path, "a1").cover, own]
        assert changed.cover == picked(tmp_path, "a1").cover

    def test_an_album_without_a_pick_stays_as_it_is(self, tmp_path):
        from tonie_studio.album import Album

        album = Album(path=tmp_path, title="T", artist=None, year=None, tracks=[], cover=None)

        assert with_pick(album, tmp_path, "a1") is album


def test_a_suggestion_names_where_it_comes_from():
    assert Suggestion(title="T", artist="A", year="", tracks=None, cover="", where="itunes",
                      score=0.5).where == "itunes"
