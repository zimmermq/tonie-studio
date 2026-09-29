"""Tests for clearing out what browser uploads leave behind."""

import os
import time

from tonie_studio.uploads import remove_old_uploads


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
