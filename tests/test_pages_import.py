"""Tests for the import page: starting a run from GitHub or uploaded dumps, and watching it."""

import random

import pytest

from tonie_studio.github import RepoError
from tonie_studio.i18n import Translator
from tonie_studio.importing import FETCHING, PENDING, STOPPED, Importer

AUTH = "AB" * 32
say = Translator("de")


def dump(last_bytes):
    spaced = " ".join(AUTH[i:i + 2] for i in range(0, len(AUTH), 2))
    return f"Filetype: Flipper NFC device\nUID: E0 04 03 50 {last_bytes}\nData Content: {spaced}\n"


WOLF_RUID = "11dfc517500304e0"


class FakeRepo:
    def __init__(self):
        self.asked = []

    def __call__(self, repo, token=""):
        self.asked.append((repo, token))
        return [("Sammlung/Wolf.nfc", dump("17 C5 DF 11"))]


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def importer(teddycloud, settings, repo):
    teddycloud.cloud[WOLF_RUID] = b"taf"
    teddycloud.unique_audio = True
    importer = Importer(lambda: teddycloud, settings.data_dir, rng=random.Random(1),
                        sleep=lambda s: None, fetch_repo=repo)
    # The tests take the steps themselves; no thread races them.
    importer.start = lambda: None
    return importer


@pytest.fixture
def site(make_site, settings, importer):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    client = make_site(importer=importer)
    yield client


GITHUB = {"repo": "https://github.com/owner/tonies/tree/main/Sammlung", "branch": "",
          "subfolder": "", "token": "", "per_hour": "10", "jitter": "25", "max_failures": "3"}
PACE = {key: GITHUB[key] for key in ("per_hour", "jitter", "max_failures")}


class TestStarting:
    def test_the_page_offers_both_sources_when_nothing_runs(self, site):
        page = site.get("/import").text

        assert 'action="/import/github"' in page and 'action="/import/files"' in page

    def test_the_menu_leads_there(self, site):
        assert 'href="/import"' in site.get("/").text

    def test_a_repository_is_read_by_the_worker_not_the_request(self, site, importer, repo):
        response = site.post("/import/github", data=GITHUB, follow_redirects=False)

        assert (response.status_code, response.headers["location"]) == (303, "/import")
        assert importer.current().phase == FETCHING and repo.asked == []

    def test_branch_subfolder_and_token_reach_the_worker(self, site, importer, repo):
        site.post("/import/github", data=dict(GITHUB, subfolder="Sammlung/Wolf", token="secret"))

        importer.step()

        (asked, token), = repo.asked
        assert (asked.owner, asked.name, asked.branch, asked.subfolder) == (
            "owner", "tonies", "main", "Sammlung/Wolf")
        assert token == "secret"

    def test_the_token_never_shows_on_the_page(self, site):
        site.post("/import/github", data=dict(GITHUB, token="secret"))

        assert "secret" not in site.get("/import").text
        assert "secret" not in site.get("/api/import").text

    def test_a_name_that_is_no_repository_is_refused_on_the_spot(self, site):
        response = site.post("/import/github", data=dict(GITHUB, repo="nonsense"))

        assert response.status_code == 400
        assert say("github.bad_repo") in response.text

    def test_uploaded_dumps_are_imported_and_other_files_ignored(self, site, importer):
        files = [("files", ("Meine/Wolf.nfc", dump("17 C5 DF 11").encode(), "text/plain")),
                 ("files", ("Meine/Fest.nfc", dump("21 C9 91 7D").encode(), "text/plain")),
                 ("files", ("Meine/README.md", b"hello", "text/markdown"))]

        response = site.post("/import/files", data=PACE, files=files, follow_redirects=False)

        run = importer.current()
        assert response.status_code == 303
        assert [item.path for item in run.items] == ["Meine/Wolf.nfc", "Meine/Fest.nfc"]
        assert run.source["label"] == "Meine"

    def test_a_folder_of_more_than_a_thousand_dumps_is_taken(self, site, importer):
        # The form parser's default stops at a thousand files.
        files = [("files", (f"Viele/{index:04}.nfc", dump("17 C5 DF 11").encode(), "text/plain"))
                 for index in range(1001)]

        response = site.post("/import/files", data=PACE, files=files, follow_redirects=False)

        assert response.status_code == 303
        assert len(importer.current().items) == 1001

    def test_an_upload_without_dumps_is_refused(self, site):
        response = site.post("/import/files", data=PACE,
                             files=[("files", ("README.md", b"hello", "text/markdown"))])

        assert response.status_code == 400
        assert say("import.no_dumps") in response.text

    def test_refused_while_cloud_access_is_off(self, site, teddycloud):
        teddycloud.settings["cloud.enabled"] = "false"

        response = site.post("/import/github", data=GITHUB)

        assert response.status_code == 400
        assert say("import.cloud_off") in response.text

    def test_refused_while_another_run_is_going(self, site):
        site.post("/import/github", data=GITHUB)

        response = site.post("/import/github", data=GITHUB)

        assert response.status_code == 409
        assert say("import.busy") in response.text

    def test_a_pace_out_of_range_is_refused(self, site):
        response = site.post("/import/github", data=dict(GITHUB, per_hour="0"))

        assert response.status_code == 400


class TestWatching:
    def test_shows_the_counts_once_checked(self, site, importer):
        site.post("/import/github", data=GITHUB)
        importer.step()
        importer.step()

        state = site.get("/api/import").json()

        assert state["phase"] == "running" and state["counts"][PENDING] == 1

    def test_without_a_run_there_is_nothing_to_watch(self, site):
        assert site.get("/api/import").json() == {"phase": None}

    def test_a_repository_that_failed_says_why(self, site, importer, repo):
        def fails(found, token=""):
            raise RepoError("github.not_found")
        importer.fetch_repo = fails
        site.post("/import/github", data=GITHUB)
        importer.step()

        assert say("github.not_found") in site.get("/import").text

    def test_the_latest_tonies_say_when_with_the_newest_first(self, site, importer, teddycloud):
        from datetime import datetime

        importer.clock = lambda: datetime(2026, 10, 17, 21, 44).timestamp()
        site.post("/import/github", data=GITHUB)
        for _ in range(3):
            importer.step()

        page = site.get("/import").text

        latest = page[page.index(say("import.recent")):]
        assert "Sammlung/Wolf.nfc" in latest and "17.10.2026 21:44" in latest

    def test_the_latest_are_in_order_of_time_not_of_the_list(self, site, importer, teddycloud):
        from tonie_studio.importing import IMPORTED, Item, _load, _save

        site.post("/import/github", data=GITHUB)
        run = importer.current()
        run.items = [Item(path=name, state=IMPORTED, settled_at=when)
                     for name, when in (("A.nfc", 3000.0), ("B.nfc", 1000.0), ("C.nfc", 2000.0))]
        _save(importer.data_dir, run)

        page = site.get("/import").text
        latest = page[page.index(say("import.recent")):]

        assert latest.index("A.nfc") < latest.index("C.nfc") < latest.index("B.nfc")
        assert _load(importer.data_dir).items[0].path == "A.nfc"

    def test_problems_are_in_order_of_time_and_say_when(self, site, importer):
        from datetime import datetime

        from tonie_studio.importing import FAILED, Item, _save

        site.post("/import/github", data=GITHUB)
        run = importer.current()
        early = datetime(2026, 10, 17, 8, 5).timestamp()
        late = datetime(2026, 10, 17, 9, 15).timestamp()
        run.items = [Item(path=name, state=FAILED, error="410", settled_at=when)
                     for name, when in (("Frueh.nfc", early), ("Spaet.nfc", late))]
        _save(importer.data_dir, run)

        page = site.get("/import").text
        problems = page[page.index(say("import.problems")):]

        assert problems.index("Spaet.nfc") < problems.index("Frueh.nfc")
        assert "17.10.2026 09:15" in problems

    def test_tonies_teddycloud_had_before_the_run_are_not_among_the_latest(self, site, importer,
                                                                           teddycloud):
        teddycloud.add_content(WOLF_RUID, source="lib://by/audioID/1.taf", cloud_auth=AUTH)
        site.post("/import/github", data=GITHUB)
        for _ in range(3):
            importer.step()

        page = site.get("/import").text

        assert say("import.recent") not in page

    def test_a_failed_tonie_is_listed_with_its_reason(self, site, importer, teddycloud):
        del teddycloud.cloud[WOLF_RUID]
        site.post("/import/github", data=GITHUB)
        for _ in range(3):
            importer.step()

        page = site.get("/import").text

        assert "Sammlung/Wolf.nfc" in page and "410" in page


class TestControls:
    def test_pause_resume_and_stop(self, site, importer):
        site.post("/import/github", data=GITHUB)

        site.post("/import/pause")
        assert importer.current().paused

        site.post("/import/resume")
        assert not importer.current().paused

        response = site.post("/import/stop", follow_redirects=False)
        assert response.status_code == 303 and importer.current().phase == STOPPED

    def test_after_a_stop_a_new_run_can_start(self, site, importer):
        site.post("/import/github", data=GITHUB)
        site.post("/import/stop")

        assert site.post("/import/github", data=GITHUB, follow_redirects=False).status_code == 303


class TestNames:
    def test_the_import_page_leads_there(self, site):
        assert 'href="/import/names"' in site.get("/import").text

    def test_with_nothing_to_do_it_says_so(self, site):
        assert say("names.nothing") in site.get("/import/names").text

    def test_shows_the_renaming_first_and_writes_nothing(self, site, teddycloud):
        teddycloud.add_tag("1d577820500304e0", source="lib://by/audioID/7.taf",
                           tonie_model="NFC-0004")
        teddycloud.custom_models.append({"model": "NFC-0004", "series": "Comics",
                                         "episodes": "Fledermaus", "audio_id": ["7"],
                                         "hash": ["77"]})

        page = site.get("/import/names").text

        assert say("names.kind.rename") in page and "NFC-0004" in page and "TS-0001" in page
        assert "Comics - Fledermaus" in page
        assert teddycloud.custom_models[0]["model"] == "NFC-0004"

    def test_a_refusal_from_teddycloud_is_said_as_one(self, site, teddycloud):
        from tonie_studio.teddycloud import TeddyCloudError

        teddycloud.add_tag("1d577820500304e0", source="lib://by/audioID/7.taf",
                           tonie_model="NFC-0004")
        teddycloud.custom_models.append({"model": "NFC-0004", "series": "Comics",
                                         "episodes": "Fledermaus", "audio_id": ["7"],
                                         "hash": ["77"]})

        def refuse(model):
            raise TeddyCloudError("teddyCloud answered 400 for /api/toniesCustomJsonUpsert: no",
                                  status=400)
        teddycloud.upsert_custom_tonie = refuse

        page = site.get("/import/names").text
        seen = page.split('name="plan" value="', 1)[1].split('"', 1)[0]

        response = site.post("/import/names", data={"plan": seen})

        assert response.status_code == 409
        assert "answered 400" in response.text and say("offline.albums_work") not in response.text

    def test_applying_after_a_change_writes_nothing_and_shows_the_new_plan(self, site, teddycloud):
        teddycloud.add_tag("1d577820500304e0", source="lib://by/audioID/7.taf",
                           tonie_model="NFC-0004")
        teddycloud.custom_models.append({"model": "NFC-0004", "series": "Comics",
                                         "episodes": "Fledermaus", "audio_id": ["7"],
                                         "hash": ["77"]})

        response = site.post("/import/names", data={"plan": "seen before the change"},
                             follow_redirects=False)

        assert response.headers["location"] == "/import/names?changed=1"
        assert teddycloud.custom_models[0]["model"] == "NFC-0004"
        assert say("names.changed") in site.get("/import/names?changed=1").text

    def test_applying_renames_and_says_how_much(self, site, teddycloud):
        teddycloud.add_tag("1d577820500304e0", source="lib://by/audioID/7.taf",
                           tonie_model="NFC-0004")
        teddycloud.custom_models.append({"model": "NFC-0004", "series": "Comics",
                                         "episodes": "Fledermaus", "audio_id": ["7"],
                                         "hash": ["77"]})

        page = site.get("/import/names").text
        seen = page.split('name="plan" value="', 1)[1].split('"', 1)[0]

        response = site.post("/import/names", data={"plan": seen}, follow_redirects=False)

        assert response.headers["location"] == "/import/names?named=1&deleted=1"
        assert [m["model"] for m in teddycloud.custom_models] == ["TS-0001"]
        assert teddycloud.tag_entries["1d577820500304e0"]["tonie_model"] == "TS-0001"
