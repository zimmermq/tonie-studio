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

        assert say("common.chapters", count=3) in shown
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
        assert 'data-tags="official nopicture"' in row(client.get("/tonies").text, OFFICIAL)


class TestFreeStickers:
    def test_stand_apart_from_the_tonies(self, client):
        page = client.get("/tonies").text

        strip = page[page.index('id="free"'):page.index('id="tonies"')]
        assert 'id="tag-deb4ed0e500304e0"' in strip


class TestTheControls:
    def test_count_each_kind_for_its_filter(self, client, runner, teddycloud, official):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies").text

        assert 'data-filter="official"' in page
        chip = page[page.index('data-filter="official"'):]
        assert ">1<" in chip[:chip.index("</button>")].replace(" ", "")

    def test_offer_both_views_and_every_order(self, client, official):
        page = client.get("/tonies").text

        assert 'data-view="grid"' in page and 'data-view="list"' in page
        for order in ("name", "series", "added", "chapters"):
            assert f'value="{order}"' in page

    def test_are_left_out_while_there_is_no_tonie_to_find(self, client):
        assert 'id="search"' not in client.get("/tonies").text


def known_to_the_shop(settings, **ages):
    """What the shop said earlier, kept beside the app's data: its age per article number."""
    import json

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "shop.json").write_text(json.dumps(
        {sales_id: {"sales_id": sales_id, "age_min": age} for sales_id, age in ages.items()}))


class TestAge:
    def test_an_official_tonie_shows_the_age_the_shop_recommends(self, client, settings,
                                                                 official):
        known_to_the_shop(settings, **{"10000001": 4})

        shown = row(client.get("/tonies").text, OFFICIAL)

        assert say("tonies.age", age=4) in shown and 'data-age="4"' in shown

    def test_an_own_model_linked_to_the_shop_shows_it_too(self, client, runner, teddycloud,
                                                          settings):
        publish_album(client, runner, teddycloud)
        next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")["shop_sales_id"] = "77"
        known_to_the_shop(settings, **{"77": 6})

        assert 'data-age="6"' in row(client.get("/tonies").text, "7ab4ed0e500304e0")

    def test_without_an_answer_the_age_stays_empty(self, client, official):
        assert 'data-age=""' in row(client.get("/tonies").text, OFFICIAL)

    def test_the_shop_is_asked_about_every_tonie_in_one_request(self, client, teddycloud,
                                                               official, no_real_shop):
        teddycloud.catalogue.append({"model": "10000002", "series": "S", "episodes": "E",
                                     "language": "de-de"})
        teddycloud.add_tag("22dfc517500304e0", source="lib://by/audioID/2.taf",
                           tonie_model="10000002")
        asked = []

        def shop(url, params):
            asked.append(params["salesId"])
            return {"documents": []}

        no_real_shop.side_effect = shop
        client.get("/tonies")

        assert asked == ["10000001,10000002"]


class TestSortableColumns:
    def test_every_column_but_the_picture_sorts(self, client, official):
        page = client.get("/tonies").text

        head = page[page.index('class="listing-head'):page.index("</div>", page.index('class="listing-head'))]
        assert [key for key in ("name", "series", "kind", "chapters", "category", "age", "added")
                if f'data-sort="{key}"' in head] == \
            ["name", "series", "kind", "chapters", "category", "age", "added"]


class TestOneDescriptionOfTheColumns:
    """The order menu, the headings and the script's orders come from one list per page."""

    @pytest.mark.parametrize("path", ["/tonies", "/library", "/listening"])
    def test_all_three_name_the_same_columns(self, client, runner, teddycloud, official, path):
        import json
        import re

        publish_album(client, runner, teddycloud)
        page = client.get(path).text

        menu = re.findall(r'<option value="([a-z]+)"', page[page.index('id="sort"'):])
        headings = re.findall(r'data-sort="([a-z]+)"', page)
        data = json.loads(page[page.index('id="page-data">') + 15:page.index("</script>",
                                                                             page.index('id="page-data">'))])
        # The page data comes out in alphabetical order; menu and headings in the column order.
        assert menu == headings and set(menu) == set(data["orders"])
