"""Tests for the pages of the tonies, the box marker, radio tonies and the library."""

import re

from web_helpers import encode_and_assign_page, jpeg_bytes, publish_album


class TestToniesPage:
    def test_counts_what_is_recorded(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)
        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "Lichterkinder",
            "episode": "Halloween", "category": "music",
        }, follow_redirects=False)

        page = client.get("/tonies").text

        assert "1 von 2 Sticker bespielt" in page
        assert "Lichterkinder - Halloween" in page

    def test_lists_free_tags_too(self, client):
        assert "frei" in client.get("/tonies").text


class TestBoxMarker:
    def test_says_nothing_is_on_the_box_before_any_event(self, client):
        state = client.get("/api/box").json()

        assert state["ruid"] is None and state["on_box"] is False

    def test_reports_the_tonie_that_was_placed(self, client, box):
        box.handle("TagValid", "E00403500EEDC9A9")

        state = client.get("/api/box").json()

        assert state["ruid"] == "a9c9ed0e500304e0"
        assert state["on_box"] is True

    def test_clears_it_once_the_tonie_is_lifted(self, client, box):
        box.handle("TagValid", "E00403500EEDC9A9")
        box.handle("rtnl-raw-log2",
                   '{"function_group":15,"function":16067,"field6":"0EEDC9A9E0040350"}')

        assert client.get("/api/box").json()["on_box"] is False

    def test_the_page_carries_a_row_per_tag_to_mark(self, client, teddycloud):
        page = client.get("/tonies").text

        assert 'id="tag-7ab4ed0e500304e0"' in page
        assert 'id="tag-deb4ed0e500304e0"' in page

    def test_forgets_a_reading_that_is_too_old_to_mean_anything(self, client, box):
        from datetime import datetime, timedelta, timezone

        box.handle("TagValid", "E00403500EEDC9A9")
        box._state.last_event = datetime.now(timezone.utc) - timedelta(hours=3)

        state = client.get("/api/box").json()

        assert state["stale"] is True
        assert state["ruid"] is None


class TestEditTonie:
    def test_shows_the_model_behind_a_tag(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert 'name="series" value="Lichterkinder"' in page
        assert "TS-0001" in page

    def test_says_so_when_there_is_nothing_to_edit(self, client):
        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "kein eigenes Modell" in page

    def test_saves_a_corrected_title(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        response = client.post("/tonies/7ab4ed0e500304e0", data={
            "series": "Lichterkinder", "episode": "Halloween Lieder", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.custom_tonies()[0]["episodes"] == "Halloween Lieder"

    def test_keeps_the_audio_identity_across_an_edit(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        before = teddycloud.custom_tonies()[0]["audio_id"]

        client.post("/tonies/7ab4ed0e500304e0", data={
            "series": "Neu", "episode": "Neu", "category": "music",
        }, follow_redirects=False)

        assert teddycloud.custom_tonies()[0]["audio_id"] == before

    def test_replaces_the_cover_from_an_upload(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        client.post("/tonies/7ab4ed0e500304e0",
                    data={"series": "L", "episode": "H", "category": "music"},
                    files={"cover": ("neu.jpg", jpeg_bytes(), "image/jpeg")},
                    follow_redirects=False)

        assert teddycloud.images["TS-0001.jpg"] == jpeg_bytes()

    def test_removing_the_model_leaves_the_tonie_playing(self, client, runner, teddycloud):
        # The audio hangs off the tag, not off the model.
        publish_album(client, runner, teddycloud)

        response = client.post("/tonies/7ab4ed0e500304e0/delete", follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.custom_tonies() == []
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"].startswith("lib://")

    def test_refuses_a_tag_teddycloud_does_not_know(self, client):
        assert client.get("/tonies/ffffffffffffffff").status_code == 404


class TestLibraryPage:
    def test_lists_the_tafs_with_their_size(self, client, runner, teddycloud):
        encode_and_assign_page(client, runner)

        page = client.get("/library").text

        assert "Halloween" in page and re.search(r"\d (Byte|KB|MB)\b", page)

    def test_names_the_tonie_that_would_fall_silent(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/library").text

        assert "wird gespielt von" in page
        assert "Lichterkinder - Halloween" in page

    def test_deletes_one(self, client, runner, teddycloud):
        encode_and_assign_page(client, runner)

        response = client.post("/library/delete",
                               data={"path": "/Lichterkinder/Halloween.taf"},
                               follow_redirects=False)

        assert response.status_code == 303
        assert "/Lichterkinder/Halloween.taf" not in teddycloud.files

    def test_refuses_a_path_that_is_not_a_taf(self, client, teddycloud):
        teddycloud.files["/wichtig.json"] = b"{}"

        response = client.post("/library/delete", data={"path": "/wichtig.json"},
                               follow_redirects=False)

        assert response.status_code == 400
        assert "/wichtig.json" in teddycloud.files


class TestHeaderMarker:
    def test_every_page_carries_the_slot(self, client):
        for path in ("/", "/tonies", "/sheet", "/library"):
            assert 'id="boxnow"' in client.get(path).text

    def test_the_state_carries_a_readable_name(self, client, box, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        box.handle("TagValid", "E00403500EEDB47A")

        state = client.get("/api/box").json()

        assert state["label"] == "Lichterkinder - Halloween"


class TestRadioTonie:
    def test_offers_every_tag_and_some_stations(self, client):
        page = client.get("/radio").text

        assert 'name="ruid"' in page
        assert "7ab4ed0e500304e0" in page and "deb4ed0e500304e0" in page
        assert "<datalist" in page

    def test_says_what_is_different_at_the_box(self, client):
        # Six hours and then silence, and nothing at all without internet.
        page = client.get("/radio").text

        assert "sechs Stunden" in page
        assert "ohne Internet" in page

    def test_points_a_tag_at_a_stream(self, client, teddycloud):
        response = client.post("/radio", data={
            "ruid": "7ab4ed0e500304e0", "url": "http://radio.example/kinder.mp3",
            "series": "Kinderradio", "episode": "", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 303
        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        assert entry["source"] == "http://radio.example/kinder.mp3"
        assert entry["live"] is True

    def test_refuses_something_that_is_not_a_url(self, client, teddycloud):
        response = client.post("/radio", data={
            "ruid": "7ab4ed0e500304e0", "url": "kinderradio",
            "series": "K", "episode": "", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 400
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == ""

    def test_lists_the_radio_tonies_that_exist(self, client, teddycloud):
        client.post("/radio", data={
            "ruid": "7ab4ed0e500304e0", "url": "http://radio.example/kinder.mp3",
            "series": "Kinderradio", "episode": "", "category": "music",
        }, follow_redirects=False)

        page = client.get("/radio").text

        assert "Vorhandene Radio-Tonies" in page
        assert "radio.example" in page


class TestRadioListing:
    def test_a_library_path_is_not_a_stream(self, client, runner, teddycloud):
        # lib:// contains "://" too, so a naive check lists every ordinary
        # tonie as a radio station.
        publish_album(client, runner, teddycloud)

        page = client.get("/radio").text

        assert "Vorhandene Radio-Tonies" not in page


class TestCopyTonie:
    def test_a_second_sticker_plays_the_same(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        played = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        free = next(r for r, e in teddycloud.tag_entries.items() if not e["source"])

        response = client.post(f"/tonies/{played}/copy", data={"target": free},
                               follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.tag_entries[free]["source"] == teddycloud.tag_entries[played]["source"]

    def test_the_page_offers_the_free_sticker(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        played = next(r for r, e in teddycloud.tag_entries.items() if e["source"])

        page = client.get(f"/tonies/{played}").text

        assert "Auf einen weiteren Sticker" in page and "frei - " in page


class TestDeletingASharedModel:
    def test_deleting_the_model_of_a_copy_keeps_it_for_the_original(self, client, runner, teddycloud,
                                                                    settings):
        from tonie_studio.runtimes import playing_times, remember

        publish_album(client, runner, teddycloud)
        original, copy = sorted(teddycloud.tag_entries,
                                key=lambda ruid: not teddycloud.tag_entries[ruid]["source"])
        model = teddycloud.tag_entries[original]["tonie_model"]
        remember(settings.data_dir, model, 1500)
        client.post(f"/tonies/{original}/copy", data={"target": copy}, follow_redirects=False)

        client.post(f"/tonies/{copy}/delete", follow_redirects=False)

        assert [entry["model"] for entry in teddycloud.custom_tonies()] == [model]
        assert teddycloud.tag_entries[original]["tonie_model"] == model
        assert teddycloud.tag_entries[copy]["tonie_model"] == ""
        assert model in playing_times(settings.data_dir)
