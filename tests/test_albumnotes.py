"""Tests for what the album list knows beyond the files: what is new, and what
already became a tonie."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tonie_studio.albumnotes import (NEW_FOR, first_sightings, forget_model, guess_made, made,
                                     remember_made)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class TestNew:
    def test_the_first_look_marks_nothing_as_new(self, tmp_path):
        # Everything is "new" to an app that has never looked; marking the
        # whole archive would tell nobody anything.
        seen = first_sightings(tmp_path, ["a1", "a2"], NOW)

        assert all(NOW - when > NEW_FOR for when in seen.values())

    def test_an_album_added_later_is_new(self, tmp_path):
        first_sightings(tmp_path, ["a1"], NOW - timedelta(days=30))

        seen = first_sightings(tmp_path, ["a1", "a2"], NOW)

        assert seen["a2"] == NOW
        assert NOW - seen["a1"] > NEW_FOR

    def test_looking_again_does_not_make_it_newer(self, tmp_path):
        first_sightings(tmp_path, ["a1"], NOW - timedelta(days=30))
        first_sightings(tmp_path, ["a1", "a2"], NOW - timedelta(days=3))

        assert first_sightings(tmp_path, ["a1", "a2"], NOW)["a2"] == NOW - timedelta(days=3)


class TestMade:
    def test_remembers_which_album_became_which_tonie(self, tmp_path):
        remember_made(tmp_path, "a1", "TS-0001")

        assert made(tmp_path) == {"a1": ["TS-0001"]}

    def test_one_album_can_become_several_tonies(self, tmp_path):
        remember_made(tmp_path, "a1", "TS-0001")
        remember_made(tmp_path, "a1", "TS-0005")
        remember_made(tmp_path, "a1", "TS-0005")

        assert made(tmp_path) == {"a1": ["TS-0001", "TS-0005"]}

    def test_a_deleted_tonie_no_longer_marks_its_album(self, tmp_path):
        remember_made(tmp_path, "a1", "TS-0001")

        forget_model(tmp_path, "TS-0001")

        assert made(tmp_path) == {}


def album(identifier, title, artist):
    return (identifier, SimpleNamespace(title=title, artist=artist), "archive")


class TestGuessing:
    """For tonies recorded before the app kept a note: a match by name, and
    only when it cannot be anything else."""

    def test_matches_on_title_and_artist(self):
        albums = [album("a1", "Tierische Tierlieder", "Lichterkinder")]
        models = [{"model": "TS-0001", "series": "Lichterkinder", "episodes": "Tierische Tierlieder"}]

        assert guess_made(albums, models) == {"a1": "TS-0001"}

    def test_matches_a_series_named_in_the_title(self):
        albums = [album("a2", "Bobo Siebenschläfer feiert Weihnachten: Geschichten",
                        "Markus Osterwalder")]
        models = [{"model": "TS-0002", "series": "Bobo Siebenschläfer",
                   "episodes": "Feiert Weihnachten"}]

        assert guess_made(albums, models) == {"a2": "TS-0002"}

    def test_two_possible_albums_means_no_guess(self):
        albums = [album("a1", "Unter Meinem Bett", "VA"), album("a2", "Unter Meinem Bett 2", "VA")]
        models = [{"model": "TS-0004", "series": "Unter meinem Bett", "episodes": "Bett"}]

        assert guess_made(albums, models) == {}

    def test_a_renamed_episode_is_not_guessed(self):
        albums = [album("a1", "Unter Meinem Bett", "VA")]
        models = [{"model": "TS-0004", "series": "Unter meinem Bett", "episodes": "Folge 1"}]

        assert guess_made(albums, models) == {}

    def test_spelling_of_umlauts_does_not_matter(self):
        # Archive names are decomposed, typed names are not.
        albums = [album("a1", "Bärenlied", "Lichterkinder")]
        models = [{"model": "TS-0009", "series": "Lichterkinder", "episodes": "Bärenlied"}]

        assert guess_made(albums, models) == {"a1": "TS-0009"}
