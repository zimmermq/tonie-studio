"""Tests for showing and printing pictures teddyCloud keeps in its image cache."""

from markupsafe import escape
from web_helpers import jpeg_bytes

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
        response = client.post("/sheet", data={"models": ["TS-0039"], "counts": ["2"]})

        assert "TS-0039" in page and f"/cover/cache/{NAME}" in page
        assert response.status_code == 200 and response.content.startswith(b"%PDF")
