"""Tests for the pages of the listening log and the podcasts."""

import re
from unittest.mock import patch

import pytest
from web_helpers import finish_job, list_tags_of, publish_album, ruid_to_uid



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
    def podcasts(self, make_site, web):
        client = make_site(fetch=web)
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


class TestArchivePages:
    @pytest.fixture
    def web(self):
        from test_podcast_archive import FakeWeb

        return FakeWeb()

    @pytest.fixture
    def site(self, make_site, web):
        yield make_site(fetch=web)

    def archive(self, site, runner):
        from test_podcast_archive import SHOW_LINK

        response = site.post("/podcasts/archives", data={"source": SHOW_LINK},
                             follow_redirects=False)
        return finish_job(runner, response)

    def test_the_page_offers_an_archive(self, site):
        assert 'action="/podcasts/archives"' in site.get("/podcasts").text

    def test_every_episode_of_a_show_goes_into_the_library(self, site, runner, teddycloud):
        job = self.archive(site, runner)

        assert job.error is None
        assert len([path for path in teddycloud.files
                    if path.startswith("/Unser Sandmännchen/")]) == 3
        assert "3 von 3 Folgen" in site.get("/podcasts").text

    def test_starting_looks_only_at_the_first_page_of_a_show(self, site, web, runner,
                                                              monkeypatch):
        # A show of a thousand episodes takes longer to read than the proxy in
        # front of the app waits; the job reads it whole.
        from test_podcast_archive import SHOW_LINK

        monkeypatch.setattr("tonie_studio.podcast_archive.AUDIOTHEK_PAGE", 2)
        with patch.object(runner, "submit", return_value="held"):
            site.post("/podcasts/archives", data={"source": SHOW_LINK}, follow_redirects=False)

        assert len([url for url in web.asked if "/programsets/" in url]) == 1

    def test_episodes_the_source_no_longer_has_are_counted(self, site, web, runner):
        web.failing["https://media.example/101.mp3"] = 404
        web.failing["https://media.example/101-other.mp3"] = 404

        self.archive(site, runner)

        page = site.get("/podcasts").text
        assert "2 von 3 Folgen" in page
        assert "1 Folge gibt es bei der Quelle nicht mehr" in page

    def test_archived_episodes_show_their_picture_and_origin_in_the_library(self, site, runner):
        self.archive(site, runner)

        page = site.get("/library").text

        assert page.count('src="/cover/archive/') == 3
        assert "Podcast-Archiv" in page

    def test_archived_episodes_speak_their_language_in_the_library(self, site, runner):
        self.archive(site, runner)

        page = site.get("/library").text

        assert page.count('data-tags="podcast" data-language="de"') + page.count(
            'data-tags="podcast unused" data-language="de"') == 3

    def test_putting_one_on_a_sticker_offers_its_language(self, site, runner, settings):
        from tonie_studio.podcast_archive import load_archives

        self.archive(site, runner)
        stored = load_archives(settings.data_dir)[0].archived["103"]

        page = site.get(f"/library/assign?path={stored.path}").text

        assert '<option value="de" selected>' in page

    def test_the_episode_to_put_on_a_sticker_can_be_heard_first(self, site, runner, settings):
        from tonie_studio.podcast_archive import load_archives

        self.archive(site, runner)
        stored = load_archives(settings.data_dir)[0].archived["103"]

        page = site.get(f"/library/assign?path={stored.path}").text

        assert 'class="play labelled"' in page and 'data-play="/audio/' in page

    def test_the_picture_of_an_archived_episode_is_served(self, site, runner, settings):
        from tonie_studio.podcast_archive import load_archives

        self.archive(site, runner)
        name = load_archives(settings.data_dir)[0].archived["103"].picture

        response = site.get(f"/cover/archive/{name}?size=96")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")

    @pytest.mark.parametrize("name", ["../podcast-archives.json", "nonsense.jpg"])
    def test_nothing_but_an_archive_picture_is_served(self, site, name):
        assert site.get(f"/cover/archive/{name}").status_code in (400, 404)

    def test_putting_on_a_sticker_is_a_button_beside_delete(self, site, runner):
        self.archive(site, runner)

        page = site.get("/library").text

        assert 'class="button small" href="/library/assign?path=' in page

    def test_the_job_page_leads_back_to_the_podcasts(self, site, runner):
        job = self.archive(site, runner)

        page = site.get(f"/job/{job.id}").text

        assert "Zu den Podcasts" in page and "Tonie bespielen" not in page

    def test_a_source_that_cannot_be_read_is_said_on_the_form(self, site, web):
        web.feed = b"<html>kein Feed</html>"

        response = site.post("/podcasts/archives", data={"source": "https://example.org/feed.xml"})

        assert response.status_code == 400
        assert "nicht lesen" in response.text

    def test_the_same_source_twice_is_refused(self, site, runner):
        from test_podcast_archive import SHOW_LINK

        self.archive(site, runner)

        response = site.post("/podcasts/archives", data={"source": SHOW_LINK})

        assert response.status_code == 409

    def test_stopping_asks_first_and_keeps_the_episodes(self, site, runner, teddycloud, settings):
        from tonie_studio.podcast_archive import load_archives

        self.archive(site, runner)
        entry = load_archives(settings.data_dir)[0]
        page = site.get("/podcasts").text
        assert re.search(rf'action="/podcasts/archives/{entry.id}/delete"\s+data-confirm=', page)

        site.post(f"/podcasts/archives/{entry.id}/delete")

        assert load_archives(settings.data_dir) == []
        assert all(stored.path in teddycloud.files for stored in entry.archived.values())

    def test_an_archived_episode_put_on_a_sticker_gets_its_title_and_picture(
            self, site, runner, teddycloud, settings):
        from tonie_studio.podcast_archive import load_archives

        self.archive(site, runner)
        stored = load_archives(settings.data_dir)[0].archived["103"]

        page = site.get(f"/library/assign?path={stored.path}").text
        assert 'value="Märchen: Rumpelstilzchen"' in page

        site.post("/library/assign", data={"path": stored.path, "ruid": "deb4ed0e500304e0",
                                           "series": "Unser Sandmännchen",
                                           "episode": "Märchen: Rumpelstilzchen"})

        model = teddycloud.custom_models[-1]
        assert model["episodes"] == "Märchen: Rumpelstilzchen"
        assert model["pic"]
        assert teddycloud.images


class TestPodcastSearchPage:
    @pytest.fixture
    def web(self):
        from test_podcast_search import FakeWeb

        return FakeWeb()

    @pytest.fixture
    def site(self, make_site, web):
        yield make_site(fetch=web)

    def test_the_page_offers_a_search(self, site):
        assert 'name="query"' in site.get("/podcasts").text

    def test_a_show_found_in_the_audiothek_can_be_archived(self, site):
        page = site.get("/podcasts?query=Sandmännchen").text

        assert "Antenne Brandenburg" in page and "214 Folgen" in page
        assert ('name="source" value="https://www.ardsounds.de/sendung/unser-sandmaennchen/'
                'urn:ard:show:0e3f60ed4ba198df/"') in page

    def test_a_podcast_found_in_the_directory_can_also_be_subscribed(self, site):
        page = site.get("/podcasts?query=Sandmännchen").text

        assert 'href="/podcasts?feed=https%3A//www.antennebrandenburg.de/podcast.xml#subscribe"' in page

    def test_the_subscription_form_takes_the_feed_it_was_sent(self, site):
        page = site.get("/podcasts?feed=https://www.antennebrandenburg.de/podcast.xml").text

        assert 'value="https://www.antennebrandenburg.de/podcast.xml"' in page

    def test_a_place_that_does_not_answer_is_said(self, make_site):
        from test_podcast_search import FakeWeb
        from tonie_studio.podcast_search import AUDIOTHEK

        site = make_site(fetch=FakeWeb(failing=(AUDIOTHEK,)))

        page = site.get("/podcasts?query=Sandmännchen").text

        assert "ARD Audiothek hat nicht geantwortet" in page
        assert "Antenne Brandenburg (rbb)" in page

    def test_a_show_archived_already_says_so(self, site, settings):
        from tonie_studio.podcast_archive import add_archive

        add_archive(settings.data_dir, "https://www.ardsounds.de/sendung/unser-sandmaennchen/"
                                       "urn:ard:show:0e3f60ed4ba198df/", "Unser Sandmännchen")

        page = site.get("/podcasts?query=Sandmännchen").text

        assert "wird archiviert" in page


class TestPuttingTogetherPage:
    @pytest.fixture
    def web(self):
        from test_podcast_archive import FakeWeb

        return FakeWeb()

    @pytest.fixture
    def archived(self, make_site, web, runner, settings):
        from test_podcast_archive import SHOW_LINK
        from tonie_studio.podcast_archive import load_archives

        site = make_site(fetch=web)
        finish_job(runner, site.post("/podcasts/archives", data={"source": SHOW_LINK},
                                     follow_redirects=False))
        return site, load_archives(settings.data_dir)[0]

    def test_the_archive_leads_to_putting_together(self, archived):
        site, entry = archived

        assert f'href="/podcasts/archives/{entry.id}/together"' in site.get("/podcasts").text

    def test_the_page_lists_every_episode_to_choose(self, archived):
        site, entry = archived

        page = site.get(f"/podcasts/archives/{entry.id}/together").text

        assert page.count('name="episodes"') == 3
        assert "Märchen: Rumpelstilzchen" in page
        assert page.count('data-play="/audio/') == 3

    def test_the_chosen_episodes_go_onto_the_sticker(self, archived, runner, teddycloud):
        site, entry = archived

        response = site.post(f"/podcasts/archives/{entry.id}/together",
                             data={"episodes": ["101", "103"], "name": "Lieblinge",
                                   "ruid": "deb4ed0e500304e0"}, follow_redirects=False)
        job = finish_job(runner, response)

        assert job.error is None
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"].endswith("/Lieblinge.taf")
        page = site.get(f"/job/{job.id}").text
        assert "Zu den Tonies" in page

    def test_nothing_chosen_is_said_on_the_form(self, archived):
        site, entry = archived

        response = site.post(f"/podcasts/archives/{entry.id}/together",
                             data={"name": "Leer", "ruid": "deb4ed0e500304e0"})

        assert response.status_code == 400
