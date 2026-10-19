"""Tests for clearing out what browser uploads leave behind."""

import io
import os
import time

import pytest

from tonie_studio.uploads import remove_old_uploads, spool


def _upload(root, name, age_days=0, size=1024):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "01-track.mp3").write_bytes(b"x" * size)
    when = time.time() - age_days * 86400
    os.utime(folder, (when, when))
    return folder


class TestRemoveOldUploads:
    def test_removes_what_is_past_its_age(self, tmp_path):
        old = _upload(tmp_path, "old", age_days=10)

        removed, freed = remove_old_uploads(tmp_path, keep_days=7)

        assert (removed, freed) == (1, 1024)
        assert not old.exists()

    def test_keeps_what_is_still_young(self, tmp_path):
        recent = _upload(tmp_path, "recent", age_days=2)

        assert remove_old_uploads(tmp_path, keep_days=7) == (0, 0)
        assert recent.exists()

    def test_keeps_an_upload_whose_encode_failed(self, tmp_path):
        # Age is the only criterion on purpose: a failed encode is exactly when
        # the files are still wanted.
        fresh = _upload(tmp_path, "fresh")

        remove_old_uploads(tmp_path, keep_days=7)

        assert fresh.exists()

    def test_reports_how_much_it_freed(self, tmp_path):
        _upload(tmp_path, "a", age_days=10, size=4096)
        _upload(tmp_path, "b", age_days=10, size=2048)

        assert remove_old_uploads(tmp_path, keep_days=7) == (2, 6144)

    def test_does_nothing_when_turned_off(self, tmp_path):
        _upload(tmp_path, "old", age_days=99)

        assert remove_old_uploads(tmp_path, keep_days=0) == (0, 0)

    def test_does_nothing_without_an_upload_directory(self, tmp_path):
        assert remove_old_uploads(tmp_path / "nowhere", keep_days=7) == (0, 0)

    def test_one_unreadable_folder_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        _upload(tmp_path, "broken", age_days=10)
        _upload(tmp_path, "fine", age_days=10)

        import shutil as real_shutil
        original = real_shutil.rmtree

        def guarded(path, *args, **kwargs):
            if "broken" in str(path):
                raise OSError("busy")
            return original(path, *args, **kwargs)

        monkeypatch.setattr("tonie_studio.uploads.shutil.rmtree", guarded)

        assert remove_old_uploads(tmp_path, keep_days=7)[0] == 1
        assert not (tmp_path / "fine").exists()


class TestHousekeepingRepeats:
    """The volume is tidied while the app runs, not only when it starts."""

    def test_old_uploads_go_on_every_round_and_staging_only_at_startup(self, settings,
                                                                      monkeypatch):
        import threading

        from tonie_studio import web

        rounds, staging = [], []
        stop = threading.Event()

        def tidy(_upload_dir, _keep_days):
            rounds.append(1)
            if len(rounds) == 3:
                stop.set()
            return 0, 0

        monkeypatch.setattr(web, "remove_old_uploads", tidy)
        monkeypatch.setattr(web, "remove_orphaned_staging", lambda _client: staging.append(1) or 0)
        monkeypatch.setattr(web, "recover_asides", lambda _client: 0)
        monkeypatch.setattr(web, "HOUSEKEEPING_EVERY", 0.01)

        web._housekeeping(settings, lambda: None, stop)

        assert len(rounds) == 3 and len(staging) == 1

    def test_a_round_that_fails_is_followed_by_the_next(self, settings, monkeypatch):
        import threading

        from tonie_studio import web

        rounds = []
        stop = threading.Event()

        def flaky(_upload_dir, _keep_days):
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("volume went away")
            stop.set()
            return 0, 0

        monkeypatch.setattr(web, "remove_old_uploads", flaky)
        monkeypatch.setattr(web, "remove_orphaned_staging", lambda _client: 0)
        monkeypatch.setattr(web, "recover_asides", lambda _client: 0)
        monkeypatch.setattr(web, "HOUSEKEEPING_EVERY", 0.01)

        web._housekeeping(settings, lambda: None, stop)

        assert len(rounds) == 2


class TestSpool:
    @staticmethod
    def sent(*names):
        return [(name, io.BytesIO(name.encode())) for name in names]

    @staticmethod
    def tree(root):
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def test_one_folder_becomes_one_album(self, tmp_path):
        albums = spool(tmp_path, self.sent("Hits/01-a.mp3", "Hits/cover.jpg", "Hits/notes.txt"))

        assert len(albums) == 1
        assert sorted(path.name for path in albums[0].iterdir()) == ["01-a.mp3", "cover.jpg"]

    def test_a_series_keeps_its_name_and_episode_order(self, tmp_path):
        albums = spool(tmp_path, self.sent("Bibi/Folge 10/01.mp3", "Bibi/Folge 2/01.mp3",
                                           "Bibi/cover.jpg"))

        assert [(path.parent.name, path.name) for path in albums] == [
            ("Bibi", "Folge 2"), ("Bibi", "Folge 10")]

    def test_folders_that_end_alike_get_a_number(self, tmp_path):
        albums = spool(tmp_path, self.sent("A/Best/01.mp3", "B/A/Best/01.mp3"))

        assert sorted(str(path.relative_to(path.parents[1])) for path in albums) == [
            "A/Best", "A/Best (2)"]

    def test_a_name_cannot_climb_out_of_its_folder(self, tmp_path):
        spool(tmp_path, self.sent("x/..\\..\\evil.mp3"))

        assert all(".." not in part for part in self.tree(tmp_path))
        assert not (tmp_path.parent / "evil.mp3").exists()

    def test_no_audio_is_no_album(self, tmp_path):
        assert spool(tmp_path, self.sent("Bilder/cover.jpg")) == []
        assert self.tree(tmp_path) == []

    def test_a_full_disk_leaves_nothing_half_written(self, tmp_path, monkeypatch):
        import errno

        from tonie_studio import uploads

        written = []

        def copy(source, target):
            written.append(target)
            if len(written) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            target.write(source.read())

        monkeypatch.setattr(uploads.shutil, "copyfileobj", copy)

        with pytest.raises(uploads.UploadError) as caught:
            spool(tmp_path, self.sent("Hits/01.mp3", "Hits/02.mp3"))

        assert caught.value.key == "upload.no_space"
        assert self.tree(tmp_path) == [] and list(tmp_path.iterdir()) == []

