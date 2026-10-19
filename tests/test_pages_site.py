"""Tests for the pages of what every page shares: errors, the language, the navigation
and startup."""

import re

import pytest
from fastapi.testclient import TestClient
from web_helpers import encode_and_assign_page, eventually, first_album_id, publish_album

from tonie_studio.teddycloud import TeddyCloudError
from tonie_studio.web import create_app


class TestStartup:
    def test_clears_staging_that_an_earlier_run_abandoned(self, make_site, teddycloud):
        teddycloud.create_dir("/_tonie-studio-staging")
        teddycloud.create_dir("/_tonie-studio-staging/abandoned")
        teddycloud.files["/_tonie-studio-staging/abandoned/001.flac"] = b"x"

        started = make_site(background=True)
        eventually(lambda: teddycloud.files == {})
        assert started.get("/health").status_code == 200

    def test_finishes_a_replace_an_earlier_run_was_stopped_in(self, make_site, teddycloud):
        # Stopped during its final encode, a replace leaves the sticker on the
        # copy beside its taf; nothing else would ever point it back.
        teddycloud.create_dir("/S")
        teddycloud.files["/S/a.new-0badf00d.taf"] = b"n" * 10
        teddycloud.taf_headers["/S/a.new-0badf00d.taf"] = {"valid": True, "trackSeconds": [0, 5]}
        teddycloud.files["/S/a.taf"] = b"n" * 10
        teddycloud.taf_headers["/S/a.taf"] = {"valid": True, "trackSeconds": [0, 5]}
        ruid = "7ab4ed0e500304e0"
        teddycloud.tag_entries[ruid]["source"] = "lib://S/a.new-0badf00d.taf"

        make_site(background=True)

        eventually(lambda: teddycloud.tag_entries[ruid]["source"] == "lib://S/a.taf")
        eventually(lambda: "/S/a.new-0badf00d.taf" not in teddycloud.files)

    def test_clears_uploads_nobody_came_back_for(self, make_site, settings):
        import os
        import time

        stale = settings.upload_dir / "abandoned"
        stale.mkdir(parents=True)
        (stale / "01-track.mp3").write_bytes(b"x" * 64)
        long_ago = time.time() - 30 * 86400
        os.utime(stale, (long_ago, long_ago))

        make_site(background=True)
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
    def offline(self, make_site):
        class Offline:
            def __getattr__(self, _name):
                def unreachable(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud at http://teddycloud.test is unreachable")
                return unreachable

        client = make_site(client_factory=Offline)
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

    def test_a_refusal_is_not_called_an_outage(self, make_site):
        class Refusing:
            def __getattr__(self, _name):
                def refuse(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud answered 400 for /api/getTagIndex: no",
                                          status=400)
                return refuse

        client = make_site(client_factory=Refusing)
        response = client.get("/tonies")

        assert response.status_code == 502
        assert "answered 400" in response.text
        assert "Ohne teddyCloud gibt es keine Sticker" not in response.text

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
        assert "That job is not known any more" in response.text and "<html" in response.text

    def test_an_address_that_is_nowhere_is_a_page_too(self, client):
        # A mistyped or outdated link must not end on a line of raw JSON.
        response = client.get("/tonie/nope")

        assert response.status_code == 404
        assert "<html" in response.text and "Diese Seite gibt es nicht" in response.text
        assert response.text.count("Zu den Alben") == 1

    def test_an_api_address_that_is_nowhere_stays_json(self, client):
        response = client.get("/api/nowhere")

        assert response.status_code == 404 and "detail" in response.json()

    def test_a_method_that_is_not_offered_keeps_its_answer(self, client):
        assert client.delete("/tonies").status_code == 405

    def test_german_is_the_default(self, client):
        assert "Diesen Auftrag gibt es nicht mehr" in client.get("/job/nope").text

    def test_the_api_answers_json_in_the_readers_language(self, client):
        response = client.get("/api/job/nope", headers={"accept-language": "en-GB,en"})

        assert response.status_code == 404
        assert response.json()["detail"].startswith("That job is not known any more")

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

        assert '<summary aria-current="true">Verwaltung</summary>' in header
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

        assert shared.count("ask('/api/box')") == 1 and "new CustomEvent('box'" in shared
        for path in ["/", "/tonies", f"/job/{job.id}/assign", "/sheet", "/listening"]:
            page = client.get(path).text
            assert "'/api/box'" not in page, path
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
    def test_the_first_page_after_a_restart_already_knows_the_models(self, make_site, teddycloud):
        # The cache fills in the background; started only by the first page,
        # that page would go without the "has a tonie" markers.
        asked = []
        original = teddycloud.custom_tonies
        teddycloud.custom_tonies = lambda: asked.append(1) or original()
        make_site()
        eventually(lambda: asked)


class TestBuild:
    """Every page says which release and commit is running, small, beside the languages."""

    def test_a_release_names_its_version_and_commit(self, client, monkeypatch):
        monkeypatch.setenv("TS_VERSION", "v0.3.1")
        monkeypatch.setenv("TS_COMMIT", "41ddb24a3f0c9e1b2d3c4b5a69788796a5b4c3d2")

        footer = client.get("/").text.split('class="build', 1)[1].split("</span>", 1)[0]

        assert "v0.3.1" in footer and "41ddb24" in footer
        assert "/releases/tag/v0.3.1" in footer
        assert "/commit/41ddb24a3f0c9e1b2d3c4b5a69788796a5b4c3d2" in footer

    def test_a_local_run_says_dev_and_links_nothing(self, client, monkeypatch):
        monkeypatch.delenv("TS_VERSION", raising=False)
        monkeypatch.delenv("TS_COMMIT", raising=False)

        footer = client.get("/").text.split('class="build', 1)[1].split("</span>", 1)[0]

        assert "dev" in footer and "href" not in footer


class TestEveryPageNamesItselfOnce:
    """A page's name is set in one place: the base layout makes the tab and the heading of it."""

    PAGES = ["/", "/tonies", "/library", "/sheet", "/checkup", "/backup", "/radio",
             "/podcasts", "/listening", "/import", "/import/names"]

    def test_the_tab_carries_the_page_s_name_and_one_heading_follows(self, client):
        import re

        for path in self.PAGES:
            page = client.get(path).text
            found = re.search(r"<title>(.*?)</title>", page, re.S)
            assert found, (path, page[:300])
            title = found.group(1)
            assert title.endswith(" - tonie-studio") and title.count("tonie-studio") == 1, path
            assert page.count("<h1") == 1, path

    def test_a_page_with_its_own_top_has_its_name_in_the_tab_only(self, client, runner,
                                                                  teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "<title>Lichterkinder - Halloween - tonie-studio</title>" in page
        assert page.count("<h1") == 1  # the one beside the picture


class TestStaticFiles:
    def test_a_versioned_file_is_kept_for_good(self, client):
        # asset() names a file after its content, so its address changes with it.
        from tonie_studio.pages.common import asset

        response = client.get(asset("app.js"))

        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    def test_an_unversioned_one_is_not(self, client):
        response = client.get("/static/app.js")

        assert "immutable" not in response.headers.get("cache-control", "")


class TestChoosingTheLanguage:
    def test_remembers_it_and_goes_back(self, client):
        response = client.get("/lang/en?next=/tonies", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/tonies"
        assert "lang=en" in response.headers["set-cookie"]

    @pytest.mark.parametrize("next_page", ["//evil.example", "/\\evil.example",
                                           "https://evil.example", "/\\/evil.example"])
    def test_never_leaves_the_site(self, client, next_page):
        response = client.get("/lang/en", params={"next": next_page}, follow_redirects=False)

        assert response.headers["location"] == "/"

    def test_an_unknown_language_is_refused(self, client):
        assert client.get("/lang/xx", follow_redirects=False).status_code == 404


class TestLowSpace:
    def test_says_so_before_the_volume_is_full(self, settings, monkeypatch, caplog):
        # A full volume only shows as records quietly not being saved.
        import shutil
        from collections import namedtuple

        from tonie_studio import web

        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**10, 10**10 - 10**8, 10**8))
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        web._check_space(settings.data_dir)

        assert any("free" in record.getMessage() and record.levelname == "WARNING"
                   for record in caplog.records)



class TestScreenReaders:
    """What changes on a page by itself has to be announced; a sighted person sees it move."""

    def test_the_list_count_is_announced(self, client):
        assert '<span class="muted" id="count" role="status"' in client.get("/").text

    def test_progress_is_a_progress_bar_and_its_line_is_announced(self, client, runner):
        from web_helpers import encode_and_assign_page

        job, _page = encode_and_assign_page(client, runner)
        page = client.get(f"/job/{job.id}").text

        assert 'role="progressbar"' in page and 'aria-valuenow=' in page
        assert re.search(r'id="detail"[^>]*role="status"', page)
        assert re.search(r'id="message"[^>]*role="status"', page)

    def test_a_group_of_choices_is_named_by_its_caption(self, client):
        page = client.get("/podcasts").text

        assert "<fieldset" in page and "<legend>" in page

    def test_the_language_links_are_a_named_navigation(self, client):
        assert re.search(r'<nav class="languages" aria-label="', client.get("/").text)


class TestRefusalsSayWhereToGo:
    def test_a_job_gone_after_a_restart_points_to_the_unused_audio(self, client):
        # Jobs live in memory; an old link after a restart finds nothing, but
        # what the job made is in the library, played by nothing.
        page = client.get("/job/gone123").text

        assert 'href="/library?filter=unused"' in page
        assert "neu gestartet" in page

    def test_every_refusal_also_leads_back_to_the_albums(self, client):
        assert '<a class="button" href="/">' in client.get("/tonies/0000000000000000").text
