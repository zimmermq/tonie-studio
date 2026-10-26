"""Tests for finding a podcast by name: in the ARD Audiothek and in the podcast directory."""

import json

from tonie_studio.podcast import FeedError
from tonie_studio.podcast_search import AUDIOTHEK, ANSWER_WITHIN, DIRECTORY, search

AUDIOTHEK_ANSWER = {"data": {"search": {"programSets": {"numberOfElements": 2, "nodes": [
    {"id": "23667030", "title": "Unser Sandmännchen", "numberOfElements": 214,
     "sharingUrl": "https://www.ardsounds.de/sendung/unser-sandmaennchen/urn:ard:show:0e3f60ed4ba198df/",
     "publicationService": {"title": "Antenne Brandenburg"}},
    {"id": "1", "title": "Ohne Link", "numberOfElements": 3, "sharingUrl": None},
]}}}}

DIRECTORY_ANSWER = {"resultCount": 2, "results": [
    {"collectionName": "Unser Sandmännchen", "artistName": "Antenne Brandenburg (rbb)",
     "feedUrl": "https://www.antennebrandenburg.de/podcast.xml", "trackCount": 68,
     "primaryGenreName": "Kinder und Familie"},
    {"collectionName": "Ohne Feed", "artistName": "Jemand", "trackCount": 1},
]}


class FakeWeb:
    def __init__(self, failing=()):
        self.failing = failing
        self.asked = []
        self.timeouts = []

    def __call__(self, url, limit=None, timeout=None):
        self.asked.append(url)
        self.timeouts.append(timeout)
        if any(url.startswith(prefix) for prefix in self.failing):
            raise FeedError("feed.not_found", host="example", detail="gone")
        if url.startswith(AUDIOTHEK):
            return json.dumps(AUDIOTHEK_ANSWER).encode()
        if url.startswith(DIRECTORY):
            return json.dumps(DIRECTORY_ANSWER).encode()
        raise AssertionError(f"unexpected {url}")


class TestSearch:
    def test_finds_audiothek_shows_to_archive(self):
        found, failed = search("Sandmännchen", FakeWeb())

        show = found[0]
        assert (show.title, show.by, show.episodes, show.where) == (
            "Unser Sandmännchen", "Antenne Brandenburg", 214, "audiothek")
        assert "urn:ard:show:0e3f60ed4ba198df" in show.source
        assert show.feed == ""
        assert failed == []

    def test_finds_podcasts_with_their_feed(self):
        found, _failed = search("Sandmännchen", FakeWeb())

        podcast = next(entry for entry in found if entry.where == "directory")
        assert podcast.feed == podcast.source == "https://www.antennebrandenburg.de/podcast.xml"
        assert (podcast.by, podcast.episodes) == ("Antenne Brandenburg (rbb)", 68)

    def test_leaves_out_what_could_not_be_archived(self):
        found, _failed = search("Sandmännchen", FakeWeb())

        assert [entry.title for entry in found] == ["Unser Sandmännchen", "Unser Sandmännchen"]

    def test_the_query_is_sent_encoded(self):
        web = FakeWeb()

        search("Jan & Henry", web)

        assert all("Jan%20%26%20Henry" in url for url in web.asked)

    def test_one_source_that_fails_leaves_the_other(self):
        found, failed = search("Sandmännchen", FakeWeb(failing=(AUDIOTHEK,)))

        assert [entry.where for entry in found] == ["directory"]
        assert failed == ["audiothek"]

    def test_a_place_is_given_only_a_short_time_to_answer(self):
        # The page waits for the search, and the proxy in front of the app
        # gives up after some fifteen seconds.
        web = FakeWeb()

        search("Sandmännchen", web)

        assert web.timeouts == [ANSWER_WITHIN, ANSWER_WITHIN]

    def test_an_empty_query_asks_nobody(self):
        web = FakeWeb()

        assert search("  ", web) == ([], [])
        assert web.asked == []
