"""Tests for the playing times kept beside the app's own data.

They live here because the model format teddyCloud shares with the official
catalogue has no field for them, so there is nowhere else to put one.
"""

from tonie_studio.runtimes import FILE_NAME, forget, minutes, playing_times, remember


class TestRemembering:
    def test_keeps_what_it_was_told(self, tmp_path):
        remember(tmp_path, "TS-0001", 3960)

        assert playing_times(tmp_path) == {"TS-0001": 3960}

    def test_survives_a_restart(self, tmp_path):
        remember(tmp_path, "TS-0001", 60)

        assert (tmp_path / FILE_NAME).exists()
        assert playing_times(tmp_path)["TS-0001"] == 60

    def test_a_second_recording_replaces_the_first(self, tmp_path):
        remember(tmp_path, "TS-0001", 60)
        remember(tmp_path, "TS-0001", 120)

        assert playing_times(tmp_path)["TS-0001"] == 120

    def test_ignores_a_time_it_does_not_have(self, tmp_path):
        remember(tmp_path, "TS-0001", None)
        remember(tmp_path, "TS-0002", 0)

        assert playing_times(tmp_path) == {}


class TestForgetting:
    def test_drops_the_time_with_the_model(self, tmp_path):
        remember(tmp_path, "TS-0001", 60)
        forget(tmp_path, "TS-0001")

        assert playing_times(tmp_path) == {}

    def test_forgetting_something_unknown_is_harmless(self, tmp_path):
        forget(tmp_path, "TS-9999")

        assert playing_times(tmp_path) == {}


class TestReading:
    def test_nothing_stored_yet_is_not_an_error(self, tmp_path):
        assert playing_times(tmp_path) == {}

    def test_a_damaged_file_does_not_stop_a_sheet(self, tmp_path):
        # A sticker without a playing time is worth printing; a sheet that
        # refuses because a side note is unreadable is not.
        (tmp_path / FILE_NAME).write_text("{ not json")

        assert playing_times(tmp_path) == {}

    def test_entries_that_are_not_times_are_left_out(self, tmp_path):
        (tmp_path / FILE_NAME).write_text('{"TS-0001": "lang", "TS-0002": 90}')

        assert playing_times(tmp_path) == {"TS-0002": 90}


class TestMinutes:
    def test_rounds_to_whole_minutes(self):
        assert minutes(3960) == 66
        assert minutes(3990) == 67

    def test_something_shorter_than_a_minute_still_counts_as_one(self):
        assert minutes(20) == 1

    def test_nothing_known_is_nothing_shown(self):
        assert minutes(None) is None
        assert minutes(0) is None
