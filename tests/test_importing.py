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
                                    GIVEN_UP, IMPORTED, PENDING, RESTORE, RETRY_SECONDS, RUNNING,
                                    SKIP_FILE, SKIPPED, STOPPED, SWITCH_OFF, UNREADABLE, Cloud,
                                    ImportProblem, Importer, Pace, Skip, add_skip, cached_at,
                                    import_one, remove_skip, skip_list)

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

    def test_an_empty_download_is_a_failure(self, teddycloud):
        # The cloud answering with nothing leaves nothing to play.
        from tonie_studio.importing import ImportProblem

        teddycloud.cloud[WOLF_RUID] = b""

        with pytest.raises(ImportProblem) as raised:
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert raised.value.key == "import.no_taf"

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


class TestNeverTheWrongToken:
    """A download goes to the cloud only with the tonie's own token in place."""

    def test_an_entry_teddycloud_did_not_keep_stops_before_the_cloud(self, teddycloud):
        # teddyCloud can answer an upload it did not finish; downloading then
        # would send the token another request left on the connection.
        teddycloud.upload = lambda *args, **kwargs: None

        with pytest.raises(ImportProblem) as raised:
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda seconds: None)

        assert raised.value.key == "import.entry_not_written"
        assert teddycloud.cloud_requests == []

    def test_a_kept_entry_goes_ahead(self, teddycloud):
        assert import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda seconds: None)
        assert teddycloud.cloud_requests == [WOLF_RUID]


class TestCloudOffOnceThere:
    """The cloud is needed once, to fetch a tonie's audio; after that it is not asked again."""

    @staticmethod
    def nocloud(teddycloud, ruid):
        return json.loads(teddycloud.files[f"/{ruid[:8].upper()}/{ruid[8:].upper()}.json"])["nocloud"]

    def test_an_imported_tonie_no_longer_asks_the_cloud(self, teddycloud):
        import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert self.nocloud(teddycloud, WOLF_RUID) is True

    def test_a_failed_download_leaves_the_cloud_on_for_the_next_try(self, teddycloud):
        teddycloud.settings["cloud.cacheContent"] = "false"

        with pytest.raises(ImportProblem):
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert self.nocloud(teddycloud, WOLF_RUID) is False

    def test_a_tonie_teddycloud_already_had_is_switched_off_too(self, importer, teddycloud):
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/9.taf", cloud_auth=AUTH,
                               nocloud=False)

        importer.begin([FEST], STEADY, "test")
        importer.step()

        assert self.nocloud(teddycloud, FEST_RUID) is True

    def test_one_that_turned_up_by_its_turn_is_switched_off_too(self, importer, teddycloud,
                                                               clock):
        importer.begin([WOLF, FEST], STEADY, "test")
        run_until_idle(importer, clock)
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/5.taf", cloud_auth=AUTH,
                               nocloud=False)

        clock.now += 360
        run_until_idle(importer, clock)

        assert self.nocloud(teddycloud, FEST_RUID) is True


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


class TestCloudSwitch:
    """A run that switches teddyCloud's cloud access on for itself, and back when it ends."""

    @pytest.fixture
    def cloud_off(self, teddycloud):
        teddycloud.settings["cloud.enabled"] = "false"
        return teddycloud

    def cloud(self, teddycloud):
        return {name: teddycloud.settings[name] for name in ("cloud.enabled", "cloud.cacheContent")}

    def test_switches_the_cloud_on_imports_and_puts_it_back(self, importer, cloud_off, clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))

        importer.step()
        assert self.cloud(cloud_off) == {"cloud.enabled": "true", "cloud.cacheContent": "true"}
        run_until_idle(importer, clock)

        assert states(importer) == {"Wolf.nfc": IMPORTED}
        assert self.cloud(cloud_off) == {"cloud.enabled": "false", "cloud.cacheContent": "true"}
        assert importer.current().cloud.before == {}

    def test_every_change_is_saved_so_a_restart_of_teddycloud_keeps_it(self, importer, cloud_off,
                                                                       clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        assert [saved["cloud.enabled"] for saved in cloud_off.saved_settings] == ["true", "false"]

    def test_restoring_leaves_a_cloud_that_was_on_before_on(self, importer, teddycloud, clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True, after=RESTORE))
        run_until_idle(importer, clock)

        assert self.cloud(teddycloud) == {"cloud.enabled": "true", "cloud.cacheContent": "true"}

    def test_restoring_puts_back_caching_it_switched_on(self, importer, cloud_off, clock):
        cloud_off.settings["cloud.cacheContent"] = "false"
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        assert self.cloud(cloud_off) == {"cloud.enabled": "false", "cloud.cacheContent": "false"}

    def test_switching_off_turns_the_cloud_off_even_when_it_was_on(self, importer, teddycloud,
                                                                  clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True, after=SWITCH_OFF))
        run_until_idle(importer, clock)

        assert self.cloud(teddycloud) == {"cloud.enabled": "false", "cloud.cacheContent": "true"}

    def test_a_stopped_run_puts_the_cloud_back(self, importer, cloud_off, clock):
        importer.begin([WOLF, FEST], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        importer.stop()
        importer.step()

        assert importer.current().phase == STOPPED
        assert cloud_off.settings["cloud.enabled"] == "false"

    def test_a_run_that_gives_up_puts_it_back_and_takes_it_again_when_resumed(
            self, importer, cloud_off, clock):
        cloud_off.cloud_refusing.update({WOLF_RUID, FEST_RUID})
        importer.begin([WOLF, FEST, BIBI], Pace(per_hour=10, jitter=0, max_failures=2), "test",
                       cloud=Cloud(switch=True))
        run_until_idle(importer, clock)
        clock.now += 360
        run_until_idle(importer, clock)
        assert importer.current().phase == GIVEN_UP
        assert cloud_off.settings["cloud.enabled"] == "false"

        importer.resume()
        importer.step()

        assert importer.current().phase == RUNNING
        assert cloud_off.settings["cloud.enabled"] == "true"

    def test_a_paused_run_keeps_the_cloud_on(self, importer, cloud_off, clock):
        importer.begin([WOLF, FEST], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        importer.pause()
        importer.step()

        assert cloud_off.settings["cloud.enabled"] == "true"

    def test_an_importer_started_anew_still_puts_it_back(self, importer, cloud_off, tmp_path,
                                                          clock):
        importer.begin([WOLF, FEST], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        again = Importer(lambda: cloud_off, tmp_path, clock=clock, rng=random.Random(1),
                         sleep=lambda seconds: None)
        clock.now += 360
        run_until_idle(again, clock)

        assert again.current().phase == FINISHED
        assert cloud_off.settings["cloud.enabled"] == "false"

    def test_a_setting_teddycloud_refuses_ends_the_run_and_asks_the_cloud_nothing(
            self, importer, cloud_off, clock):
        cloud_off.settings_refusing.add("cloud.enabled")
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))

        run_until_idle(importer, clock)

        run = importer.current()
        assert run.phase == STOPPED
        assert run.error_key == "import.cloud_not_switched"
        assert cloud_off.cloud_requests == []
        assert cloud_off.settings["cloud.enabled"] == "false"

    def test_putting_it_back_waits_for_teddycloud_to_answer(self, importer, cloud_off, clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))
        importer.step()
        importer.stop()

        cloud_off.settings_down = True
        assert importer.step() == RETRY_SECONDS
        assert importer.current().cloud.before

        cloud_off.settings_down = False
        importer.step()
        assert cloud_off.settings["cloud.enabled"] == "false"
        assert importer.current().cloud.before == {}

    def test_a_new_run_takes_over_what_the_last_one_still_owes(self, importer, cloud_off, clock):
        importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True))
        importer.step()
        importer.stop()
        cloud_off.settings_down = True
        importer.step()
        cloud_off.settings_down = False

        importer.begin([FEST], STEADY, "test", cloud=Cloud(switch=True))
        run_until_idle(importer, clock)

        assert states(importer) == {"Fest.nfc": IMPORTED}
        assert cloud_off.settings["cloud.enabled"] == "false"

    def test_without_the_switch_the_settings_are_left_alone(self, importer, teddycloud, clock):
        importer.begin([WOLF], STEADY, "test")
        run_until_idle(importer, clock)

        assert teddycloud.saved_settings == []

    def test_a_choice_for_afterwards_it_does_not_know_is_refused(self, importer):
        with pytest.raises(ImportProblem) as raised:
            importer.begin([WOLF], STEADY, "test", cloud=Cloud(switch=True, after="later"))

        assert raised.value.key == "import.bad_cloud"


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


class TestWhen:
    def test_a_tonie_the_run_imported_notes_when(self, importer, clock):
        importer.begin([WOLF], STEADY, "test")
        clock.now = 2_000_000.0

        run_until_idle(importer, clock)

        assert importer.current().items[0].settled_at == 2_000_000.0

    def test_one_teddycloud_had_before_the_run_has_no_time(self, importer, teddycloud, clock):
        # The run did nothing for it, so it is no part of what the run did lately.
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/9.taf", cloud_auth=AUTH)
        importer.begin([FEST], STEADY, "test")

        run_until_idle(importer, clock)

        assert importer.current().items[0].settled_at == 0.0

    def test_one_that_turned_up_by_its_turn_notes_when(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST], STEADY, "test")
        run_until_idle(importer, clock)
        teddycloud.add_content(FEST_RUID, source="lib://by/audioID/5.taf", cloud_auth=AUTH)
        clock.now += 360

        run_until_idle(importer, clock)

        assert importer.current().items[1].settled_at == clock.now

    def test_a_failure_notes_it_too(self, importer, teddycloud, clock):
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert importer.current().items[0].settled_at == clock.now


class TestQuiet:
    def test_a_new_tonie_is_looked_up_once_per_check_not_once_per_call(self, importer, teddycloud,
                                                                      clock):
        # Each lookup of a tonie teddyCloud does not know yet is a warning in its log.
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert teddycloud.content_misses == 2  # the check, and the look right before


class TestAfterImport:
    def test_each_imported_tonie_is_handed_on_with_its_dump(self, teddycloud, tmp_path, clock):
        seen = []
        importer = Importer(lambda: teddycloud, tmp_path, clock=clock, sleep=lambda s: None,
                            after_import=lambda ruid, path: seen.append((ruid, path)))
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert seen == [(WOLF_RUID, "Wolf.nfc")]

    def test_a_failure_after_the_import_does_not_undo_it(self, teddycloud, tmp_path, clock):
        def fails(ruid, path):
            raise RuntimeError("naming broke")
        importer = Importer(lambda: teddycloud, tmp_path, clock=clock, sleep=lambda s: None,
                            after_import=fails)
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert states(importer) == {"Wolf.nfc": IMPORTED}


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


class TestTheSkipList:
    """Dumps the cloud will not hand the audio for are left out of later runs."""

    def _refused_once(self, importer, teddycloud, clock):
        teddycloud.cloud_refusing.add(WOLF_RUID)
        importer.begin([WOLF, FEST], STEADY, "first")
        run_until_idle(importer, clock)
        clock.now += 360
        run_until_idle(importer, clock)

    def test_a_dump_the_cloud_refuses_goes_on_the_list(self, importer, teddycloud, clock, tmp_path):
        self._refused_once(importer, teddycloud, clock)

        (entry,) = skip_list(tmp_path)

        assert (entry.ruid, entry.path) == (WOLF_RUID, "Wolf.nfc")
        assert entry.token and "401" in entry.reason

    def test_the_list_never_holds_a_token(self, importer, teddycloud, clock, tmp_path):
        # A dump's token is what the cloud takes for the tonie; it stays in
        # the run record only while a run needs it.
        self._refused_once(importer, teddycloud, clock)

        assert AUTH not in (tmp_path / SKIP_FILE).read_text(encoding="utf-8")

    def test_a_later_run_leaves_it_out_without_asking(self, importer, teddycloud, clock):
        self._refused_once(importer, teddycloud, clock)
        importer.begin([WOLF, BIBI], STEADY, "second")
        asked = len(teddycloud.cloud_requests)

        run_until_idle(importer, clock)

        assert states(importer)["Wolf.nfc"] == SKIPPED
        assert teddycloud.cloud_requests[asked:] == [BIBI_RUID]

    def test_a_skipped_one_neither_waits_nor_counts_as_failing(self, importer, teddycloud, clock):
        self._refused_once(importer, teddycloud, clock)
        importer.begin([WOLF, BIBI], Pace(per_hour=10, jitter=0, max_failures=1), "second")

        run_until_idle(importer, clock)

        assert importer.current().phase == FINISHED
        assert states(importer)["Bibi.nfc"] == IMPORTED

    def test_a_new_dump_of_the_same_tonie_is_tried(self, importer, teddycloud, clock):
        self._refused_once(importer, teddycloud, clock)
        teddycloud.cloud_refusing.clear()
        fresh = ("Wolf neu.nfc", dump("17 C5 DF 11", auth="CD" * 32))
        importer.begin([fresh], STEADY, "second")

        run_until_idle(importer, clock)

        assert states(importer)["Wolf neu.nfc"] == IMPORTED

    def test_an_entry_without_a_token_leaves_out_every_dump_of_the_sticker(self, importer,
                                                                          teddycloud, clock,
                                                                          tmp_path):
        add_skip(tmp_path, Skip(ruid=WOLF_RUID, path="Wolf.nfc"))
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert states(importer)["Wolf.nfc"] == SKIPPED
        assert teddycloud.cloud_requests == []

    def test_one_put_on_the_list_during_a_run_is_left_out_at_its_turn(self, importer,
                                                                      teddycloud, clock,
                                                                      tmp_path):
        importer.begin([FEST, WOLF], STEADY, "test")
        run_until_idle(importer, clock)
        add_skip(tmp_path, Skip(ruid=WOLF_RUID, path="Wolf.nfc"))

        clock.now += 360
        run_until_idle(importer, clock)

        assert states(importer) == {"Fest.nfc": IMPORTED, "Wolf.nfc": SKIPPED}

    def test_other_failures_are_not_put_on_it(self, importer, teddycloud, clock, tmp_path):
        # A tonie the cloud does not have now may have it tomorrow.
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF], STEADY, "test")

        run_until_idle(importer, clock)

        assert states(importer)["Wolf.nfc"] == FAILED
        assert skip_list(tmp_path) == []

    def test_taken_off_it_the_dump_is_tried_again(self, importer, teddycloud, clock, tmp_path):
        self._refused_once(importer, teddycloud, clock)
        teddycloud.cloud_refusing.clear()

        assert remove_skip(tmp_path, WOLF_RUID) == 1
        importer.begin([WOLF], STEADY, "second")
        run_until_idle(importer, clock)

        assert states(importer)["Wolf.nfc"] == IMPORTED

    def test_a_failed_dump_of_the_run_can_be_put_on_it_by_hand(self, importer, teddycloud,
                                                                clock, tmp_path):
        # Still running, so the dump's token is there to take a fingerprint of.
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF, FEST], STEADY, "test")
        run_until_idle(importer, clock)

        assert importer.skip_failed("Wolf.nfc")
        (entry,) = skip_list(tmp_path)
        assert entry.ruid == WOLF_RUID and entry.token

    def test_after_the_run_the_entry_covers_every_dump_of_the_sticker(self, importer, teddycloud,
                                                                        clock, tmp_path):
        del teddycloud.cloud[WOLF_RUID]
        importer.begin([WOLF], STEADY, "test")
        run_until_idle(importer, clock)

        assert importer.skip_failed("Wolf.nfc")
        assert skip_list(tmp_path)[0].token == ""

    def test_refusals_from_before_the_list_existed_are_taken_onto_it(self, importer, teddycloud,
                                                                     clock, tmp_path):
        self._refused_once(importer, teddycloud, clock)
        (tmp_path / SKIP_FILE).unlink()

        importer.adopt_refusals()

        assert [entry.ruid for entry in skip_list(tmp_path)] == [WOLF_RUID]


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


class Disk:
    """Stands in for the data volume's writes: working, full, or losing what it is given."""

    OK, FULL, LOSSY = "ok", "full", "lossy"

    def __init__(self, real):
        self.real = real
        self.mode = self.OK

    def __call__(self, path, value):
        if self.mode == self.FULL:
            return False
        if self.mode == self.LOSSY:
            return True
        return self.real(path, value)


@pytest.fixture
def disk(monkeypatch):
    from tonie_studio import importing, store

    stand_in = Disk(store.write_json)
    monkeypatch.setattr(importing, "write_json", stand_in)
    monkeypatch.setattr(store, "write_json", stand_in)
    return stand_in


def count_lookups(teddycloud):
    """Counts every question to teddyCloud about a tonie's entry or a folder."""
    asked = []
    for name in ("content_entry", "list_dir"):
        original = getattr(teddycloud, name)

        def counted(*args, _original=original, **kwargs):
            asked.append(args)
            return _original(*args, **kwargs)
        setattr(teddycloud, name, counted)
    return asked


class TestARunThatCannotBeSaved:
    """A run whose record cannot be written asks nobody anything until it can.

    Every step works from the stored run, so a result that is not stored is
    worked out again at once: without a hold that is teddyCloud asked
    thousands of times a minute, and the Tonies cloud asked again and again
    for a tonie it refused.
    """

    def test_a_refused_tonie_is_not_asked_for_again_and_again(self, importer, teddycloud, clock,
                                                              disk):
        from tonie_studio.importing import HOLD_SECONDS

        teddycloud.cloud_refusing.add(WOLF_RUID)
        importer.begin([WOLF, FEST], STEADY, "test")
        importer.step()
        disk.mode = Disk.FULL
        asked = count_lookups(teddycloud)

        waits = [importer.step() for _ in range(50)]

        assert teddycloud.cloud_requests == []
        assert all(wait >= HOLD_SECONDS for wait in waits)
        assert len(asked) <= 2  # the one look before the start could not be noted

    def test_the_check_is_not_repeated_either(self, importer, teddycloud, disk):
        importer.begin([WOLF, FEST], STEADY, "test")
        disk.mode = Disk.FULL
        asked = count_lookups(teddycloud)

        importer.step()
        once = len(asked)
        for _ in range(50):
            importer.step()

        assert len(asked) == once

    def test_the_page_is_told_why(self, importer, disk):
        importer.begin([WOLF], STEADY, "test")
        disk.mode = Disk.FULL

        importer.step()

        assert importer.held.key == "import.cannot_save"

    def test_it_carries_on_once_the_record_can_be_written_again(self, importer, teddycloud,
                                                               clock, disk):
        importer.begin([WOLF], STEADY, "test")
        disk.mode = Disk.FULL
        importer.step()

        disk.mode = Disk.OK
        run_until_idle(importer, clock)

        assert importer.held is None
        assert teddycloud.cloud_requests == [WOLF_RUID]
        assert states(importer) == {"Wolf.nfc": IMPORTED}

    def test_a_result_that_could_not_be_noted_is_not_fetched_again_before_its_time(
            self, importer, teddycloud, clock, disk):
        teddycloud.cloud_refusing.add(WOLF_RUID)
        importer.begin([WOLF, FEST], STEADY, "test")
        importer.step()
        refuse = teddycloud.cache_content

        def disk_fills_meanwhile(folder, file):
            disk.mode = Disk.FULL
            return refuse(folder, file)
        teddycloud.cache_content = disk_fills_meanwhile
        importer.step()

        disk.mode = Disk.OK
        wait = importer.step()

        assert teddycloud.cloud_requests == [WOLF_RUID]
        assert wait >= 360

    def test_the_pace_holds_even_when_the_record_loses_what_it_was_given(self, importer,
                                                                        teddycloud, clock, disk):
        # The worker keeps the pace in memory too, so no answer from the disk
        # makes it ask the cloud sooner than the pace allows.
        teddycloud.cloud_refusing.add(WOLF_RUID)
        importer.begin([WOLF, FEST], STEADY, "test")
        importer.step()
        disk.mode = Disk.LOSSY

        importer.step()
        wait = importer.step()

        assert teddycloud.cloud_requests == [WOLF_RUID]
        assert wait == pytest.approx(360)

    def test_a_run_that_cannot_be_noted_does_not_start(self, importer, disk):
        disk.mode = Disk.FULL

        with pytest.raises(ImportProblem) as raised:
            importer.begin([WOLF], STEADY, "test")

        assert raised.value.key == "import.cannot_save"


class Killed(BaseException):
    """The app going away in the middle of a download."""


class TestAKillDuringADownload:
    """teddyCloud carries on with a download the app stopped waiting for."""

    def _killed(self, importer, teddycloud, clock):
        importer.begin([WOLF, FEST], STEADY, "test")
        importer.step()
        fetch = teddycloud.cache_content

        def killed(folder, file):
            raise Killed()
        teddycloud.cache_content = killed
        with pytest.raises(Killed):
            importer.step()
        teddycloud.cache_content = fetch

    def test_a_restart_does_not_fetch_the_same_tonie_again_at_once(self, importer, teddycloud,
                                                                  tmp_path, clock):
        self._killed(importer, teddycloud, clock)

        again = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                         sleep=lambda s: None)
        wait = again.step()

        assert wait >= 360 and teddycloud.cloud_requests == []

    def test_after_the_pause_it_is_fetched_again_if_teddycloud_has_nothing(self, importer,
                                                                          teddycloud, tmp_path,
                                                                          clock):
        self._killed(importer, teddycloud, clock)
        again = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                         sleep=lambda s: None)

        clock.now += again.step()
        run_until_idle(again, clock)

        assert teddycloud.cloud_requests == [WOLF_RUID]
        assert states(again)["Wolf.nfc"] == IMPORTED

    def test_audio_teddycloud_finished_meanwhile_counts_as_imported(self, importer, teddycloud,
                                                                   tmp_path, clock):
        self._killed(importer, teddycloud, clock)
        teddycloud.add_content(WOLF_RUID, source="lib://by/audioID/3.taf", cloud_auth=AUTH,
                               nocloud=False)
        seen = []
        again = Importer(lambda: teddycloud, tmp_path, clock=clock, rng=random.Random(1),
                         sleep=lambda s: None,
                         after_import=lambda ruid, path: seen.append(path))

        again.step()

        assert states(again)["Wolf.nfc"] == IMPORTED
        assert seen == ["Wolf.nfc"] and teddycloud.cloud_requests == []
        assert TestCloudOffOnceThere.nocloud(teddycloud, WOLF_RUID) is True


def cut_short(teddycloud, finished):
    """teddyCloud's connection breaks during the download; ``finished``: it completes it anyway."""
    from tonie_studio.teddycloud import TeddyCloudError

    fetch = teddycloud.cache_content

    def broken(folder, file):
        if finished:
            fetch(folder, file)
        else:
            teddycloud.cloud_requests.append(f"{folder}{file}".lower())
        raise TeddyCloudError(f"download of {folder}/{file} from teddyCloud broke off "
                              "after 0 bytes: Connection broken")
    teddycloud.cache_content = broken


class TestADownloadCutShort:
    def test_audio_teddycloud_finished_anyway_is_imported(self, teddycloud):
        cut_short(teddycloud, finished=True)

        where = import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert where.startswith("lib://by/audioID/")
        assert TestCloudOffOnceThere.nocloud(teddycloud, WOLF_RUID) is True

    def test_otherwise_it_fails_with_a_reason_a_person_reads(self, teddycloud):
        cut_short(teddycloud, finished=False)

        with pytest.raises(ImportProblem) as raised:
            import_one(teddycloud, WOLF_RUID, AUTH, sleep=lambda s: None)

        assert raised.value.key == "import.download_broke"

    def test_teddycloud_is_given_time_to_finish(self, teddycloud):
        cut_short(teddycloud, finished=False)
        slept = []

        def finishes_later(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                teddycloud.add_content(WOLF_RUID, source="lib://by/audioID/8.taf",
                                       cloud_auth=AUTH)

        assert import_one(teddycloud, WOLF_RUID, AUTH, sleep=finishes_later) == \
            "lib://by/audioID/8.taf"

    def test_it_is_no_refusal_by_the_cloud(self, importer, teddycloud, clock, tmp_path):
        cut_short(teddycloud, finished=False)
        importer.begin([WOLF, FEST], Pace(per_hour=10, jitter=0, max_failures=1), "test")

        run_until_idle(importer, clock)

        run = importer.current()
        assert run.phase != GIVEN_UP and run.streak == 0
        assert run.items[0].error_key == "import.download_broke"
        assert skip_list(tmp_path) == []
