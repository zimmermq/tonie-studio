"""Tests for the pages of the tonies, the box marker, radio tonies and the library."""

import re
from unittest.mock import patch

import pytest
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

    def test_names_the_tonie_without_reading_the_tag_index_on_every_poll(self, client, box,
                                                                          teddycloud):
        box.handle("TagValid", "E00403500EEDC9A9")
        original = teddycloud.tags
        asked = []
        teddycloud.tags = lambda: asked.append(1) or original()

        for _poll in range(5):
            client.get("/api/box")

        assert len(asked) == 1

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
        assert 'class="warn"' not in page  # nothing is wrong with it

    def test_a_tonie_the_catalogue_names_says_where_its_name_comes_from(self, client, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"].update(
            source="lib://by/audioID/1.taf", tonie_model="10000581",
            picture="/cache/" + "A" * 64 + ".png")

        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "offizielle Katalog" in page and "10000581" in page
        assert 'class="warn"' not in page
        assert 'href="/sheet?model=10000581"' in page
        assert 'src="/cover/cache/' + "A" * 64 + '.png?size=' in page

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

    def test_deleting_the_tonie_frees_the_sticker_and_deletes_model_and_taf(self, client, runner,
                                                                          teddycloud):
        publish_album(client, runner, teddycloud)
        played = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]

        response = client.post("/tonies/7ab4ed0e500304e0/delete", follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.custom_tonies() == []
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == ""
        assert "/" + played[len("lib://"):] not in teddycloud.files

    def test_the_page_lists_what_deleting_takes_with_it(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        played = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]

        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "TS-0001" in page and played[len("lib://"):] in page
        assert 'action="/tonies/7ab4ed0e500304e0/delete"' in page

    def test_a_sticker_without_a_model_can_be_emptied_too(self, client, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Weg/Weg.taf"

        page = client.get("/tonies/7ab4ed0e500304e0").text
        client.post("/tonies/7ab4ed0e500304e0/delete", follow_redirects=False)

        assert 'action="/tonies/7ab4ed0e500304e0/delete"' in page
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == ""

    def test_a_free_sticker_offers_nothing_to_delete(self, client):
        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "/delete" not in page

    def test_its_podcast_subscription_goes_too(self, client, runner, teddycloud, settings):
        from tonie_studio.podcast import Subscription, add_subscription, load_subscriptions

        publish_album(client, runner, teddycloud)
        add_subscription(settings.data_dir, Subscription(id="p1", feed="https://x.example/f",
                                                         ruid="7ab4ed0e500304e0"))
        add_subscription(settings.data_dir, Subscription(id="p2", feed="https://x.example/g",
                                                         ruid="deb4ed0e500304e0"))

        client.post("/tonies/7ab4ed0e500304e0/delete", follow_redirects=False)

        assert [entry.id for entry in load_subscriptions(settings.data_dir)] == ["p2"]

    def test_refuses_a_tag_teddycloud_does_not_know(self, client):
        assert client.get("/tonies/ffffffffffffffff").status_code == 404

    def test_editing_an_unknown_tag_says_it_is_unknown(self, client):
        response = client.post("/tonies/ffffffffffffffff", data={"series": "x"})

        assert response.status_code == 404
        assert "teddyCloud kennt diesen Sticker nicht" in response.text


class TestLibraryPage:
    def test_lists_the_tafs_with_their_size(self, client, runner, teddycloud):
        encode_and_assign_page(client, runner)

        page = client.get("/library").text

        assert "Halloween" in page and re.search(r"\d (Byte|KB|MB)\b", page)

    def test_names_the_tonie_that_would_fall_silent(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/library").text

        assert 'href="/tonies/7ab4ed0e500304e0">Lichterkinder - Halloween</a>' in page

    @staticmethod
    def imported(teddycloud, name="1636444326.taf", info=None, ruid="11dfc517500304e0"):
        """A taf teddyCloud fetched from the cloud, filed under its audio id."""
        teddycloud.dirs.update({"/by", "/by/audioID"})
        path = f"/by/audioID/{name}"
        teddycloud.files[path] = b"taf"
        teddycloud.taf_headers[path] = {"valid": True, "trackSeconds": [0, 60]}
        teddycloud.taf_info[path] = info or {"model": "10000681", "series": "Barbie",
                                             "episode": "Princess Adventure"}
        if ruid:
            teddycloud.add_tag(ruid, source=f"lib:/{path}", tonie_model="10000681")
        return path

    def test_an_imported_taf_is_named_after_its_tonie_not_its_number(self, client, teddycloud):
        self.imported(teddycloud)

        shown = self.row(client.get("/library").text, "/by/audioID/1636444326.taf")

        assert "Barbie - Princess Adventure" in shown
        assert 'data-tags="imported"' in shown

    def test_says_where_each_taf_comes_from_and_whether_it_is_used(self, client, runner,
                                                                  teddycloud):
        publish_album(client, runner, teddycloud)
        self.imported(teddycloud, ruid="")

        page = client.get("/library").text

        assert 'data-tags="recorded"' in page and 'data-tags="imported unused"' in page

    def test_the_box_s_own_sounds_are_shown_but_cannot_be_deleted(self, client, teddycloud):
        path = self.imported(teddycloud, "1580745093.taf", ruid="",
                             info={"model": "box-en-us-02-00000000",
                                   "series": "System sounds en-us", "episode": "Jingle (Startup)"})

        shown = self.row(client.get("/library").text, path)

        assert 'data-tags="box"' in shown and "/library/delete" not in shown

    def test_a_file_teddycloud_cannot_name_is_named_after_its_sticker(self, client, teddycloud):
        path = self.imported(teddycloud, "1678897164.taf", info={"model": ""})
        teddycloud.custom_models.append({"model": "TS-0099", "series": "Fantasia"})
        teddycloud.tag_entries["11dfc517500304e0"]["tonie_model"] = "TS-0099"

        shown = self.row(client.get("/library").text, path)

        assert "<strong>Fantasia</strong>" in shown

    def test_is_in_the_main_navigation(self, client):
        page = client.get("/").text

        nav = page[page.index('<nav class="mainnav"'):page.index("</nav>")]
        assert nav.index('href="/library"') < nav.index("<details")

    @staticmethod
    def row(page, path):
        start = page.rindex("<div", 0, page.index(f'data-path="{path}"'))
        end = page.find('<div class="taf', start + 10)
        return page[start:end if end != -1 else len(page)]

    def test_deletes_one(self, client, runner, teddycloud):
        encode_and_assign_page(client, runner)

        response = client.post("/library/delete",
                               data={"path": "/Lichterkinder/Halloween.taf"},
                               follow_redirects=False)

        assert response.status_code == 303
        assert "/Lichterkinder/Halloween.taf" not in teddycloud.files

    def test_always_asks_first(self, client, runner):
        encode_and_assign_page(client, runner)

        row = self.row(client.get("/library").text, "/Lichterkinder/Halloween.taf")

        assert "data-confirm=" in row

    def test_a_taf_given_to_a_sticker_since_the_page_was_drawn_stays(self, client, runner,
                                                                     teddycloud):
        # The page showed it unused and so asked nothing about stickers; a
        # page left open must not delete what has since been put on one.
        encode_and_assign_page(client, runner)
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Lichterkinder/Halloween.taf"

        response = client.post("/library/delete",
                               data={"path": "/Lichterkinder/Halloween.taf", "players": ""},
                               follow_redirects=False)

        assert response.status_code == 409
        assert "/Lichterkinder/Halloween.taf" in teddycloud.files

    def test_a_taf_shown_as_played_is_deleted_when_asked(self, client, runner, teddycloud):
        encode_and_assign_page(client, runner)
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Lichterkinder/Halloween.taf"

        response = client.post("/library/delete", data={
            "path": "/Lichterkinder/Halloween.taf", "players": "7ab4ed0e500304e0"},
            follow_redirects=False)

        assert response.status_code == 303
        assert "/Lichterkinder/Halloween.taf" not in teddycloud.files

    def test_refuses_a_taf_the_library_does_not_have(self, client, teddycloud):
        response = client.post("/library/delete", data={"path": "/Weg/../Weg.taf"},
                               follow_redirects=False)

        assert response.status_code == 404

    def test_refuses_a_taf_an_encode_is_making(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)
        hold = __import__("threading").Event()
        runner.submit("encode", job.label, lambda progress: hold.wait(5))

        response = client.post("/library/delete", data={"path": job.label},
                               follow_redirects=False)
        hold.set()

        assert response.status_code == 409
        assert job.label in teddycloud.files

    def test_refuses_a_path_that_is_not_a_taf(self, client, teddycloud):
        teddycloud.files["/wichtig.json"] = b"{}"

        response = client.post("/library/delete", data={"path": "/wichtig.json"},
                               follow_redirects=False)

        assert response.status_code == 400
        assert "/wichtig.json" in teddycloud.files

    @pytest.mark.parametrize("error, answer", [
        ({"status": 403}, 502),
        # A command endpoint says no at status 200, in its body.
        ({"refused": True}, 502),
        ({}, 503),
    ])
    def test_teddycloud_failing_is_answered_as_everywhere(self, client, runner, error, answer):
        # Refusing and being away read differently, and the same on every page.
        from tonie_studio.teddycloud import TeddyCloudError

        encode_and_assign_page(client, runner)
        with patch("tonie_studio.pages.library.delete_taf",
                   side_effect=TeddyCloudError("no", **error)):
            response = client.post("/library/delete",
                                   data={"path": "/Lichterkinder/Halloween.taf"},
                                   follow_redirects=False)

        assert response.status_code == answer


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

        assert "Auf einen weiteren Sticker" in page
        assert 'name="target" value="deb4ed0e500304e0" required' in page


class TestDeletingASharedModel:
    def test_deleting_a_copy_keeps_model_and_audio_for_the_original(self, client, runner, teddycloud,
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
        assert teddycloud.tag_entries[copy]["source"] == ""
        assert teddycloud.tag_entries[original]["source"].startswith("lib://")
        assert model in playing_times(settings.data_dir)


class TestCoverRouteServesPicturesOnly:
    def test_a_name_that_is_not_a_picture_is_refused(self, client, teddycloud):
        teddycloud.images["x.html"] = b"<script>alert(1)</script>"

        assert client.get("/cover/custom/x.html").status_code == 400

    def test_a_picture_comes_with_its_type_and_no_guessing(self, client, teddycloud):
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        response = client.get("/cover/custom/TS-0001.jpg")

        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["x-content-type-options"] == "nosniff"


class TestRadioAsksBeforeReplacing:
    def test_only_a_sticker_that_plays_something_asks_first(self, client, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"

        page = client.get("/radio").text

        assert page.count("data-confirm=") == 1


class TestOnlyOwnCoversAreLinked:
    """A model whose picture is not in custom_img has no cover this app can serve."""

    def test_a_tonie_without_a_picture_of_ours_shows_none(self, client, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"].update(
            source="lib://Andere/Folge.taf", tonie_model="10000001")

        for path in ("/tonies", "/tonies/7ab4ed0e500304e0", "/radio", "/listening"):
            assert "/cover/custom/img_unknown.png" not in client.get(path).text, path

    def test_one_of_ours_still_shows_its_cover(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert "/cover/custom/TS-0001." in client.get("/tonies").text


class TestBoxSaysWhyItIsDisconnected:
    def test_a_silent_stream_is_reported_as_such(self, client, box):
        box._state.connected = False
        box._state.silent = True

        assert client.get("/api/box").json()["silent"] is True

    def test_a_connected_stream_is_never_silent(self, client, box):
        box._state.connected = True
        box._state.silent = True

        assert client.get("/api/box").json()["silent"] is False



class TestMoveOntoAnotherSticker:
    def test_the_page_offers_to_move_with_its_own_question(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/tonies/7ab4ed0e500304e0").text

        button = page[page.index('name="move"'):page.index("</button>", page.index('name="move"'))]
        assert "data-confirm=" in button

    def test_moving_leaves_the_tonie_on_the_new_sticker_only(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        played = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]

        response = client.post("/tonies/7ab4ed0e500304e0/copy",
                               data={"target": "deb4ed0e500304e0", "move": "1"},
                               follow_redirects=False)

        assert response.headers["location"] == "/tonies/deb4ed0e500304e0"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == played
        assert "7ab4ed0e500304e0" not in teddycloud.tag_entries
        assert 'id="tag-7ab4ed0e500304e0"' not in client.get("/tonies").text

    def test_copying_still_keeps_both(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        client.post("/tonies/7ab4ed0e500304e0/copy", data={"target": "deb4ed0e500304e0"})

        assert {"7ab4ed0e500304e0", "deb4ed0e500304e0"} <= set(teddycloud.tag_entries)


class TestStickerPicker:
    """Radio, podcasts, a tonie's copy and the walkthrough choose a sticker the same way."""

    def test_every_page_that_chooses_a_sticker_can_search_for_it(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        for path in ("/radio", "/podcasts", "/tonies/7ab4ed0e500304e0"):
            assert 'class="picker-find"' in client.get(path).text, path

    def test_finds_a_sticker_by_its_uid_with_or_without_colons(self, client):
        page = client.get("/radio").text

        assert "e0:04:03:50:0e:ed:b4:7a e00403500eedb47a" in page

    def test_the_first_free_sticker_is_chosen(self, client, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"

        page = client.get("/radio").text

        chosen = page[page.index(" checked>") - 200:page.index(" checked>")]
        assert 'value="deb4ed0e500304e0"' in chosen
