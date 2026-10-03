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


class TestPlacing:
    def _placing(self, *episodes):
        from tonie_studio.batch import Placing, Slot

        return Placing([Slot(job_id=f"j{n}", episode=name) for n, name in enumerate(episodes, 1)])

    def test_stickers_go_to_the_episodes_in_the_order_they_are_placed(self):
        placing = self._placing("Folge 1", "Folge 2")
        never = lambda job_id: False  # noqa: E731

        first, _ = placing.reserve("t1", never)
        second, _ = placing.reserve("t2", never)

        assert (first.episode, second.episode) == ("Folge 1", "Folge 2")

    def test_the_same_sticker_placed_again_keeps_its_episode(self):
        # It lies on the box for several polls; it must not eat the next one.
        placing = self._placing("Folge 1", "Folge 2")
        never = lambda job_id: False  # noqa: E731

        placing.reserve("t1", never)
        again, _ = placing.reserve("t1", never)

        assert again.episode == "Folge 1"
        assert placing.waiting(never).episode == "Folge 2"

    def test_a_sticker_placed_after_the_encode_is_published_by_the_placer(self):
        placing = self._placing("Folge 1")
        never = lambda job_id: False  # noqa: E731

        assert placing.settle("j1") is None
        _slot, settled = placing.reserve("t1", never)

        assert settled

    def test_a_sticker_placed_before_the_encode_is_taken_when_it_finishes(self):
        placing = self._placing("Folge 1")
        never = lambda job_id: False  # noqa: E731

        _slot, settled = placing.reserve("t1", never)

        assert not settled
        assert placing.settle("j1") == "t1"

    def test_a_failed_encode_is_passed_over(self):
        placing = self._placing("Folge 1", "Folge 2")

        slot, _ = placing.reserve("t1", lambda job_id: job_id == "j1")

        assert slot.episode == "Folge 2"

    def test_a_failed_encode_gives_its_sticker_back(self):
        placing = self._placing("Folge 1", "Folge 2")
        failed = set()
        placing.reserve("t1", lambda job_id: job_id in failed)

        failed.add("j1")
        slot, _ = placing.reserve("t1", lambda job_id: job_id in failed)

        assert slot.episode == "Folge 2"

    def test_nothing_left_to_place(self):
        placing = self._placing("Folge 1")
        never = lambda job_id: False  # noqa: E731
        placing.reserve("t1", never)

        assert placing.reserve("t2", never) == (None, False)
        assert placing.waiting(never) is None
