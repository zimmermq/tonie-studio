"""Tests for cutting a long album into parts a tonie can hold."""

import pytest

from tonie_studio.parts import MOST_CHAPTERS, fewest_parts, part_name, split


def sizes(groups):
    return [len(group) for group in groups]


class TestLimit:
    def test_the_limit_is_what_teddycloud_enforces(self):
        # teddyCloud keeps 100 slots and refuses a new chapter from the 99th
        # on, then carries on encoding into the last one. Nothing past 99
        # can be skipped to.
        assert MOST_CHAPTERS == 99

    def test_fewest_parts_keeps_every_part_within_the_limit(self):
        assert fewest_parts(99) == 1
        assert fewest_parts(100) == 2
        assert fewest_parts(258) == 3


class TestSplit:
    def test_one_part_is_everything_in_order(self):
        assert split([60.0] * 5, 1) == [[0, 1, 2, 3, 4]]

    def test_keeps_the_order_and_loses_nothing(self):
        groups = split([60.0] * 258, 3)

        assert [index for group in groups for index in group] == list(range(258))

    def test_no_part_holds_more_than_a_tonie_can_skip_through(self):
        assert max(sizes(split([60.0] * 258, 3))) <= MOST_CHAPTERS

    def test_parts_play_about_equally_long(self):
        # Ten long chapters and many short ones: counting tracks would put
        # all the long ones in one part.
        durations = [1800.0] * 10 + [60.0] * 90
        groups = split(durations, 2)
        lengths = [sum(durations[index] for index in group) for group in groups]

        assert max(lengths) - min(lengths) <= 1800.0

    def test_unknown_durations_split_by_count(self):
        assert sizes(split([None] * 10, 2)) == [5, 5]

    def test_refuses_more_parts_than_tracks(self):
        with pytest.raises(ValueError):
            split([60.0] * 2, 3)

    def test_refuses_too_few_parts_for_the_limit(self):
        with pytest.raises(ValueError):
            split([60.0] * 200, 2)


class TestNames:
    def test_a_single_part_keeps_the_name(self):
        assert part_name("Der Astronaut", 1, 1) == "Der Astronaut"

    def test_parts_are_numbered_out_of_the_whole(self):
        assert part_name("Der Astronaut", 2, 3) == "Der Astronaut - Teil 2 von 3"
