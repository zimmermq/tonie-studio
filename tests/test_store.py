"""Tests for the small JSON records the app keeps on the data volume."""

import json
from dataclasses import dataclass

import pytest

from tonie_studio.albumnotes import MADE_FILE, forget_model, remember_made
from tonie_studio.store import changing_json, from_record, read_json


class TestReadJson:
    def test_a_missing_file_reads_as_the_default(self, tmp_path):
        assert read_json(tmp_path / "none.json", {}) == {}

    def test_content_of_another_kind_reads_as_the_default(self, tmp_path):
        path = tmp_path / "record.json"
        path.write_text("[1, 2]", encoding="utf-8")

        assert read_json(path, {}) == {}

    def test_without_a_default_anything_readable_comes_back(self, tmp_path):
        path = tmp_path / "record.json"
        path.write_text("[1, 2]", encoding="utf-8")

        assert read_json(path, None) == [1, 2]

    def test_a_record_is_readable_by_this_app_only(self, tmp_path):
        # The import run keeps each dump's cloud token in its record.
        from tonie_studio.store import write_json

        write_json(tmp_path / "record.json", {"token": "x"})

        assert (tmp_path / "record.json").stat().st_mode & 0o777 == 0o600


class TestAFailedWrite:
    """Most records are notes, so a failed write is logged and the caller carries on;
    callers that must not carry on without their record are told."""

    @staticmethod
    def unwritable(tmp_path):
        # A file where the folder belongs: no write below it can succeed.
        (tmp_path / "data").write_text("", encoding="utf-8")
        return tmp_path / "data" / "record.json"

    def test_does_not_raise_and_says_it_failed(self, tmp_path):
        from tonie_studio.store import write_json

        assert write_json(self.unwritable(tmp_path), {"a": 1}) is False

    def test_a_kept_record_says_so(self, tmp_path):
        from tonie_studio.store import write_json

        assert write_json(tmp_path / "record.json", {"a": 1}) is True

    def test_a_change_that_cannot_be_written_is_carried_on_from(self, tmp_path):
        with changing_json(self.unwritable(tmp_path), {}) as record:
            record["a"] = 1


class TestADamagedRecordIsKept:
    @pytest.mark.parametrize("content", ['{"half": ', "[1, 2]"])
    def test_it_is_set_aside_before_anything_is_written_over_it(self, tmp_path, content):
        # One bad byte must not cost every podcast subscription for good.
        path = tmp_path / "podcasts.json"
        path.write_text(content, encoding="utf-8")

        with changing_json(path, {}) as record:
            record["a"] = 1

        kept = list(tmp_path.glob("podcasts.json.broken-*"))
        assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == content
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_a_missing_file_is_nothing_to_keep(self, tmp_path):
        with changing_json(tmp_path / "new.json", {}) as record:
            record["a"] = 1

        assert not list(tmp_path.glob("*.broken-*"))


class TestChangingJson:
    def test_a_change_is_written(self, tmp_path):
        path = tmp_path / "record.json"

        with changing_json(path, {}) as record:
            record["a"] = 1

        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_nothing_changed_writes_nothing(self, tmp_path):
        path = tmp_path / "record.json"

        with changing_json(path, {}):
            pass

        assert not path.exists()

    def test_a_failure_inside_writes_nothing(self, tmp_path):
        path = tmp_path / "record.json"

        with pytest.raises(RuntimeError), changing_json(path, {}) as record:
            record["a"] = 1
            raise RuntimeError("half way")

        assert not path.exists()


@dataclass
class Point:
    x: int
    y: int = 0


class TestFromRecord:
    def test_unknown_keys_are_left_out(self):
        assert from_record(Point, {"x": 1, "colour": "red"}) == Point(x=1)

    def test_a_missing_required_field_raises(self):
        with pytest.raises(TypeError):
            from_record(Point, {"y": 2})


class TestAlbumNotesOfAnotherShape:
    @pytest.mark.parametrize("change", [
        lambda data_dir: remember_made(data_dir, "album", "model"),
        lambda data_dir: forget_model(data_dir, "model"),
    ])
    def test_a_list_in_the_file_is_carried_on_from(self, tmp_path, change):
        (tmp_path / MADE_FILE).write_text("[]", encoding="utf-8")

        change(tmp_path)


class TestSweep:
    def test_takes_old_folders_too(self, tmp_path):
        # A podcast refresh works in a folder of its own; a process killed
        # in the middle leaves it behind.
        import os

        from tonie_studio.store import sweep

        old, new = tmp_path / "old", tmp_path / "new"
        (old / "inner").mkdir(parents=True)
        (old / "inner" / "ep.mp3").write_bytes(b"x")
        new.mkdir()
        os.utime(old, (0, 0))

        sweep(tmp_path, "*", 3600)

        assert not old.exists() and new.exists()
