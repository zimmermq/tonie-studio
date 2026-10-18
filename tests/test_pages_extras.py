"""Tests for the pages of the listening log and the podcasts."""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from web_helpers import finish_job, list_tags_of, publish_album, ruid_to_uid

from tonie_studio.web import create_app


def row(page, ruid):
    start = page.rindex("<div", 0, page.index(f'id="heard-{ruid}"'))
    end = page.find('<div class="listing-row', start + 10)
    return page[start:end if end != -1 else len(page)]


class TestListeningPage:
    """The box's events end up in the log and on the page, with names."""

    def test_a_tonie_put_on_the_box_appears_as_heard(self, client, runner, teddycloud, box):
        publish_album(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        uid = ruid_to_uid(ruid)

        box.handle("TagValid", uid)
        box.handle("playback", "started")

        heard = row(client.get("/listening?period=7").text, ruid)
        assert 'data-tags="heard"' in heard
        assert "Lichterkinder" in heard and "einmal aufgelegt" in heard

    def test_a_recorded_tonie_never_put_on_is_listed_as_quiet(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)

        quiet = row(client.get("/listening").text, ruid)

        assert 'data-tags="quiet"' in quiet and ">nie</span>" in quiet
        # Ordered by when it was last placed, never comes before any day.
        assert 'data-last="0"' in quiet

    def test_the_list_can_be_searched_filtered_and_ordered(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/listening").text

        assert 'id="search"' in page and 'data-filter="quiet"' in page
        assert 'data-sort="played"' in page and 'data-sort="last"' in page

    def test_says_since_when_it_has_been_listening(self, client, runner, teddycloud, box):
        # Numbers without their start date read as all-time totals.
        publish_album(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        box.handle("TagValid", ruid_to_uid(ruid))

        assert "Aufgezeichnet seit" in client.get("/listening").text

    def test_an_unknown_period_falls_back_rather_than_failing(self, client):
        assert client.get("/listening?period=forever").status_code == 200

    def test_the_log_lives_on_the_data_volume(self, client, runner, teddycloud, box, settings):
        from tonie_studio.listening import FILE_NAME

        publish_album(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        box.handle("TagValid", ruid_to_uid(ruid))
        box.flush()

        assert (settings.data_dir / FILE_NAME).exists()


class TestPodcastPages:
    @pytest.fixture
    def web(self):
        from test_podcast import FakeWeb

        return FakeWeb()

    @pytest.fixture
    def podcasts(self, settings, teddycloud, runner, tags, box, web):
        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner,
                         watcher=box, fetch=web)
        with TestClient(app) as client:
            yield client

    def test_subscribing_notes_what_the_sticker_played(self, podcasts, runner, teddycloud,
                                                       settings):
        from tonie_studio.podcast import load_subscriptions

        teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
        with patch.object(runner, "submit", return_value="held"):
            podcasts.post("/podcasts", data={"feed": "https://example.org/feed.xml",
                                             "ruid": "deb4ed0e500304e0", "keep": "2"},
                          follow_redirects=False)

        assert load_subscriptions(settings.data_dir)[0].played_before == "lib://Andere/Folge.taf"

    def test_a_sticker_that_plays_something_asks_first(self, podcasts, teddycloud):
        teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"

        page = podcasts.get("/podcasts").text

        assert page.count("data-confirm=") == 1

    def test_subscribing_puts_the_episodes_on_the_sticker(self, podcasts, runner, teddycloud):
        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0", "keep": "2",
        }, follow_redirects=False)
        job = finish_job(runner, response)

        assert job.error is None
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"].startswith("lib://Podcasts/")
        assert "Kakadu" in podcasts.get("/podcasts").text

    def test_unsubscribing_asks_first(self, podcasts, runner):
        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False)
        finish_job(runner, response)

        page = podcasts.get("/podcasts").text

        assert re.search(r'<form method="post" action="/podcasts/\w+/delete"\s+data-confirm=', page)

    def test_a_wrong_address_is_said_on_the_form(self, podcasts, web):
        web.feed = b"<html>kein Feed</html>"

        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        })

        assert response.status_code == 400
        assert "nicht lesen" in response.text

    def test_a_failed_refresh_is_kept_on_the_subscription(self, podcasts, runner, teddycloud):
        teddycloud.encode_fails = "ffmpeg died"

        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False)
        finish_job(runner, response)

        assert "ffmpeg died" in podcasts.get("/podcasts").text

    def test_unsubscribing_leaves_the_sticker_playing(self, podcasts, runner, teddycloud, settings):
        from tonie_studio.podcast import load_subscriptions

        finish_job(runner, podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False))
        subscription = load_subscriptions(settings.data_dir)[0]
        playing = teddycloud.tag_entries["deb4ed0e500304e0"]["source"]

        podcasts.post(f"/podcasts/{subscription.id}/delete")

        assert load_subscriptions(settings.data_dir) == []
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == playing

    def test_a_finished_podcast_job_does_not_offer_to_assign(self, podcasts, runner):
        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False)
        job = finish_job(runner, response)

        page = podcasts.get(f"/job/{job.id}").text

        assert "Zu den Podcasts" in page and "Auf einen Tonie legen" not in page


class TestListeningRefreshes:
    def test_the_page_carries_the_parts_it_swaps(self, client):
        page = client.get("/listening").text

        assert 'id="live"' in page and 'id="lists"' in page

    def test_asking_again_shows_a_second_listen(self, client, runner, teddycloud, box):
        # The page refreshes by fetching itself; a second placement has to be
        # in that answer, including the one still held back in its bundle.
        publish_album(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        uid = ruid_to_uid(ruid)
        removed = ('{"function_group":15,"function":16067,"field6":"'
                   + uid[8:] + uid[:8] + '"}')

        box.handle("TagValid", uid)
        assert "einmal aufgelegt" in client.get("/listening?period=7").text

        box.handle("rtnl-raw-log2", removed)
        box.handle("TagValid", uid)

        assert "2-mal aufgelegt" in client.get("/listening?period=7").text
