"""Tests for deciding what an encode makes, the same for one album and for a batch."""

import pytest

from tonie_studio.album import Album, Track
from tonie_studio.planning import PlanError, chosen_tracks, plan_parts, suggest_series


def _track(number, duration=60.0):
    from pathlib import Path

    return Track(path=Path(f"/x/{number:02d}.mp3"), number=number, title=f"Titel {number}",
                 duration=duration)


def _chosen(count):
    return [(_track(n), f"Titel {n}") for n in range(1, count + 1)]


class TestPlanParts:
    def test_one_part_keeps_the_episode_name(self):
        (part,) = plan_parts(_chosen(3), "Serie", "Folge", 1)

        assert (part.target, part.episode, len(part.tracks)) == ("/Serie/Folge.taf", "Folge", 3)

    def test_parts_are_named_and_cover_every_track_once(self):
        parts = plan_parts(_chosen(4), "Serie", "Folge", 2)

        assert [part.episode for part in parts] == ["Folge - Teil 1 von 2", "Folge - Teil 2 von 2"]
        assert sum(len(part.tracks) for part in parts) == 4

    @pytest.mark.parametrize("chosen, parts, key", [
        (0, 1, "error.no_track"), (3, 0, "error.no_parts"), (2, 3, "error.too_many_parts"),
    ])
    def test_refuses_what_cannot_be_played(self, chosen, parts, key):
        with pytest.raises(PlanError) as caught:
            plan_parts(_chosen(chosen), "S", "E", parts)

        assert caught.value.key == key


class TestChosenTracks:
    def test_names_follow_the_order_the_page_sent(self):
        album = Album(path=None, title="A", artist=None, year=None,
                      tracks=[_track(1), _track(2)], cover=None)

        chosen = chosen_tracks(album, "2,1", [1, 2], ["Zweiter", "Erster"])

        assert [(track.number, name) for track, name in chosen] == [(2, "Zweiter"), (1, "Erster")]


class TestSuggestSeries:
    def test_a_folder_in_the_archive_names_the_series(self, tmp_path):
        album = Album(path=tmp_path / "archive" / "Bobo" / "Folge 1", title="Folge 1",
                      artist=None, year=None, tracks=[], cover=None)

        assert suggest_series(album, tmp_path / "archive", tmp_path / "uploads") == "Bobo"

    def test_the_folder_an_upload_was_spooled_into_does_not(self, tmp_path):
        album = Album(path=tmp_path / "uploads" / "a1b2c3" / "Folge 1", title="Folge 1",
                      artist=None, year=None, tracks=[], cover=None)

        assert suggest_series(album, tmp_path / "archive", tmp_path / "uploads") == ""
