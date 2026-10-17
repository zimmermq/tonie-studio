"""Tests for the import page: starting a run from GitHub or uploaded dumps, and watching it."""

import random

import pytest
from fastapi.testclient import TestClient

from tonie_studio.github import RepoError
from tonie_studio.i18n import Translator
from tonie_studio.importing import FETCHING, PENDING, STOPPED, Importer
from tonie_studio.web import create_app

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
    importer = Importer(lambda: teddycloud, settings.data_dir, rng=random.Random(1),
                        sleep=lambda s: None, fetch_repo=repo)
    # The tests take the steps themselves; no thread races them.
    importer.start = lambda: None
    return importer


@pytest.fixture
def site(settings, teddycloud, runner, box, importer):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box,
                     importer=importer)
    with TestClient(app) as client:
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
