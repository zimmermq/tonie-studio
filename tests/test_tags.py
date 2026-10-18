"""Tests for how a tonie is named in lists."""

import pytest

from tonie_studio.tags import label


class TestLabel:
    def test_series_and_episode(self):
        assert label("Lichterkinder", "Halloween") == "Lichterkinder - Halloween"

    def test_whichever_is_there(self):
        assert label("", "Halloween") == "Halloween"
        assert label("Lichterkinder", None) == "Lichterkinder"
        assert label(None, None) == ""

    @pytest.mark.parametrize("series, episode, shown", [
        # The catalogue often repeats the series at the start of the episode.
        ("100% Wolf", "100% Wolf - Das Original-Hörspiel zum Kinofilm",
         "100% Wolf - Das Original-Hörspiel zum Kinofilm"),
        ("Madagascar", "Madagascar", "Madagascar"),
        ("ADAC", "ADAC - Meine erste Verkehrsschule", "ADAC - Meine erste Verkehrsschule"),
        ("asterix", "Asterix, der Gallier", "Asterix, der Gallier"),
    ])
    def test_a_series_the_episode_already_names_is_not_said_twice(self, series, episode, shown):
        assert label(series, episode) == shown

    def test_a_series_that_only_starts_a_longer_word_stays(self):
        assert label("Bibi", "Bibis Geburtstag") == "Bibi - Bibis Geburtstag"
