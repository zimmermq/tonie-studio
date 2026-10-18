"""Tests for backing up and restoring what makes a tonie a tonie.

The case that matters is the bad day: teddyCloud's volume is gone, the tafs
can be rebuilt from the archive, and everything a person typed and chose - the
names, the covers, which sticker plays what - has to come back exactly.
"""

import io
import json
import zipfile

import pytest
from fake_teddycloud import FakeTeddyCloud

from tonie_studio.backup import BackupError, apply_restore, create_backup, plan_restore, read_backup
from tonie_studio.listening import FILE_NAME as LISTENING_FILE
from tonie_studio.runtimes import FILE_NAME as TIMES_FILE

TIERE = "a9c9ed0e500304e0"


@pytest.fixture
def cloud():
    cloud = FakeTeddyCloud()
    cloud.create_dir("/Lichterkinder")
    cloud.files["/Lichterkinder/Tierische Tierlieder.taf"] = b"taf"
    cloud.custom_models.append({
        "model": "TS-0001", "series": "Bobo Siebenschläfer",
        "episodes": "Tierische Tierlieder", "tracks": ["Grüße", "Papagei"],
        "pic": "/custom_img/TS-0001.jpg", "release": "1640995200",
    })
    cloud.images["TS-0001.jpg"] = b"\xff\xd8\xffcover"
    cloud.add_tag(TIERE, source="lib://Lichterkinder/Tierische Tierlieder.taf",
                  tonie_model="TS-0001", nocloud=True)
    return cloud


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / TIMES_FILE).write_text('{"TS-0001": 5021}')
    (tmp_path / LISTENING_FILE).write_text(
        '{"at": "2026-10-01T18:00:00+00:00", "kind": "placed", "ruid": "a9c9ed0e500304e0"}\n')
    return tmp_path


def wiped(cloud):
    """The same box after teddyCloud lost its volume: tags known, nothing else."""
    fresh = FakeTeddyCloud()
    fresh.dirs, fresh.files = set(cloud.dirs), dict(cloud.files)
    fresh.add_tag(TIERE)
    return fresh


class TestSettingsOfThisApp:
    """Podcast subscriptions and the printer calibration are set up by hand; a lost volume takes them."""

    @staticmethod
    def subscribed(data_dir, **fields):
        from tonie_studio.podcast import Subscription, add_subscription

        fields = {"keep": 3, "guids": ["ep-1"], **fields}
        subscription = Subscription(id="kakadu", feed="https://example.org/feed.xml", ruid=TIERE,
                                    **fields)
        add_subscription(data_dir, subscription)
        return subscription

    def test_come_back_on_a_fresh_volume(self, cloud, data_dir, tmp_path):
        from tonie_studio.calibration import read_shift, save_measurement
        from tonie_studio.podcast import load_subscriptions

        subscription = self.subscribed(data_dir, error="feed was away")
        save_measurement(data_dir, 30.0, 30.0)
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = tmp_path / "fresh"

        apply_restore(wiped(cloud), backup, fresh)

        (restored,) = load_subscriptions(fresh)
        assert (restored.feed, restored.keep, restored.guids) == (
            subscription.feed, 3, ["ep-1"])
        # Last time's failure is no setting worth keeping.
        assert restored.error == ""
        assert read_shift(fresh) == read_shift(data_dir)

    def test_what_is_set_here_stays(self, cloud, data_dir, tmp_path):
        from tonie_studio.calibration import read_shift, reset, save_measurement
        from tonie_studio.podcast import load_subscriptions

        self.subscribed(data_dir)
        save_measurement(data_dir, 30.0, 30.0)
        backup = read_backup(create_backup(cloud, data_dir))
        here = tmp_path / "here"
        self.subscribed(here, keep=7)
        reset(here)

        apply_restore(cloud, backup, here)

        assert [entry.keep for entry in load_subscriptions(here)] == [7]
        assert read_shift(here) == (0.0, 0.0)


class TestCreating:
    def test_holds_models_tags_covers_and_the_apps_records(self, cloud, data_dir):
        archive = zipfile.ZipFile(io.BytesIO(create_backup(cloud, data_dir)))
        names = set(archive.namelist())

        assert {"manifest.json", "models.json", "tags.json", "covers/TS-0001.jpg",
                TIMES_FILE, LISTENING_FILE} <= names

    def test_does_not_carry_the_audio(self, cloud, data_dir):
        # The tafs come back from the archive; a backup holding them would be
        # gigabytes nobody wants to download.
        archive = zipfile.ZipFile(io.BytesIO(create_backup(cloud, data_dir)))

        assert not any(name.endswith(".taf") for name in archive.namelist())

    def test_keeps_umlauts_as_they_are(self, cloud, data_dir):
        archive = zipfile.ZipFile(io.BytesIO(create_backup(cloud, data_dir)))
        models = json.loads(archive.read("models.json").decode("utf-8"))

        assert models[0]["series"] == "Bobo Siebenschläfer"
        assert models[0]["tracks"][0] == "Grüße"


class TestReading:
    def test_refuses_something_that_is_not_a_backup(self):
        with pytest.raises(BackupError):
            read_backup(b"not a zip at all")

    def test_refuses_a_zip_without_a_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("models.json", "[]")

        with pytest.raises(BackupError):
            read_backup(buffer.getvalue())


class TestRestoring:
    def test_waits_for_other_writes(self, cloud, data_dir, tmp_path):
        from web_helpers import waits_for_writing

        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)

        assert waits_for_writing(lambda: apply_restore(fresh, backup, tmp_path / "fresh-data"))

    def test_brings_everything_back_after_the_volume_was_lost(self, cloud, data_dir, tmp_path):
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)
        empty = tmp_path / "fresh-data"

        apply_restore(fresh, backup, empty)

        assert fresh.custom_models == cloud.custom_models
        assert fresh.images == cloud.images
        assert fresh.tag_entries[TIERE]["source"] == cloud.tag_entries[TIERE]["source"]
        assert fresh.tag_entries[TIERE]["tonie_model"] == "TS-0001"
        assert fresh.tag_entries[TIERE]["nocloud"] is True
        assert json.loads((empty / TIMES_FILE).read_text()) == {"TS-0001": 5021}
        assert (empty / LISTENING_FILE).read_text() == (data_dir / LISTENING_FILE).read_text()

    def test_says_what_it_would_do_before_doing_it(self, cloud, data_dir, tmp_path):
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)

        steps = plan_restore(fresh, backup, tmp_path / "fresh-data")

        assert fresh.custom_models == []  # planning writes nothing
        assert {step.what for step in steps} >= {"model", "cover", "tag"}

    def test_a_restore_onto_the_same_state_changes_nothing(self, cloud, data_dir):
        backup = read_backup(create_backup(cloud, data_dir))

        steps = plan_restore(cloud, backup, data_dir)

        assert all(step.action == "same" for step in steps)

    def test_a_tag_the_box_has_not_read_is_skipped_and_said(self, cloud, data_dir, tmp_path):
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)
        fresh.tag_entries.clear()

        steps = plan_restore(fresh, backup, tmp_path / "x")

        tag_step = next(step for step in steps if step.what == "tag")
        assert tag_step.action == "skipped"

    def test_warns_when_a_tag_would_point_at_a_missing_taf(self, cloud, data_dir, tmp_path):
        # The tafs are rebuilt separately; restoring a tag before its taf is
        # back is allowed, but the tonie stays silent until then.
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)
        fresh.files.clear()

        tag_step = next(s for s in plan_restore(fresh, backup, tmp_path / "x") if s.what == "tag")

        assert tag_step.note == "restore.note.taf_missing"

    def test_does_not_delete_what_the_backup_does_not_know(self, cloud, data_dir):
        backup = read_backup(create_backup(cloud, data_dir))
        cloud.custom_models.append({"model": "TS-0002", "series": "Neuer"})

        apply_restore(cloud, backup, data_dir)

        assert any(model["model"] == "TS-0002" for model in cloud.custom_models)

    def test_keeps_a_listening_log_that_has_grown_since(self, cloud, data_dir):
        # Restoring an old log over a newer one would throw away listening.
        backup = read_backup(create_backup(cloud, data_dir))
        with (data_dir / LISTENING_FILE).open("a") as handle:
            handle.write('{"at": "2026-10-02T18:00:00+00:00", "kind": "removed", "ruid": null}\n')
        grown = (data_dir / LISTENING_FILE).read_text()

        apply_restore(cloud, backup, data_dir)

        assert (data_dir / LISTENING_FILE).read_text() == grown


class TestRestoreOnlyAdds:
    """A restore undoes a loss; it must never cause one."""

    def test_a_sticker_empty_in_the_backup_keeps_what_it_plays_now(self, cloud, data_dir):
        cloud.add_tag("deb4ed0e500304e0")  # empty when the backup is made
        backup = read_backup(create_backup(cloud, data_dir))
        cloud.tag_entries["deb4ed0e500304e0"].update(
            source="lib://Lichterkinder/Tierische Tierlieder.taf", tonie_model="TS-0001")

        apply_restore(cloud, backup, data_dir)

        assert cloud.tag_entries["deb4ed0e500304e0"]["source"] != ""
        assert cloud.tag_entries["deb4ed0e500304e0"]["tonie_model"] == "TS-0001"

    def test_a_name_given_since_is_not_taken_away(self, cloud, data_dir):
        cloud.tag_entries[TIERE]["tonie_model"] = ""
        backup = read_backup(create_backup(cloud, data_dir))
        cloud.tag_entries[TIERE]["tonie_model"] = "TS-0001"

        apply_restore(cloud, backup, data_dir)

        assert cloud.tag_entries[TIERE]["tonie_model"] == "TS-0001"

    def test_the_cloud_fallback_is_never_switched_back_on(self, cloud, data_dir):
        cloud.tag_entries[TIERE]["nocloud"] = False
        backup = read_backup(create_backup(cloud, data_dir))
        cloud.tag_entries[TIERE]["nocloud"] = True

        apply_restore(cloud, backup, data_dir)

        assert cloud.tag_entries[TIERE]["nocloud"] is True


class TestRestoreKeepsGoing:
    def test_one_item_that_cannot_be_written_does_not_stop_the_rest(self, cloud, data_dir, tmp_path):
        # Half a restore that stops with an error page leaves nobody knowing
        # what came back. Each item stands on its own, and the list says which
        # one failed.
        cloud.custom_models.insert(0, {"model": "MEIN-1", "series": ""})
        backup = read_backup(create_backup(cloud, data_dir))
        fresh = wiped(cloud)

        steps = apply_restore(fresh, backup, tmp_path / "fresh-data")

        failed = [step for step in steps if step.action == "failed"]
        assert [step.what for step in failed] == ["model"]
        assert any(model["model"] == "TS-0001" for model in fresh.custom_models)
        assert fresh.tag_entries[TIERE]["source"]


class TestDescribing:
    """What the backup page lists, without building the backup itself."""

    def test_lists_each_part_with_its_count(self, cloud, data_dir):
        from tonie_studio.backup import describe_backup

        parts = {part.what: part for part in describe_backup(cloud, data_dir)}

        assert parts["models"].count == 1
        assert parts["tags"].count == 1
        assert parts["covers"].count == 1
        assert parts["listening"].count == 1

    def test_cover_sizes_come_from_the_listing(self, cloud, data_dir):
        # Fetching every picture to measure it would move half the backup on
        # every page view.
        from tonie_studio.backup import describe_backup

        cloud.fetch = lambda path: (_ for _ in ()).throw(AssertionError("fetched " + path))

        cover = next(p for p in describe_backup(cloud, data_dir) if p.what == "covers")

        assert cover.size == len(cloud.images["TS-0001.jpg"])

    def test_a_picture_no_model_uses_is_not_counted(self, cloud, data_dir):
        from tonie_studio.backup import describe_backup

        cloud.images["alt.jpg"] = b"x" * 5000

        cover = next(p for p in describe_backup(cloud, data_dir) if p.what == "covers")

        assert cover.count == 1

    def test_the_estimate_is_close_to_the_real_file(self, cloud, data_dir):
        from tonie_studio.backup import describe_backup

        estimate = sum(part.size for part in describe_backup(cloud, data_dir))
        real = len(create_backup(cloud, data_dir))

        assert abs(estimate - real) < 2048


class TestRestoreSharesTheRecords:
    def test_playing_times_are_merged_under_the_lock_every_record_write_takes(
            self, cloud, data_dir, tmp_path):
        # An encode finishing during a restore records its playing time at the
        # same moment; both have to read, change and write one after the other.
        import threading

        from tonie_studio.store import LOCK

        backup = read_backup(create_backup(cloud, data_dir))
        target = tmp_path / "now"
        target.mkdir()
        (target / TIMES_FILE).write_text('{"TS-0002": 60}')

        with LOCK:
            worker = threading.Thread(target=apply_restore, args=(cloud, backup, target))
            worker.start()
            worker.join(0.3)
            assert json.loads((target / TIMES_FILE).read_text()) == {"TS-0002": 60}
        worker.join(5)

        assert not worker.is_alive()
        assert json.loads((target / TIMES_FILE).read_text()) == {"TS-0001": 5021, "TS-0002": 60}

    def test_recording_a_playing_time_takes_that_lock_too(self, tmp_path):
        import threading

        from tonie_studio.runtimes import playing_times, remember
        from tonie_studio.store import LOCK

        with LOCK:
            worker = threading.Thread(target=remember, args=(tmp_path, "TS-0001", 60))
            worker.start()
            worker.join(0.3)
            assert worker.is_alive()
        worker.join(5)

        assert not worker.is_alive()
        assert playing_times(tmp_path) == {"TS-0001": 60}


class TestRestoringAHostileFile:
    def _backup_with(self, cloud, data_dir, name, content):
        import io
        import zipfile

        buffer = io.BytesIO(create_backup(cloud, data_dir))
        with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, content)
        return buffer.getvalue()

    @pytest.mark.parametrize("name", ["../evil.jpg", "a/b.jpg", "a\\b.jpg", ".hidden.jpg"])
    def test_a_cover_named_with_a_path_is_left_out(self, cloud, data_dir, name):
        # The cover names are written into teddyCloud's custom_img folder; one
        # with a path in it would land somewhere else.
        data = self._backup_with(cloud, data_dir, f"covers/{name}", b"\xff\xd8\xff")

        assert name not in read_backup(data).covers

    def test_refuses_a_backup_that_unpacks_to_far_more_than_any_backup(self, cloud, data_dir,
                                                                       monkeypatch):
        # A small zip can unpack to gigabytes; it is refused before a byte of
        # it is read, not after.
        import tonie_studio.backup as backup

        monkeypatch.setattr(backup, "LARGEST_UNPACKED", 1_000_000)
        data = self._backup_with(cloud, data_dir, "covers/big.jpg", b"\0" * 5_000_000)

        assert len(data) < 1_000_000
        with pytest.raises(BackupError):
            read_backup(data)

    def test_brings_back_only_pictures_as_covers(self, cloud, data_dir):
        data = self._backup_with(cloud, data_dir, "covers/x.html", b"<script>alert(1)</script>")

        assert "x.html" not in read_backup(data).covers


class TestAMalformedBackupIsDamaged:
    """Valid JSON of the wrong shape is a damaged backup, not a crash."""

    @staticmethod
    def _zip(manifest, models, tags):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("models.json", json.dumps(models))
            archive.writestr("tags.json", json.dumps(tags))
        return buffer.getvalue()

    @pytest.mark.parametrize("manifest, models, tags", [
        ([1], [], []),
        ({"format": 1}, {"model": "TS-0001"}, []),
        ({"format": 1}, [], "tags"),
    ])
    def test_says_damaged(self, manifest, models, tags):
        with pytest.raises(BackupError) as caught:
            read_backup(self._zip(manifest, models, tags))

        assert caught.value.key == "backup.damaged"


class TestPlayingTimesFromABackup:
    def test_numbers_that_are_not_finite_are_left_out(self, tmp_path):
        from tonie_studio.runtimes import add_missing, playing_times

        add_missing(tmp_path, {"TS-0001": float("inf"), "TS-0002": float("nan"), "TS-0003": 600})

        assert playing_times(tmp_path) == {"TS-0003": 600}


class TestWhichAlbumBecameWhichTonie:
    """The records of which album a tonie was made from travel with the backup."""

    @pytest.fixture
    def recorded(self, data_dir):
        from tonie_studio.albumnotes import remember_made

        remember_made(data_dir, "a1234", "TS-0001")
        return data_dir

    def test_the_backup_holds_them(self, cloud, recorded):
        from tonie_studio.albumnotes import MADE_FILE

        archive = zipfile.ZipFile(io.BytesIO(create_backup(cloud, recorded)))

        assert MADE_FILE in archive.namelist()

    def test_a_lost_volume_gets_them_back(self, cloud, recorded, tmp_path):
        from tonie_studio.albumnotes import made

        backup = read_backup(create_backup(cloud, recorded))
        empty = tmp_path / "fresh-data"

        apply_restore(wiped(cloud), backup, empty)

        assert made(empty) == {"a1234": ["TS-0001"]}

    def test_records_made_since_are_kept_beside_them(self, cloud, recorded, tmp_path):
        from tonie_studio.albumnotes import made, remember_made

        backup = read_backup(create_backup(cloud, recorded))
        now = tmp_path / "now"
        remember_made(now, "a1234", "TS-0007")
        remember_made(now, "b5678", "TS-0002")

        apply_restore(cloud, backup, now)

        assert made(now) == {"a1234": ["TS-0007", "TS-0001"], "b5678": ["TS-0002"]}

    def test_the_backup_page_lists_them(self, cloud, recorded):
        from tonie_studio.backup import describe_backup

        parts = {part.what: part.count for part in describe_backup(cloud, recorded)}

        assert parts["made"] == 1


class TestParkedBackups:
    def test_a_parked_backup_is_handed_out_once(self, tmp_path):
        from tonie_studio.backup import park, take_parked

        token = park(tmp_path, b"zip")

        assert take_parked(tmp_path, token) == b"zip"
        assert take_parked(tmp_path, token) is None

    def test_a_token_that_is_not_one_reaches_no_file(self, tmp_path):
        from tonie_studio.backup import take_parked

        (tmp_path / "secret.zip").write_bytes(b"x")

        assert take_parked(tmp_path, "../secret") is None
        assert (tmp_path / "secret.zip").exists()

    def test_only_old_parked_backups_are_cleared(self, tmp_path):
        import os

        from tonie_studio.backup import PARKED_FOLDER, clear_parked, park

        old, new = park(tmp_path, b"old"), park(tmp_path, b"new")
        os.utime(tmp_path / PARKED_FOLDER / f"{old}.zip", (0, 0))

        clear_parked(tmp_path)

        assert [path.stem for path in (tmp_path / PARKED_FOLDER).iterdir()] == [new]
