"""Tests for the pages of what every page shares: errors, the language, the navigation
and startup."""


import pytest
from fastapi.testclient import TestClient
from web_helpers import encode_and_assign_page, eventually, first_album_id, publish_album

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
        # script sends; a second loop of their own doubles the requests.
        job, _page = encode_and_assign_page(client, runner)
        shared = client.get(_script_of(client.get("/").text)).text

        assert shared.count("fetch('/api/box')") == 1 and "new CustomEvent('box'" in shared
        for path in ["/", "/tonies", f"/job/{job.id}/assign", "/sheet", "/listening"]:
            page = client.get(path).text
            assert "fetch('/api/box')" not in page, path
            assert _script_of(page).startswith("/static/app.js?v="), path


def _script_of(page):
    start = page.index('<script src="') + len('<script src="')
    return page[start:page.index('"', start)]


class TestSharedFiles:
    def test_are_served_and_named_after_their_content(self, client):
        page = client.get("/").text

        for name in ("app.css", "app.js"):
            start = page.index(f"/static/{name}?v=")
            url = page[start:page.index('"', start)]
            assert client.get(url).status_code == 200
            assert len(url.rsplit("=", 1)[1]) == 12


class TestConfirmations:
    """Every action that cannot be taken back asks first, the same way everywhere."""

    def test_one_handler_asks_for_every_form_that_wants_it(self, client):
        page = client.get("/").text

        shared = client.get(_script_of(page)).text

        assert "dataset.confirm" not in page
        assert shared.count("addEventListener('submit'") == 1 and "dataset.confirm" in shared

    def test_deleting_a_model_asks_first(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(ruid for ruid, entry in teddycloud.tag_entries.items() if entry["source"])

        page = client.get(f"/tonies/{ruid}").text
        form = page[page.index(f'action="/tonies/{ruid}/delete"') - 200:page.index(f'action="/tonies/{ruid}/delete"') + 200]

        assert "data-confirm=" in form

    def test_deleting_a_taf_a_sticker_plays_says_which_one(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/library").text

        assert 'data-confirm="Dieses Taf wird gespielt von:' in page


class TestNoRouteBlocksTheLoop:
    def test_every_route_runs_in_the_thread_pool(self, client):
        # Every route here reaches teddyCloud or the disk with blocking calls.
        # Run as a coroutine, one slow call would stall every other request,
        # the health check included.
        import inspect

        ours = [route for route in client.app.routes
                if getattr(route, "endpoint", None)
                and route.endpoint.__module__.startswith("tonie_studio")]
        coroutines = [route.path for route in ours if inspect.iscoroutinefunction(route.endpoint)]

        assert len(ours) > 40

        assert coroutines == []


class TestOtherSitesCannotChangeAnything:
    """A page elsewhere can make the browser post here; the browser says where it came from."""

    def _delete(self, client, **headers):
        return client.post("/podcasts/nope/delete", headers=headers, follow_redirects=False)

    def test_a_post_from_another_site_is_refused(self, client):
        for site in ("cross-site", "same-site"):
            assert self._delete(client, **{"Sec-Fetch-Site": site}).status_code == 403

    def test_a_post_from_this_app_goes_through(self, client):
        assert self._delete(client, **{"Sec-Fetch-Site": "same-origin"}).status_code != 403

    def test_reading_from_anywhere_is_fine(self, client):
        assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


class TestTeddyCloudAwayOnTheApi:
    def test_a_script_asking_gets_json_not_a_page(self, client):
        from tonie_studio.teddycloud import TeddyCloudError

        def away():
            raise TeddyCloudError("connection refused")

        client.app.add_api_route("/api/test-away", away)

        response = client.get("/api/test-away")

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()


class TestProgressInWords:
    """The server describes a running job the way the page's script does."""

    def _say(self, step, done, total):
        from types import SimpleNamespace

        from tonie_studio.i18n import Translator
        from tonie_studio.pages.common import describe_progress

        job = SimpleNamespace(step=step, done_count=done, total=total)
        return describe_progress(job, Translator("de"))

    def test_an_encode_counts_minutes_not_seconds(self):
        assert self._say("encode", 312, 4917) == "encodiert, etwa 5 von 82 Minuten"

    def test_a_short_encode_just_says_so(self):
        assert self._say("encode", 30, 60) == "wird encodiert"

    def test_uploads_count_tracks(self):
        assert self._say("upload", 2, 9) == "Track 2 von 9 hochgeladen"

    def test_every_step_an_encode_reports_is_put_in_words(self):
        from tonie_studio.encode import STEPS

        for step in STEPS:
            assert not self._say(step, 1, 600).startswith(step)

    def test_before_the_first_step(self):
        assert self._say(None, 0, 0) == self._say("", 0, 0) != ""


class TestModelsAreLoadedAtStartup:
    def test_the_first_page_after_a_restart_already_knows_the_models(self, settings, runner,
                                                                      teddycloud, box, tags):
        # The cache fills in the background; started only by the first page,
        # that page would go without the "has a tonie" markers.
        asked = []
        original = teddycloud.custom_tonies
        teddycloud.custom_tonies = lambda: asked.append(1) or original()
        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)

        with TestClient(app):
            eventually(lambda: asked)
