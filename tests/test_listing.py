"""Tests for what every list page shares: its columns and its filter chips."""

from tonie_studio.listing import Column, filters, orders


def test_each_column_tells_the_script_how_to_order():
    assert orders([Column("name", "x"), Column("added", "y", number=True, desc=True)]) == {
        "name": {"number": False, "desc": False}, "added": {"number": True, "desc": True}}


def test_filters_are_counted_and_offered_in_order_where_they_keep_a_row():
    counts, offered = filters([["own"], ["official", "nopicture"], ["official"]],
                              ("official", "own", "none", "nopicture"))

    assert offered == ["official", "own", "nopicture"]
    assert (counts["official"], counts["own"], counts["none"]) == (2, 1, 0)
