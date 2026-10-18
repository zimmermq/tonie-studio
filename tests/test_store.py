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
