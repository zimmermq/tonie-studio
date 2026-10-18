"""Tests for the list of tonies: what each one shows, and what search, filter and sort work on."""

from datetime import datetime, timezone

import pytest
from web_helpers import publish_album

from tonie_studio.i18n import Translator

say = Translator("de")
OFFICIAL = "11dfc517500304e0"
ADDED = int(datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def official(teddycloud):
    """An official tonie whose audio sits in the library, with three chapters."""
    teddycloud.catalogue = [{"model": "10000001", "series": "Madagascar", "episodes": "Madagascar",
                             "category": "audio-play", "language": "de-de"}]
    teddycloud.dirs.update({"/by", "/by/audioID"})
    teddycloud.files["/by/audioID/1.taf"] = b"taf"
    teddycloud.file_dates["/by/audioID/1.taf"] = ADDED
    teddycloud.add_tag(OFFICIAL, source="lib://by/audioID/1.taf", tonie_model="10000001",
                       track_seconds=[0, 120, 300])


def row(page, ruid):
    start = page.index(f'id="tag-{ruid}"')
    start = page.rindex("<", 0, start)
    return page[start:page.index("</a>", start)]


class TestEachTonie:
    def test_says_what_kind_of_tonie_it_is(self, client, runner, teddycloud, official):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies").text

        assert 'data-kind="official"' in row(page, OFFICIAL)
        assert 'data-kind="own"' in row(page, "7ab4ed0e500304e0")

    def test_shows_chapters_category_and_when_it_was_added(self, client, official):
        shown = row(client.get("/tonies").text, OFFICIAL)

        assert say("tonies.chapters", count=3) in shown
        assert "audio-play" in shown
        assert say.day(datetime.fromtimestamp(ADDED, timezone.utc)) in shown
        assert f'data-added="{ADDED}"' in shown and 'data-chapters="3"' in shown

    def test_the_category_of_an_own_model_is_its_own(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert "music" in row(client.get("/tonies").text, "7ab4ed0e500304e0")

    def test_the_whole_name_is_at_hand_where_it_is_cut_short(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert 'title="Lichterkinder - ' in row(client.get("/tonies").text, "7ab4ed0e500304e0")

    def test_a_tonie_without_a_picture_says_so_for_the_filter(self, client, official):
        assert 'data-picture="no"' in row(client.get("/tonies").text, OFFICIAL)


class TestFreeStickers:
    def test_stand_apart_from_the_tonies(self, client):
        page = client.get("/tonies").text

        strip = page[page.index('id="free"'):page.index('id="tonies"')]
        assert 'id="tag-deb4ed0e500304e0"' in strip


class TestTheControls:
    def test_count_each_kind_for_its_filter(self, client, runner, teddycloud, official):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies").text

        assert 'data-kind-filter="official"' in page
        chip = page[page.index('data-kind-filter="official"'):]
        assert ">1<" in chip[:chip.index("</button>")].replace(" ", "")

    def test_offer_both_views_and_every_order(self, client, official):
        page = client.get("/tonies").text

        assert 'data-view="grid"' in page and 'data-view="list"' in page
        for order in ("name", "series", "added", "chapters"):
            assert f'value="{order}"' in page

    def test_are_left_out_while_there_is_no_tonie_to_find(self, client):
        assert 'id="search"' not in client.get("/tonies").text
