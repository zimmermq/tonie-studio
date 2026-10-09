"""Tests for the pages of what every page shares: errors, the language, the navigation
and startup."""


import pytest
from fastapi.testclient import TestClient
from web_helpers import encode_and_assign_page, eventually, first_album_id

from tonie_studio.teddycloud import TeddyCloudError
from tonie_studio.web import create_app


class TestStartup:
    def test_clears_staging_that_an_earlier_run_abandoned(self, settings, teddycloud, runner, tags):
        teddycloud.create_dir("/_tonie-studio-staging")
        teddycloud.create_dir("/_tonie-studio-staging/abandoned")
        teddycloud.files["/_tonie-studio-staging/abandoned/001.flac"] = b"x"

        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner)
        with TestClient(app) as started:
            eventually(lambda: teddycloud.files == {})
            assert started.get("/health").status_code == 200

    def test_clears_uploads_nobody_came_back_for(self, settings, teddycloud, runner, tags, box):
        import os
        import time

        stale = settings.upload_dir / "abandoned"
        stale.mkdir(parents=True)
        (stale / "01-track.mp3").write_bytes(b"x" * 64)
        long_ago = time.time() - 30 * 86400
        os.utime(stale, (long_ago, long_ago))

        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)
        with TestClient(app):
            eventually(lambda: not stale.exists())

    def test_starts_while_teddycloud_is_unreachable(self, settings, runner, tags):
        # An unreachable teddyCloud answers only after DNS timeouts and
        # retries. Startup must not wait for that, or the readiness probe stays
        # down for an outage this app can serve pages through.
        import threading as _threading

        blocked = _threading.Event()

        class Hanging:
            def list_dir(self, path="/", special=None):
                blocked.wait(10)
                raise TeddyCloudError("teddyCloud is unreachable")

        app = create_app(settings, client_factory=Hanging, runner=runner)
        try:
            with TestClient(app) as started:
                assert started.get("/health").status_code == 200
        finally:
            blocked.set()


class TestTeddyCloudDown:
    @pytest.fixture
    def offline(self, settings, runner, tags):
        class Offline:
            def __getattr__(self, _name):
                def unreachable(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud at http://teddycloud.test is unreachable")
                return unreachable

        app = create_app(settings, client_factory=Offline, runner=runner)
        with TestClient(app) as client:
            yield client

    def test_the_album_list_still_works(self, offline):
        # Browsing the archive needs nothing from teddyCloud.
        assert offline.get("/").status_code == 200

    def test_says_which_service_is_away(self, offline):
        response = offline.get("/tonies")

        assert response.status_code == 503
        assert "teddyCloud antwortet nicht" in response.text

    def test_the_sheet_page_says_it_too(self, offline):
        assert offline.get("/sheet").status_code == 503

    def test_health_stays_green_because_this_app_is_fine(self, offline):
        # Readiness must not follow teddyCloud, or its restart rolls this pod.
        assert offline.get("/health").status_code == 200


class TestGermanSurface:
    def test_names_the_ordering_in_german(self, client):
        # "Reihenfolge nach filename number" is an internal identifier leaking
        # into a German sentence.
        page = client.get(f"/album/{first_album_id(client)}").text

        assert "Reihenfolge nach Dateinummer" in page
        assert "file_number" not in page

    def test_writes_umlauts_rather_than_transliterating_them(self, client):
        for path in ("/", "/tonies", "/library", "/radio", "/sheet"):
            page = client.get(path).text
            for wrong in ("loeschen", "auswaehlen", "zurueck", "fuer ", "Zusaetzliche"):
                assert wrong not in page, f"{wrong!r} steht noch in {path}"


class TestSmallRefusals:
    @pytest.mark.parametrize("typed", ["nan", "inf", "-inf"])
    def test_a_measurement_that_is_not_a_length_is_refused(self, client, typed):
        response = client.post("/sheet/calibration", data={"left_mm": typed, "top_mm": "50"})

        assert response.status_code == 400

    def test_more_parts_than_tracks_is_said_in_german(self, client):
        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "S", "episode": "E", "order": "1,2,3", "selected": ["1", "2", "3"],
            "titles": ["a", "b", "c"], "parts": "5",
        })

        assert response.status_code == 400
        assert "Teile" in response.text and "tracks cannot" not in response.text

    def test_no_parts_at_all_is_refused_plainly(self, client):
        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "S", "episode": "E", "order": "1", "selected": ["1"],
            "titles": ["a"], "parts": "0",
        })

        assert response.status_code == 400 and "passen nicht" not in response.text


class TestRefusals:
    def test_a_refusal_is_a_page_in_the_readers_language(self, client):
        client.cookies.set("lang", "en")

        response = client.get("/job/nope")

        assert response.status_code == 404
        assert "There is no such job." in response.text and "<html" in response.text

    def test_german_is_the_default(self, client):
        assert "Diesen Auftrag gibt es nicht." in client.get("/job/nope").text

    def test_the_api_answers_json_in_the_readers_language(self, client):
        response = client.get("/api/job/nope", headers={"accept-language": "en-GB,en"})

        assert response.status_code == 404
        assert response.json() == {"detail": "There is no such job."}

    def test_a_cause_that_is_itself_translated_is_said_in_the_same_language(self):
        from tonie_studio.i18n import Translator
        from tonie_studio.pages.common import Refused
        from tonie_studio.podcast import FeedError

        refusal = Refused(400, "error.feed_unreadable", detail=FeedError("feed.no_title"))

        assert refusal.say(Translator("de")) == (
            "Dieser Feed l\u00e4sst sich nicht lesen: Der Feed hat keinen Titel.")
        assert refusal.say(Translator("en")) == "That feed cannot be read: The feed has no title."


class TestNavigation:
    def test_making_tonies_is_in_view_and_the_rest_in_two_menus(self, client):
        page = client.get("/").text
        header = page[page.index("<header"):page.index("</header>")]

        assert header.count('<details class="menu">') == 2
        assert header.index('href="/sheet"') < header.index("<details")
        assert all(f'href="{path}"' in header for path in
                   ("/", "/tonies", "/sheet", "/radio", "/podcasts", "/listening",
                    "/library", "/checkup", "/backup"))

    def test_marks_the_page_and_its_menu(self, client):
        page = client.get("/backup").text
        header = page[page.index("<header"):page.index("</header>")]

        assert '<summary aria-current="true">Pflege</summary>' in header
        assert 'href="/backup" aria-current="page"' in header
        assert header.count("aria-current") == 2

    def test_an_album_page_counts_as_albums(self, client):
        page = client.get(f"/album/{first_album_id(client)}").text

        assert 'href="/" aria-current="page"' in page


class TestOneBoxPoller:
    def test_every_page_asks_about_the_box_in_one_place_only(self, client, runner, teddycloud):
        # The pages that follow the box listen for the "box" event the shared
        # poller sends; a second loop of their own doubles the requests.
        job, _page = encode_and_assign_page(client, runner)
        pages = ["/", "/tonies", f"/job/{job.id}/assign", "/sheet", "/listening"]

        for path in pages:
            page = client.get(path).text
            assert page.count("fetch('/api/box')") == 1, path
            assert "new CustomEvent('box'" in page, path
