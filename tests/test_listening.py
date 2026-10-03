"""Tests for the listening log: what was played on the box, and for how long."""

from datetime import datetime, timedelta, timezone

import pytest

from tonie_studio.listening import FILE_NAME, LONGEST_SESSION, ListeningLog, summarise

T0 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
TIERE = "a9c9ed0e500304e0"
BOBO = "a5b0ed0e500304e0"


def at(minutes):
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def log(tmp_path):
    return ListeningLog(tmp_path)


class TestRecording:
    def test_keeps_what_it_was_told(self, log):
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(30))

        assert [(event.kind, event.ruid) for event in log.events()] == \
               [("placed", TIERE), ("removed", TIERE)]

    def test_survives_a_restart(self, tmp_path):
        ListeningLog(tmp_path).record("placed", TIERE, at(0))

        assert len(ListeningLog(tmp_path).events()) == 1

    def test_a_damaged_line_costs_that_line_only(self, tmp_path):
        # A crash halfway through a write leaves half a line. The rest of the
        # history is still worth reading.
        log = ListeningLog(tmp_path)
        log.record("placed", TIERE, at(0))
        with (tmp_path / FILE_NAME).open("a", encoding="utf-8") as handle:
            handle.write('{"at": "2026-10-01T18:05')
        log.record("removed", TIERE, at(30))

        assert [event.kind for event in log.events()] == ["placed", "removed"]

    def test_no_file_yet_is_no_history(self, log):
        assert log.events() == []


class TestSummarising:
    def test_counts_how_often_a_tonie_was_put_on(self, log):
        for start in (0, 60, 120):
            log.record("placed", TIERE, at(start))
            log.record("removed", TIERE, at(start + 10))

        assert summarise(log.events(), now=at(200))[TIERE].placed == 3

    def test_adds_up_the_time_on_the_box(self, log):
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(25))

        assert summarise(log.events(), now=at(60))[TIERE].on_box == timedelta(minutes=25)

    def test_adds_up_only_the_time_it_actually_played(self, log):
        # A tonie left on the box after its last chapter is not being heard.
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(1))
        log.record("stopped", TIERE, at(21))
        log.record("removed", TIERE, at(60))

        summary = summarise(log.events(), now=at(90))[TIERE]
        assert summary.played == timedelta(minutes=20)
        assert summary.on_box == timedelta(minutes=60)

    def test_putting_on_another_tonie_ends_the_first(self, log):
        # Swapping without a removal event in between happens when a removal
        # is lost; the first tonie cannot still be on the box.
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(0))
        log.record("placed", BOBO, at(15))

        summary = summarise(log.events(), now=at(20))
        assert summary[TIERE].on_box == timedelta(minutes=15)
        assert summary[TIERE].played == timedelta(minutes=15)
        assert summary[BOBO].placed == 1

    def test_a_box_that_lost_power_does_not_count_for_days(self, log):
        # The removal never arrives when the box goes dark, and without a cap
        # the tonie would be credited with every hour until the next event.
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(0))
        log.record("placed", BOBO, at(60 * 48))

        summary = summarise(log.events(), now=at(60 * 49))[TIERE]
        assert summary.on_box == LONGEST_SESSION
        # Two days of "playing" is no evidence of any listening.
        assert summary.played == timedelta(0)

    def test_a_tonie_still_on_the_box_counts_until_now(self, log):
        log.record("placed", TIERE, at(0))

        assert summarise(log.events(), now=at(10))[TIERE].on_box == timedelta(minutes=10)

    def test_remembers_when_it_was_last_put_on(self, log):
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(5))
        log.record("placed", TIERE, at(100))

        assert summarise(log.events(), now=at(110))[TIERE].last_placed == at(100)

    def test_a_removal_without_a_placement_is_ignored(self, log):
        # After a restart the first thing heard can be a tonie coming off.
        log.record("removed", TIERE, at(0))

        assert summarise(log.events(), now=at(10)) == {}


class TestPeriod:
    def test_only_counts_placements_inside_the_period(self, log):
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(5))
        log.record("placed", TIERE, at(100))
        log.record("removed", TIERE, at(105))

        summary = summarise(log.events(), now=at(200), since=at(50))[TIERE]
        assert summary.placed == 1
        assert summary.on_box == timedelta(minutes=5)

    def test_a_session_across_the_start_counts_only_its_inside_part(self, log):
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(60))

        summary = summarise(log.events(), now=at(90), since=at(45))[TIERE]
        assert summary.on_box == timedelta(minutes=15)
        assert summary.placed == 0

    def test_the_last_time_is_known_even_from_before_the_period(self, log):
        # "Not heard this week" still wants to say when it last was.
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(5))

        summary = summarise(log.events(), now=at(200), since=at(100))
        assert summary[TIERE].last_placed == at(0)


class TestPlayingCannotOutlastTheContent:
    """A box that falls asleep keeps its last log bundle until it wakes, so an
    end can arrive hours late - Home Assistant's record of 29.09. shows an 84
    minute album 'playing' for forty hours. A run longer than its content has
    no observed end, and how much of it was heard is unknown."""

    def test_a_run_longer_than_its_content_is_not_credited(self, log):
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(0))
        log.record("stopped", TIERE, at(60 * 7))

        summary = summarise(log.events(), now=at(60 * 8),
                            content={TIERE: timedelta(minutes=84)})

        assert summary[TIERE].played == timedelta(0)
        assert summary[TIERE].placed == 1

    def test_a_run_within_its_content_counts_in_full(self, log):
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(0))
        log.record("stopped", TIERE, at(80))

        summary = summarise(log.events(), now=at(90), content={TIERE: timedelta(minutes=84)})

        assert summary[TIERE].played == timedelta(minutes=80)

    def test_without_a_known_length_the_general_limit_decides(self, log):
        log.record("placed", TIERE, at(0))
        log.record("started", TIERE, at(0))
        log.record("stopped", TIERE, at(60 * 7))

        summary = summarise(log.events(), now=at(60 * 8), content={})

        assert summary[TIERE].played == timedelta(0)

    def test_lying_on_the_box_is_not_judged_by_the_content(self, log):
        # A tonie can sit on the box long after its last chapter.
        log.record("placed", TIERE, at(0))
        log.record("removed", TIERE, at(150))

        summary = summarise(log.events(), now=at(200), content={TIERE: timedelta(minutes=84)})

        assert summary[TIERE].on_box == timedelta(minutes=150)
