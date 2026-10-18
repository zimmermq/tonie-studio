"""Tests for what the tonie page shows and takes over from the tonies.com shop."""

import pytest
from markupsafe import escape
from fastapi.testclient import TestClient
from web_helpers import jpeg_bytes, publish_album

from tonie_studio.i18n import Translator
from tonie_studio.shop import ShopInfo
from tonie_studio.web import create_app

say = Translator("de")
RUID = "7ab4ed0e500304e0"


def madagascar(**changes):
    info = ShopInfo(sales_id="10001924", url="https://tonies.com/de-de/tonies/madagascar/madagascar/",
                    name="Madagascar", series="Madagascar",
                    picture="https://images.example.com/10001924.png",
                    description="<b>Das Original</b> mit Zootieren.", age_min=3, minutes=55,
                    genre="Hörspiel", genre_key="audio-play", themes=["Abenteuer"],
                    tracks=["Eins", "Zwei"])
    for name, value in changes.items():
        setattr(info, name, value)
    return info


class FakeShop:
    def __init__(self):
        self.products = {"10001924": madagascar()}
        self.looked_up = []

    def lookup(self, sales_id, language="de-de"):
        self.looked_up.append(sales_id)
        return self.products.get(sales_id)

    def search(self, series, episode):
        return list(self.products.values())


@pytest.fixture
def shop():
    return FakeShop()


@pytest.fixture
def pictures():
    asked = []

    def fetch(url, limit=None):
        asked.append(url)
        return jpeg_bytes()
    fetch.asked = asked
    return fetch


@pytest.fixture
def site(settings, teddycloud, runner, box, tags, shop, pictures):
    app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box,
                     shop=shop, fetch=pictures)
    with TestClient(app) as client:
        yield client


class TestAnOfficialTonie:
    @staticmethod
    def official(teddycloud):
        teddycloud.catalogue = [{"model": "10001924", "series": "Madagascar",
                                 "episodes": "Madagascar", "language": "de-de",
                                 "tracks": ["Madagascar"], "release": "1600000000"}]
        teddycloud.tag_entries[RUID].update(source="lib://by/audioID/1.taf", tonie_model="10001924")

    def test_shows_the_shop_s_description_facts_and_link(self, site, teddycloud, shop):
        self.official(teddycloud)

        page = site.get(f"/tonies/{RUID}").text

        assert "<b>Das Original</b> mit Zootieren." in page
        assert "ab 3 Jahren" in page and "55 min" in page and "Abenteuer" in page
        assert 'href="https://tonies.com/de-de/tonies/madagascar/madagascar/"' in page
        assert shop.looked_up == ["10001924"]

    def test_without_the_shop_the_catalogue_still_speaks(self, site, teddycloud, shop):
        self.official(teddycloud)
        shop.products = {}

        page = site.get(f"/tonies/{RUID}").text

        assert "tonies.com/de-de" not in page and say("tonie.about_tracks") in page


class TestAModelOfThisApp:
    def test_offers_to_look_for_it_on_tonies_com(self, site, runner, teddycloud, shop):
        publish_album(site, runner, teddycloud)

        page = site.get(f"/tonies/{RUID}").text

        assert f'href="/tonies/{RUID}/shop"' in page and shop.looked_up == []

    def test_lists_what_the_shop_has_with_its_picture_and_chapters(self, site, runner, teddycloud):
        publish_album(site, runner, teddycloud)

        page = site.get(f"/tonies/{RUID}/shop").text

        assert "Madagascar" in page and 'src="https://images.example.com/10001924.png"' in page
        # The album is encoded with two chapters, the product has two.
        assert say("shop.chapters_match", count=2) in page

    def test_says_when_the_chapters_do_not_match(self, site, runner, teddycloud, shop):
        publish_album(site, runner, teddycloud)
        shop.products["10001924"] = madagascar(tracks=["Eins"])

        page = site.get(f"/tonies/{RUID}/shop").text

        assert say("shop.chapters_differ", shop=1, taf=2) in page

    def test_taking_it_over_brings_cover_category_tracks_and_the_link(self, site, runner,
                                                                       teddycloud, pictures):
        publish_album(site, runner, teddycloud)

        response = site.post(f"/tonies/{RUID}/shop", data={"sales_id": "10001924"},
                             follow_redirects=False)

        model = next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")
        assert response.headers["location"] == f"/tonies/{RUID}"
        assert pictures.asked == ["https://images.example.com/10001924.png"]
        assert model["pic"].startswith("/custom_img/TS-0001")
        assert (model["category"], model["tracks"]) == ("audio-play", ["Eins", "Zwei"])
        assert model["shop_sales_id"] == "10001924"
        assert model["series"] == "Lichterkinder"  # the name stays the one given here

    def test_tracks_are_only_taken_when_the_chapters_match(self, site, runner, teddycloud, shop):
        publish_album(site, runner, teddycloud)
        before = next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")["tracks"]
        shop.products["10001924"] = madagascar(tracks=["Eins"])

        site.post(f"/tonies/{RUID}/shop", data={"sales_id": "10001924"})

        assert next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")["tracks"] == before

    def test_once_linked_its_page_shows_what_the_shop_says(self, site, runner, teddycloud, shop):
        publish_album(site, runner, teddycloud)
        site.post(f"/tonies/{RUID}/shop", data={"sales_id": "10001924"})

        page = site.get(f"/tonies/{RUID}").text

        assert "<b>Das Original</b> mit Zootieren." in page

    def test_an_unknown_product_is_refused(self, site, runner, teddycloud):
        publish_album(site, runner, teddycloud)

        assert site.post(f"/tonies/{RUID}/shop", data={"sales_id": "99"}).status_code == 404


class TestTheLayout:
    def test_an_official_tonie_is_named_by_a_badge_that_keeps_the_reason(self, site, teddycloud):
        TestAnOfficialTonie.official(teddycloud)

        page = site.get(f"/tonies/{RUID}").text

        assert say("tonie.badge_official") in page
        assert str(escape(say("tonie.official", model="10001924"))) in page
        assert 'name="series"' not in page

    def test_the_form_to_edit_a_model_of_this_app_is_folded_away(self, site, runner, teddycloud):
        publish_album(site, runner, teddycloud)

        page = site.get(f"/tonies/{RUID}").text

        folded = page.index('<details class="section">')
        assert say("tonie.badge_own", model="TS-0001") in page
        assert folded < page.index('name="series"') < page.index("</details>", folded)
