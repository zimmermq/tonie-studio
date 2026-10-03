"""Tests for planning several albums at once."""

from tonie_studio.batch import allocate, episode_order


class TestEpisodeOrder:
    def test_numbers_count_as_numbers(self):
        # "Folge 10" sorted as text lands between 1 and 2, and the stickers
        # would be handed out in that order.
        names = ["Folge 10", "Folge 2", "Folge 1"]

        assert sorted(names, key=episode_order) == ["Folge 1", "Folge 2", "Folge 10"]

    def test_case_does_not_matter(self):
        assert sorted(["b", "A", "c"], key=episode_order) == ["A", "b", "c"]


class TestAllocate:
    def test_hands_out_free_tags_in_order(self):
        assert allocate([1, 1, 1], ["t1", "t2", "t3", "t4"]) == [["t1"], ["t2"], ["t3"]]

    def test_an_album_in_parts_gets_one_tag_per_part(self):
        assert allocate([1, 2], ["t1", "t2", "t3"]) == [["t1"], ["t2", "t3"]]

    def test_stops_where_the_tags_run_out(self):
        assert allocate([1, 1, 1], ["t1", "t2"]) == [["t1"], ["t2"], []]

    def test_never_gives_an_album_only_some_of_its_parts(self):
        # Half an album on a sticker is a tonie that stops in the middle.
        assert allocate([1, 2, 1], ["t1", "t2"]) == [["t1"], [], []]

    def test_keeps_the_order_rather_than_filling_gaps(self):
        # Skipping the album that did not fit and giving its tag to a later
        # one would leave episode 3 on a sticker while episode 2 has none.
        assert allocate([2, 1], ["t1"]) == [[], []]

    def test_no_free_tags_gives_no_tags(self):
        assert allocate([1, 1], []) == [[], []]
