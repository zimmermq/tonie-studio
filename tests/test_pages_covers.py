"""Tests for showing and printing pictures teddyCloud keeps in its image cache."""

from markupsafe import escape
from web_helpers import jpeg_bytes, print_sheet

from tonie_studio.i18n import Translator
from tonie_studio.models import cache_name

say = Translator("de")
WARNING = str(escape(say("covers.cache_off")))
CDN = "https://images.example.com/tonies/10000001-cover.png"
NAME = cache_name(CDN)


class TestShowing:
    def test_a_tonie_with_a_cached_picture_shows_it_through_this_app(self, client, teddycloud):
        teddycloud.add_tag("11dfc517500304e0", source="lib://by/audioID/1.taf",
                           tonie_model="10000001", picture=f"/cache/{NAME}")

        page = client.get("/tonies").text

        assert f'src="/cover/cache/{NAME}"' in page

    def test_the_picture_comes_from_teddycloud_s_cache(self, client, teddycloud):
        teddycloud.files[f"/cache/{NAME}"] = jpeg_bytes()

        response = client.get(f"/cover/cache/{NAME}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\xff\xd8\xff")

    def test_refuses_a_name_that_is_no_cache_name(self, client):
        assert client.get("/cover/cache/..%2Fconfig.ini").status_code in (400, 404)
        assert client.get("/cover/cache/nope.png").status_code == 400


class TestWarning:
    def test_the_tonies_page_warns_while_teddycloud_keeps_no_pictures(self, client, teddycloud):
        teddycloud.settings["tonie_json.cache_images"] = "false"

        assert WARNING in client.get("/tonies").text

    def test_and_says_nothing_once_it_does(self, client, teddycloud):
        assert WARNING not in client.get("/tonies").text

    def test_the_sheet_warns_too(self, client, teddycloud):
        teddycloud.settings["tonie_json.cache_images"] = "false"

        assert WARNING in client.get("/sheet").text


class TestPrinting:
    def test_a_model_with_a_picture_hosted_elsewhere_can_be_printed(self, client, teddycloud):
        teddycloud.custom_models.append({"model": "TS-0039", "series": "Eule",
                                         "episodes": "Mit Gefühl", "pic": CDN})
        teddycloud.files[f"/cache/{NAME}"] = jpeg_bytes()

        page = client.get("/sheet").text
        response = print_sheet(client, [("TS-0039", 2)])

        assert "TS-0039" in page and f"/cover/cache/{NAME}" in page
        assert response.status_code == 200 and response.content.startswith(b"%PDF")


WOLF = {"model": "10000581", "series": "Wilde Wölfe", "episodes": "Das große Rennen",
        "language": "de-de", "category": "audio-play", "pic": CDN,
        "tracks": ["Kapitel 1", "Kapitel 2"], "release": "1600000000"}


class TestPrintingOfficialTonies:
    """A tonie named by the catalogue can have its cover printed, asked for or searched."""

    @staticmethod
    def on_a_sticker(teddycloud):
        teddycloud.catalogue = [WOLF, dict(WOLF, model="10000999", episodes="Nirgends")]
        teddycloud.add_tag("11dfc517500304e0", source="lib://by/audioID/1.taf",
                           tonie_model="10000581")
        teddycloud.files[f"/cache/{NAME}"] = jpeg_bytes()

    def test_asked_for_by_the_tonie_list_it_is_on_the_sheet_page(self, client, teddycloud):
        self.on_a_sticker(teddycloud)

        page = client.get("/sheet?model=10000581").text

        assert "10000581" in page and f"/cover/cache/{NAME}" in page

    def test_on_a_sticker_it_is_on_the_page_without_being_asked_for(self, client, teddycloud):
        # The page loads a cover only once its row is in view, so hundreds are fine.
        self.on_a_sticker(teddycloud)

        assert 'data-model="10000581"' in client.get("/sheet").text

    def test_a_search_finds_it_by_name(self, client, teddycloud):
        self.on_a_sticker(teddycloud)

        page = client.get("/sheet?q=wölfe").text

        assert "10000581" in page

    def test_only_tonies_on_a_sticker_are_found(self, client, teddycloud):
        self.on_a_sticker(teddycloud)

        assert "10000999" not in client.get("/sheet?q=Nirgends").text

    def test_its_cover_and_back_are_printed(self, client, teddycloud):
        self.on_a_sticker(teddycloud)

        response = print_sheet(client, [("10000581", 2)], back="1")

        assert response.status_code == 200 and response.content.startswith(b"%PDF")
