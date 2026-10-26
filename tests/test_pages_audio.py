"""Tests for playing audio in the browser: the library's tafs, passed through from teddyCloud."""

import pytest

TAF = "/Lichterkinder/Tierische Tierlieder.taf"


@pytest.fixture
def site(make_site, teddycloud):
    teddycloud.create_dir("/Lichterkinder")
    teddycloud.files[TAF] = b"0123456789"
    return make_site()


class TestAudio:
    def test_a_library_taf_plays(self, site):
        response = site.get("/audio/Lichterkinder/Tierische%20Tierlieder.taf")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/ogg"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-length"] == "10"
        assert response.content == b"0123456789"

    def test_a_range_is_answered_for_seeking(self, site):
        response = site.get("/audio/Lichterkinder/Tierische%20Tierlieder.taf",
                            headers={"Range": "bytes=4-"})

        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 4-9/10"
        assert response.content == b"456789"

    def test_a_taf_that_is_not_there_is_not_found(self, site):
        assert site.get("/audio/Lichterkinder/Weg.taf").status_code == 404

    @pytest.mark.parametrize("path", ["Lichterkinder/notes.txt", "Lichterkinder/%2E%2E/x.taf"])
    def test_only_tafs_inside_the_library_are_served(self, site, path):
        assert site.get(f"/audio/{path}").status_code == 400


class TestPlayButtons:
    """Wherever audio of the library shows, it can be played."""

    @pytest.fixture
    def official(self, teddycloud):
        teddycloud.dirs.update({"/by", "/by/audioID"})
        teddycloud.files["/by/audioID/1.taf"] = b"taf"
        teddycloud.taf_headers["/by/audioID/1.taf"] = {"valid": True, "trackSeconds": [0, 120]}
        teddycloud.taf_info["/by/audioID/1.taf"] = {"model": "10000001", "series": "Madagascar",
                                                    "episode": "Madagascar",
                                                    "tracks": ["Teil 1", "Teil 2"]}
        teddycloud.add_tag("11dfc517500304e0", source="lib://by/audioID/1.taf",
                           tonie_model="10000001", track_seconds=[0, 120])

    def test_every_page_has_the_player(self, site):
        page = site.get("/library").text

        assert 'id="player"' in page and "player.js" in page

    def test_a_taf_in_the_library_plays(self, site, official):
        page = site.get("/library").text

        assert 'data-play="/audio/by/audioID/1.taf"' in page
        assert 'data-starts="0,120"' in page
        assert "data-tracks='[\"Teil 1\", \"Teil 2\"]'" in page

    def test_a_path_with_spaces_and_umlauts_is_encoded(self, site):
        page = site.get("/library").text

        assert 'data-play="/audio/Lichterkinder/Tierische%20Tierlieder.taf"' in page

    def test_a_sticker_in_the_list_plays_without_following_its_link(self, site, official):
        page = site.get("/tonies").text
        start = page.index('id="tag-11dfc517500304e0"')
        row = page[start:page.index("</a>", start)]

        assert 'role="button"' in row and 'data-play="/audio/by/audioID/1.taf"' in row

    def test_a_sticker_without_audio_has_nothing_to_play(self, site):
        page = site.get("/tonies").text
        start = page.index('id="tag-7ab4ed0e500304e0"')

        assert "data-play" not in page[start:page.index("</a>", start)]

    def test_the_sticker_page_plays_it(self, site, official):
        page = site.get("/tonies/11dfc517500304e0").text

        assert 'data-play="/audio/by/audioID/1.taf"' in page
