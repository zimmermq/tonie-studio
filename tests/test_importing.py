"""Tests for importing tonies from Flipper Zero dumps.

The importer runs for days, one tonie every few minutes, and has to carry on
after a restart. The tests drive it one step at a time with a clock of their
own, so a schedule of hours runs in milliseconds and every wait is visible.
"""

import json
import random

import pytest

from fake_teddycloud import FakeTeddyCloud
from tonie_studio.github import Repo, RepoError
from tonie_studio.importing import (CACHED, CHECKING, DUPLICATE, FAILED, FETCHING, FINISHED,
                                    GIVEN_UP, IMPORTED, PENDING, STOPPED, UNREADABLE,
                                    ImportProblem, Importer, Pace, cached_at, import_one)

AUTH = "AB" * 32


def dump(last_bytes, auth=AUTH):
    """A dump whose UID ends in ``last_bytes``, e.g. "17 C5 DF 11" gives rUID 11dfc517500304e0."""
    spaced = " ".join(auth[i:i + 2] for i in range(0, len(auth), 2))
    return f"Filetype: Flipper NFC device\nUID: E0 04 03 50 {last_bytes}\nData Content: {spaced}\n"


WOLF = ("Wolf.nfc", dump("17 C5 DF 11"))
WOLF_RUID = "11dfc517500304e0"
FEST = ("Fest.nfc", dump("21 C9 91 7D"))
FEST_RUID = "7d91c921500304e0"
BIBI = ("Bibi.nfc", dump("1D C2 6E A6"))
BIBI_RUID = "a66ec21d500304e0"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def teddycloud():
    fake = FakeTeddyCloud()
    for ruid in (WOLF_RUID, FEST_RUID, BIBI_RUID):
        fake.cloud[ruid] = b"taf of " + ruid.encode()
    return fake


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def importer(teddycloud, tmp_path, clock):
    return Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                    sleep=lambda seconds: None)


STEADY = Pace(per_hour=10, jitter=0, max_failures=3)


def states(importer):
    return {item.path: item.state for item in importer.current().items}


def run_until_idle(importer, clock, limit=50):
    """Step until the importer has nothing to do now; returns the wait it asked for."""
    for _ in range(limit):
        wait = importer.step()
        if wait > 0:
            return wait
    raise AssertionError("the importer never came to rest")


class TestCachedAt:
    def test_a_tonie_in_the_library_is_cached_there(self, teddycloud):
        teddycloud.add_content(WOLF_RUID, source="lib://by/audioID/1.taf", cloud_auth=AUTH)

        assert cached_at(teddycloud, WOLF_RUID) == "lib://by/audioID/1.taf"

    def test_a_taf_beside_its_json_counts(self, teddycloud):
        teddycloud.add_content(WOLF_RUID, source="", cloud_auth=AUTH)
        teddycloud.files["/11DFC517/500304E0"] = b"taf"

        assert cached_at(teddycloud, WOLF_RUID) == "content/11DFC517/500304E0"

    def test_an_empty_file_beside_it_does_not(self, teddycloud):
        teddycloud.add_content(WOLF_RUID, source="", cloud_auth=AUTH)
        teddycloud.files["/11DFC517/500304E0"] = b""

        assert cached_at(teddycloud, WOLF_RUID) == ""

    def test_a_tonie_teddycloud_has_never_seen_is_not(self, teddycloud):
        assert cached_at(teddycloud, WOLF_RUID) == ""


class TestImportOne:
    def test_writes_the_content_json_and_caches_the_taf(self, teddycloud):
        source = import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        entry = json.loads(teddycloud.files["/11DFC517/500304E0.json"])
        assert (entry["cloud_ruid"], entry["cloud_auth"]) == (WOLF_RUID, AUTH)
        assert source.startswith("lib://by/audioID/")
        assert teddycloud.cloud_requests == [WOLF_RUID]

    def test_keeps_what_an_existing_entry_already_says(self, teddycloud):
        teddycloud.add_content(WOLF_RUID, source="", tonie_model="10000123", hide=True)

        import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        entry = json.loads(teddycloud.files["/11DFC517/500304E0.json"])
        assert (entry["tonie_model"], entry["hide"], entry["cloud_auth"]) == ("10000123", True, AUTH)

    def test_a_refusal_from_the_cloud_is_passed_on(self, teddycloud):
        del teddycloud.cloud[WOLF_RUID]

        with pytest.raises(Exception) as raised:
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert getattr(raised.value, "status", None) == 410

    def test_a_download_teddycloud_did_not_keep_is_a_failure(self, teddycloud):
        teddycloud.settings["cloud.cacheContent"] = "false"

        with pytest.raises(ImportProblem) as raised:
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert raised.value.key == "import.not_cached"

    def test_waits_for_the_move_into_the_library_rather_than_report_the_file_beside(
            self, teddycloud):
        # teddyCloud renames the finished taf next to the JSON first and moves it
        # into the library a moment later; asked in between, it is beside the JSON.
        teddycloud.settings["cloud.cacheToLibrary"] = "false"

        def move_into_library(seconds):
            path = "/11DFC517/500304E0"
            if path in teddycloud.files:
                teddycloud.files["/by/audioID/7.taf"] = teddycloud.files.pop(path)
                entry = json.loads(teddycloud.files[path + ".json"])
                entry["source"] = "lib://by/audioID/7.taf"
                teddycloud.files[path + ".json"] = json.dumps(entry).encode()

        assert import_one(teddycloud, WOLF_RUID, AUTH, sleep=move_into_library) == \
            "lib://by/audioID/7.taf"

    def test_without_the_library_the_taf_stays_beside_its_json(self, teddycloud):
        teddycloud.settings["cloud.cacheToLibrary"] = "false"

        assert import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None) == "content/11DFC517/500304E0"


class TestPlanning:
    def test_sorts_dumps_into_pending_cached_duplicate_and_unreadable(self, importer, teddycloud):
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/9.taf", cloud_auth=AUTH)

        importer.begin([WOLF, FEST, ("Wolf copy.nfc", WOLF[1]), ("notes.nfc", "hello")], STEADY, "test")
        importer.step()

        assert states(importer) == {"Wolf.nfc": PENDING, "Fest.nfc": CACHED,
                                    "Wolf copy.nfc": DUPLICATE, "notes.nfc": UNREADABLE}

    def test_an_unreadable_dump_says_why(self, importer):
        importer.begin([("notes.nfc", "hello")], STEADY, "test")

        assert importer.current().items[0].error_key == "nfc.missing_fields"

    def test_refuses_to_start_while_cloud_access_is_off(self, importer, teddycloud):
        teddycloud.settings["cloud.enabled"] = "false"

        with pytest.raises(ImportProblem) as raised:
            importer.begin([WOLF], STEADY, "test")

        assert raised.value.key == "import.cloud_off"
        assert importer.current() is None

    def test_refuses_a_second_run_while_one_is_going(self, importer):
        importer.begin([WOLF], STEADY, "test")

        with pytest.raises(ImportProblem) as raised:
            importer.begin([FEST], STEADY, "test")

        assert raised.value.key == "import.busy"

    def test_refuses_a_pace_that_makes_no_sense(self, importer):
        with pytest.raises(ImportProblem) as raised:
            importer.begin([WOLF], Pace(per_hour=0), "test")

        assert raised.value.key == "import.bad_pace"


class TestPacing:
    def test_imports_the_first_at_once_and_the_next_after_the_interval(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST], STEADY, "test")

        wait = run_until_idle(importer, clock)

        assert teddycloud.cloud_requests == [WOLF_RUID]
        assert wait == pytest.approx(360)

        clock.now += 360
        run_until_idle(importer, clock)
        assert teddycloud.cloud_requests == [WOLF_RUID, FEST_RUID]
        assert importer.current().phase == FINISHED

    def test_the_spread_keeps_each_pause_within_its_bounds(self, teddycloud, tmp_path, clock):
        importer = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(7),
                            sleep=lambda s: None)
        importer.begin([WOLF, FEST], Pace(per_hour=10, jitter=0.25), "test")

        wait = run_until_idle(importer, clock)

        assert 270 <= wait <= 450

    def test_a_tonie_cached_meanwhile_is_skipped_without_waiting(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST, BIBI], STEADY, "test")
        run_until_idle(importer, clock)
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/5.taf", cloud_auth=AUTH)

        clock.now += 360
        run_until_idle(importer, clock)

        assert states(importer)["Fest.nfc"] == CACHED
        assert teddycloud.cloud_requests == [WOLF_RUID, BIBI_RUID]

    def test_finishes_and_forgets_the_token_when_nothing_is_left(self, importer, clock):
        importer.begin([WOLF], STEADY, "test", token="secret")

        run_until_idle(importer, clock)

        run = importer.current()
        assert run.phase == FINISHED and states(importer) == {"Wolf.nfc": IMPORTED}
        assert "secret" not in json.dumps(run.source)


class TestQuiet:
    def test_a_new_tonie_is_looked_up_once_per_check_not_once_per_call(self, importer, teddycloud,
                                                                      clock):
        # Each lookup of a tonie teddyCloud does not know yet is a warning in its log.
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert teddycloud.content_misses == 2  # the check, and the look right before


class TestFailures:
    def test_a_failure_is_recorded_and_the_run_goes_on(self, importer, teddycloud, clock):
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF, FEST], STEADY, "test")

        run_until_idle(importer, clock)
        clock.now += 360
        run_until_idle(importer, clock)

        assert states(importer) == {"Wolf.nfc": FAILED, "Fest.nfc": IMPORTED}
        assert "410" in importer.current().items[0].error

    def test_gives_up_after_too_many_failures_in_a_row(self, importer, teddycloud, clock):
        for ruid in (WOLF_RUID, FEST_RUID):
            del teddycloud.cloud[ruid]
        importer.begin([WOLF, FEST, BIBI], Pace(per_hour=10, jitter=0, max_failures=2), "test")

        run_until_idle(importer, clock)
        clock.now += 360
        run_until_idle(importer, clock)

        assert importer.current().phase == GIVEN_UP
        assert states(importer)["Bibi.nfc"] == PENDING

    def test_carrying_on_after_giving_up_starts_the_count_afresh(self, importer, teddycloud, clock):
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF, FEST], Pace(per_hour=10, jitter=0, max_failures=1), "test")
        run_until_idle(importer, clock)

        importer.resume()
        clock.now += 360
        run_until_idle(importer, clock)

        assert states(importer)["Fest.nfc"] == IMPORTED


class TestControls:
    def test_a_paused_run_imports_nothing_until_resumed(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST], STEADY, "test")
        importer.step()
        importer.pause()

        clock.now += 3600
        importer.step()
        assert teddycloud.cloud_requests == []

        importer.resume()
        run_until_idle(importer, clock)
        assert teddycloud.cloud_requests == [WOLF_RUID]

    def test_stopping_ends_the_run_and_forgets_the_token(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST], STEADY, "test", token="secret")
        importer.stop()

        importer.step()

        run = importer.current()
        assert run.phase == STOPPED and teddycloud.cloud_requests == []
        assert "secret" not in json.dumps(run.source)

    def test_a_stopped_run_makes_room_for_a_new_one(self, importer):
        importer.begin([WOLF], STEADY, "test")
        importer.stop()

        importer.begin([FEST], STEADY, "again")

        assert importer.current().source["label"] == "again"


class TestRestart:
    def test_a_new_importer_carries_on_where_the_last_one_stopped(self, teddycloud, tmp_path, clock):
        first = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                         sleep=lambda s: None)
        first.begin([WOLF, FEST], STEADY, "test")
        run_until_idle(first, clock)

        second = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                          sleep=lambda s: None)

        assert second.step() == pytest.approx(360)
        clock.now += 360
        run_until_idle(second, clock)
        assert teddycloud.cloud_requests == [WOLF_RUID, FEST_RUID]

    def test_a_damaged_state_file_means_no_run(self, tmp_path, teddycloud):
        (tmp_path / "import.json").write_text("{not json")

        importer = Importer(lambda: teddycloud, tmp_path)

        assert importer.current() is None


class FakeRepo:
    """Stands in for github.dumps: hands out dumps, or fails, and remembers the token."""

    def __init__(self, found=(WOLF, FEST), fails=None):
        self.found = list(found)
        self.fails = fails
        self.tokens = []

    def __call__(self, repo, token=""):
        self.tokens.append(token)
        if self.fails:
            raise self.fails
        return self.found


def github_importer(teddycloud, tmp_path, clock, fetch):
    return Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                    sleep=lambda s: None, fetch_repo=fetch)


class TestFromGitHub:
    def test_starting_fetches_nothing_until_the_worker_runs(self, teddycloud, tmp_path, clock):
        fetch = FakeRepo()
        importer = github_importer(teddycloud, tmp_path, clock, fetch)

        run = importer.begin_github(Repo("owner", "tonies"), STEADY, token="secret")

        assert run.phase == FETCHING and run.items == [] and fetch.tokens == []

    def test_the_worker_reads_the_dumps_and_then_forgets_the_token(self, teddycloud, tmp_path, clock):
        fetch = FakeRepo()
        importer = github_importer(teddycloud, tmp_path, clock, fetch)
        importer.begin_github(Repo("owner", "tonies", subfolder="Sammlung"), STEADY, token="secret")

        importer.step()

        run = importer.current()
        assert fetch.tokens == ["secret"]
        assert run.phase == CHECKING and [item.path for item in run.items] == ["Wolf.nfc", "Fest.nfc"]
        assert "secret" not in (tmp_path / "import.json").read_text()
        assert run.source["label"] == "owner/tonies/Sammlung"

    def test_then_imports_like_any_other_run(self, teddycloud, tmp_path, clock):
        importer = github_importer(teddycloud, tmp_path, clock, FakeRepo())
        importer.begin_github(Repo("owner", "tonies"), STEADY)

        run_until_idle(importer, clock)

        assert teddycloud.cloud_requests == [WOLF_RUID]

    def test_a_repository_that_cannot_be_read_ends_the_run_and_says_why(self, teddycloud, tmp_path, clock):
        fetch = FakeRepo(fails=RepoError("github.not_found"))
        importer = github_importer(teddycloud, tmp_path, clock, fetch)
        importer.begin_github(Repo("owner", "tonies"), STEADY, token="secret")

        importer.step()

        run = importer.current()
        assert run.phase == STOPPED and run.error_key == "github.not_found"
        assert "secret" not in (tmp_path / "import.json").read_text()

    def test_a_restart_while_fetching_fetches_again(self, teddycloud, tmp_path, clock):
        fetch = FakeRepo()
        github_importer(teddycloud, tmp_path, clock, fetch).begin_github(
            Repo("owner", "tonies"), STEADY, token="secret")

        github_importer(teddycloud, tmp_path, clock, fetch).step()

        assert fetch.tokens == ["secret"]
