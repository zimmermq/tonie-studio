"""Tests for the pages of the album list, uploads, one album's page, its encode and its
assignment."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from web_helpers import (album_page_after_upload, checked_ruid, encode_and_assign_page, finish_job,
                         eventually, first_album_id, page_script, publish_album, upload_series,
                         write_jpeg)

from tonie_studio.album import TrackTags
from tonie_studio.jobs import JobState
from tonie_studio.settings import Settings
from tonie_studio.teddycloud import TeddyCloudError
from tonie_studio.web import create_app


class TestAnAlbumWithATonie:
    # The batch page would only say the album is skipped; what one wants from
    # such a row is the tonie.
    def test_its_row_leads_to_the_tonie(self, client, settings):
        from tonie_studio.albumnotes import remember_made

        identifier = first_album_id(client)
        remember_made(settings.data_dir, identifier, "TS-0012")

        page = client.get("/").text

        assert 'href="/model/TS-0012"' in page
        assert f'href="/batch?ids={identifier}"' not in page

    def test_the_tonie_is_the_sticker_it_is_on(self, client, teddycloud):
        teddycloud.add_tag("e00403500e1234ab", source="lib://a.taf", tonie_model="TS-0012")

        response = client.get("/model/TS-0012", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/tonies/e00403500e1234ab"

    def test_without_a_sticker_its_cover_is_offered(self, client):
        response = client.get("/model/TS-0012", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/sheet?model=TS-0012"


class TestIndex:
    def test_shows_the_readable_title_not_the_scene_folder(self, client):
        page = client.get("/").text

        assert ">Halloween</strong>" in page
        assert "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN" not in page.split("data-search=")[0]

    def test_keeps_the_folder_name_searchable(self, client):
        # The readable title is what one reads, but the scene folder name is
        # what one might type, so both have to be in the search haystack.
        page = client.get("/").text

        haystack = page.split('data-search="')[1].split('"')[0]
        assert "halloween" in haystack and "lichterkinder" in haystack

    def test_says_how_many_tracks_it_has(self, client):
        assert "3 Tracks" in client.get("/").text

    def test_offers_a_search_field(self, client):
        assert 'id="search"' in client.get("/").text

    def test_serves_a_preview_far_smaller_than_the_cover(self, client, archive):
        # Sixteen full covers would be megabytes of JPEG for pictures the size
        # of a stamp.
        from PIL import Image

        cover = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN" / "cover.jpg"
        Image.new("RGB", (1200, 1200), (10, 120, 200)).save(cover, format="JPEG")
        identifier = first_album_id(client)

        full = client.get(f"/album/{identifier}/cover")
        preview = client.get(f"/album/{identifier}/cover?size=64")

        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/jpeg"
        assert len(preview.content) < len(full.content) / 4

    def test_survives_an_archive_that_is_not_mounted(self, make_site, settings):
        settings.archive_dir = Path("/nowhere-at-all")
        client = make_site()
        assert client.get("/").status_code == 200


class TestAlbumPage:
    def test_prefills_series_and_episode_from_the_folder_name(self, client):
        page = client.get(f"/album/{first_album_id(client)}").text

        assert 'name="series" value="Lichterkinder"' in page
        assert 'name="episode" value="Halloween"' in page

    def test_lists_every_track_in_order(self, client):
        page = client.get(f"/album/{first_album_id(client)}").text

        assert page.index("Kleine monster") < page.index("Suesses") < page.index("Geister")

    def test_serves_the_cover(self, client):
        response = client.get(f"/album/{first_album_id(client)}/cover")

        assert response.status_code == 200
        assert response.content.startswith(b"\xff\xd8\xff")

    def test_refuses_an_id_that_names_nothing(self, client):
        assert client.get("/album/a0000deadbeef").status_code == 404


class TestTrackSelection:
    def test_offers_karaoke_takes_unticked(self, make_site, client, archive):
        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        (folder / "04-karaoke.flac").write_bytes(b"\x04" * 8)

        with patch("tonie_studio.album._read_tags",
                   side_effect=lambda p: TrackTags(
                       title="Lied (Karaoke Version)" if "karaoke" in p.name else "Lied")):
            fresh = make_site()
            page = fresh.get(f"/album/{first_album_id(fresh)}").text

        assert page.count('name="selected"') == 4
        # "checked>" is the rendered attribute; the word alone also appears in
        # the script below the form.
        assert page.count("checked>") == 3

    def test_offers_a_way_to_drop_them_all_at_once(self, client):
        assert 'data-pick="all"' in client.get(f"/album/{first_album_id(client)}").text


class TestOccupiedName:
    def test_asks_instead_of_failing_inside_the_job(self, client, teddycloud):
        # An occupied name is a question for the form, not an error minutes
        # later on a progress page.
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"older"

        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        assert response.status_code == 200
        assert "Dort liegt schon ein Taf" in response.text
        assert teddycloud.files["/Lichterkinder/Halloween.taf"] == b"older"

    def test_replaces_it_when_that_is_what_was_asked(self, client, teddycloud, runner):
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"older"

        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1", "selected": ["1"], "titles": ["a"],
            "replace": "/Lichterkinder/Halloween.taf",
        }, follow_redirects=False)

        assert response.status_code == 303
        finish_job(runner, response)
        assert teddycloud.files["/Lichterkinder/Halloween.taf"] != b"older"


class TestConflictKeepsWhatWasEntered:
    """The page that asks about an occupied name is the form as it was sent."""

    def _conflict(self, client, teddycloud, **overrides):
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Mein Name.taf"] = b"older"
        data = {"series": "Lichterkinder", "episode": "Mein Name", "order": "3,1,2",
                "selected": ["3", "1"], "titles": ["Drei neu", "Eins neu", "Zwei neu"],
                "cover_zoom": "1.4", "cover_x": "0.25", "cover_y": "-0.5"}
        data.update(overrides)
        return client.post(f"/album/{first_album_id(client)}/encode", data=data,
                           follow_redirects=False)

    def test_names_order_and_choice_survive(self, client, teddycloud):
        page = self._conflict(client, teddycloud).text

        assert 'name="episode" value="Mein Name"' in page
        assert 'name="order" id="order" value="3,1,2"' in page
        assert page.index('value="Drei neu"') < page.index('value="Eins neu"') \
            < page.index('value="Zwei neu"')
        assert page.count("checked>") == 2

    def test_the_cover_crop_survives(self, client, teddycloud):
        page = self._conflict(client, teddycloud).text

        assert 'id="cx" value="0.25"' in page and 'id="cy" value="-0.5"' in page
        assert 'step="0.02" value="1.4"' in page

    def test_replace_names_exactly_the_tafs_that_were_shown(self, client, teddycloud):
        page = self._conflict(client, teddycloud).text

        assert 'name="replace" value="/Lichterkinder/Mein Name.taf"' in page

    def test_an_answer_about_other_tafs_replaces_nothing(self, client, teddycloud):
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"older too"

        # A blanket "all of them" must not reach a taf the page never named.
        response = self._conflict(client, teddycloud, episode="Halloween", replace="*")

        assert response.status_code == 200
        assert teddycloud.files["/Lichterkinder/Halloween.taf"] == b"older too"


class TestEncoding:
    def test_encodes_the_selected_tracks_into_one_taf(self, client, teddycloud, runner):
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1,2,3", "selected": ["1", "2", "3"],
            "titles": ["Kleine Monster", "Suesses", "Geister"],
        }, follow_redirects=False)

        job = finish_job(runner, response)
        assert job.state == JobState.DONE
        assert "/Lichterkinder/Halloween.taf" in teddycloud.files

    def test_encodes_in_the_order_the_page_submitted(self, client, teddycloud, runner):
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Umsortiert",
            "order": "3,1,2", "selected": ["1", "2", "3"],
            "titles": ["Geister", "Kleine Monster", "Suesses"],
        }, follow_redirects=False)

        finish_job(runner, response)
        assert teddycloud.files["/Umsortiert.taf"] == bytes([3]) * 8 + bytes([1]) * 8 + bytes([2]) * 8

    def test_leaves_out_what_was_unticked(self, client, teddycloud, runner):
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Nur zwei",
            "order": "1,2,3", "selected": ["1", "3"],
            "titles": ["Kleine Monster", "Suesses", "Geister"],
        }, follow_redirects=False)

        finish_job(runner, response)
        assert teddycloud.files["/Nur zwei.taf"] == bytes([1]) * 8 + bytes([3]) * 8

    def test_keeps_the_names_aligned_to_the_reordered_tracks(self, client, runner):
        # The titles are submitted in row order, so the second name belongs to
        # whatever track ended up second, not to track two.
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Namen",
            "order": "3,1", "selected": ["1", "3"],
            "titles": ["Geister", "Kleine Monster"],
        }, follow_redirects=False)

        assert finish_job(runner, response).result.tracks == ["Geister", "Kleine Monster"]

    def test_refuses_when_nothing_is_ticked(self, client):
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Leer", "order": "1,2,3", "titles": ["a", "b", "c"],
        }, follow_redirects=False)

        assert response.status_code == 400

    def test_refuses_an_episode_that_is_only_whitespace(self, client):
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "Lichterkinder", "episode": "   ",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        assert response.status_code == 400

    def test_reports_a_failed_encode_instead_of_swallowing_it(self, client, teddycloud, runner):
        teddycloud.encode_fails = "ffmpeg died"
        identifier = first_album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Kaputt",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        job = finish_job(runner, response)
        assert job.state == JobState.FAILED
        assert "ffmpeg died" in job.error


class TestJobPage:
    def test_reports_the_state_as_json(self, client, runner):
        identifier = first_album_id(client)
        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Status", "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)
        job = finish_job(runner, response)

        status = client.get(f"/api/job/{job.id}").json()

        assert status["state"] == "done"
        assert status["percent"] == 100
        assert status["target"] == "/Status.taf"

    def test_a_finished_job_names_no_step_beside_its_state(self, client, runner):
        # A job keeps the step it was last at; "encoding" beside "done" is
        # two answers to one question.
        job, _page = encode_and_assign_page(client, runner)
        assert job.step

        page = client.get(f"/job/{job.id}").text
        home = client.get("/").text

        assert page.split('id="detail"', 1)[1].split("</p>", 1)[0] == ' role="status">'
        assert "wird encodiert" not in home and "Minuten" not in home
        assert "fertig - " not in home

    def test_refuses_an_unknown_job(self, client):
        assert client.get("/api/job/nope").status_code == 404
        assert client.get("/job/nope").status_code == 404


class TestCatalogueCache:
    def test_scans_the_archive_once_for_a_whole_page_load(self, client, monkeypatch):
        # One page load is one listing plus a preview request per album. Over a
        # network share each of those walks costs more than rendering does.
        catalogue = client.app.state.catalogue
        catalogue.forget()
        scans = []
        original = catalogue._scan
        monkeypatch.setattr(catalogue, "_scan",
                            lambda *args, **kwargs: scans.append(1) or original(*args, **kwargs))

        identifier = first_album_id(client)
        for _ in range(5):
            client.get(f"/album/{identifier}/cover?size=64")

        assert len(scans) <= 2  # one described listing, one cheap lookup

    def test_a_new_upload_shows_up_at_once(self, client):
        before = client.get("/").text.count('class="item"')

        client.post("/upload", files=[("files", ("01-neu.mp3", b"\x01" * 8, "audio/mpeg"))],
                    follow_redirects=False)

        assert client.get("/").text.count('class="item"') == before + 1

    def test_two_archives_do_not_share_a_cache(self, settings, teddycloud, runner, tags, tmp_path):
        other = tmp_path / "other-archive"
        (other / "Anderes Album").mkdir(parents=True)
        (other / "Anderes Album" / "01-x.flac").write_bytes(b"")

        first = create_app(settings, client_factory=lambda: teddycloud, runner=runner)
        elsewhere = Settings(teddycloud_url="http://teddycloud.test",
                             archive_dir=other, data_dir=tmp_path / "other-data")
        second = create_app(elsewhere, client_factory=lambda: teddycloud, runner=runner)

        with TestClient(first) as one, TestClient(second) as two:
            assert "Halloween" in one.get("/").text
            assert "Anderes Album" in two.get("/").text


class TestUpload:
    def test_takes_the_files_and_opens_them_as_an_album(self, client):
        response = client.post("/upload", files=[
            ("files", ("01-eins.mp3", b"\x01" * 8, "audio/mpeg")),
            ("files", ("02-zwei.mp3", b"\x02" * 8, "audio/mpeg")),
            ("files", ("cover.jpg", b"\xff\xd8\xff", "image/jpeg")),
        ], follow_redirects=False)

        assert response.status_code == 303
        page = album_page_after_upload(client, response)
        assert "Eins" in page and "Zwei" in page

    def test_a_folder_of_more_than_a_thousand_files_is_taken(self, client):
        # A series folder with its notes and pictures soon passes the form
        # parser's default of a thousand files.
        files = [("files", (f"Serie/Folge 1/{index:04}.txt", b"x", "text/plain"))
                 for index in range(1000)]
        files.append(("files", ("Serie/Folge 1/01-eins.mp3", b"\x01" * 8, "audio/mpeg")))

        response = client.post("/upload", files=files, follow_redirects=False)

        assert response.status_code == 303

    def test_the_page_sending_by_hand_gets_where_to_go_rather_than_a_redirect(self, client):
        # Followed by the request itself, the redirect would plan the batch
        # once for nothing before the page opens it.
        response = client.post("/upload", headers={"X-Upload": "1"}, follow_redirects=False,
                               files=[("files", ("01-eins.mp3", b"\x01" * 8, "audio/mpeg"))])

        assert response.status_code == 200
        assert response.json()["next"].startswith("/batch?ids=")

    def test_a_full_disk_while_the_form_is_read_says_so(self, client, monkeypatch):
        import errno

        from starlette.requests import Request

        async def full(self, *args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(Request, "form", full)

        response = client.post("/upload", files=[("files", ("01.mp3", b"x", "audio/mpeg"))])

        assert response.status_code == 507

    def test_an_upload_larger_than_the_free_space_is_refused_before_it_is_read(
            self, client, monkeypatch):
        import shutil
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**9, 10**9 - 10, 10))

        response = client.post("/upload", files=[("files", ("01.mp3", b"x" * 4096, "audio/mpeg"))])

        assert response.status_code == 507

    def test_files_are_held_in_memory_only_briefly(self):
        # Up to MOST_UPLOAD_PARTS files, each held in memory up to this size
        # before it goes to disk: the default of a megabyte would be gigabytes.
        from starlette.formparsers import MultiPartParser

        import tonie_studio.pages.common  # noqa: F401 - setting it is its import

        assert MultiPartParser.spool_max_size <= 64 * 1024

    def test_a_broken_form_is_not_called_too_many_files(self, client):
        from tonie_studio.i18n import Translator

        response = client.post("/upload", content=b"--x\r\nbroken",
                               headers={"Content-Type": "multipart/form-data; boundary=x"})

        assert response.status_code == 400
        assert Translator("de")("error.upload_too_many", most=20000) not in response.text

    def test_too_many_files_are_refused_in_words(self, client, monkeypatch):
        from tonie_studio.i18n import Translator

        monkeypatch.setattr("tonie_studio.pages.common.MOST_UPLOAD_PARTS", 2)
        files = [("files", (f"A/0{index}.mp3", b"x", "audio/mpeg")) for index in range(3)]

        response = client.post("/upload", files=files, follow_redirects=False)

        assert response.status_code == 400
        assert Translator("de")("error.upload_too_many", most=2) in response.text

    def test_refuses_a_upload_without_audio(self, client):
        response = client.post("/upload", files=[
            ("files", ("notes.txt", b"hello", "text/plain")),
        ], follow_redirects=False)

        assert response.status_code == 400

    def test_flattens_the_paths_a_folder_upload_sends(self, client):
        # Picking a folder makes the browser send names like
        # "Album/01-track.mp3"; teddyCloud rejects a separator in an upload
        # name, and a subfolder would put the tracks out of one taf's reach.
        response = client.post("/upload", files=[
            ("files", ("Lichterkinder-Album/01-eins.mp3", b"\x01" * 8, "audio/mpeg")),
            ("files", ("Lichterkinder-Album/02-zwei.mp3", b"\x02" * 8, "audio/mpeg")),
        ], follow_redirects=False)

        page = album_page_after_upload(client, response)
        assert "Eins" in page and "Zwei" in page

    @pytest.mark.parametrize("names, title", [
        (["Gute_Nacht/01-a.mp3", "Gute_Nacht/02-b.mp3"], "Gute Nacht"),
        (["02-zwei.mp3", "01-eins.mp3"], "Eins"),
    ])
    def test_an_untagged_upload_is_called_what_was_picked_everywhere(self, client, names, title):
        response = client.post("/upload", files=[
            ("files", (name, b"\x01" * 8, "audio/mpeg")) for name in names],
            follow_redirects=False)

        batch = client.get(response.headers["location"]).text
        listing = client.get("/").text
        assert f'<strong class="truncate">{title}' in listing
        assert f'name="episodes" value="{title}"' in batch

    def test_offers_a_folder_picker(self, client):
        assert "webkitdirectory" in client.get("/").text

    def test_lists_the_upload_next_to_the_archive(self, client):
        client.post("/upload", files=[("files", ("01-eins.mp3", b"\x01" * 8, "audio/mpeg"))],
                    follow_redirects=False)

        # Uploads lead the list: what was just added is what one is looking for.
        page = client.get("/").text
        assert page.count('class="item"') == 2

    def test_hides_the_unstylable_file_input_behind_a_label(self, client):
        page = client.get("/").text

        assert '<label class="button">' in page
        assert 'type="file"' in page and "hidden>" in page


class TestAssignPage:
    def test_offers_the_tags_the_box_knows(self, client, runner):
        _job, page = encode_and_assign_page(client, runner)

        assert page.status_code == 200
        assert "7ab4ed0e500304e0" in page.text and "deb4ed0e500304e0" in page.text

    def test_prefills_what_was_typed_on_the_album_page(self, client, runner):
        _job, page = encode_and_assign_page(client, runner)

        assert 'name="series" value="Lichterkinder"' in page.text
        assert 'name="episode" value="Halloween"' in page.text

    def test_says_a_fresh_sticker_has_to_meet_the_box_first(self, client, runner):
        _job, page = encode_and_assign_page(client, runner)

        assert "Toniebox" in page.text

    def test_reports_a_sticker_the_box_does_not_know(self, client, runner, box):
        # A blank NFC sticker reaches teddyCloud as a tag it has never seen.
        box.handle("TagInvalid", "E00403500EEDFFFF")

        assert client.get("/api/box").json()["unknown_ruid"] == "ffffed0e500304e0"

    def test_preselects_the_tag_that_was_just_held_against_the_box(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=deb4ed0e500304e0").text

        marked = page.split('value="deb4ed0e500304e0"')[1].split(">")[0]
        assert "checked" in marked
        assert "gerade aufgelegt" in page

    def test_keeps_what_was_typed_when_drawn_again_for_a_new_sticker(self, client, runner):
        # The page reloads itself once a sticker is held against the box, and
        # sends along what had been typed so it is not lost.
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign", params={
            "new": "deb4ed0e500304e0", "series": "Eigene Reihe", "episode": "Folge <1>",
            "category": "audio-book"}).text

        assert 'name="series" value="Eigene Reihe"' in page
        assert 'name="episode" value="Folge &lt;1&gt;"' in page
        assert '<option value="audio-book" selected>' in page

    def test_an_unknown_category_in_the_address_selects_none(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign", params={"category": "nonsense"}).text

        assert " selected>" not in page.split('name="category"')[1].split("</select>")[0]

    def test_ignores_a_stale_link_to_a_tag_that_is_not_there(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=ffffffffffffffff").text

        assert "gerade aufgelegt" not in page

    def test_refuses_before_the_encode_finished(self, client, runner, teddycloud):
        teddycloud.encode_fails = "ffmpeg died"
        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "", "episode": "Kaputt", "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)
        job = finish_job(runner, response)

        assert client.get(f"/job/{job.id}/assign").status_code == 409


class TestAssign:
    def test_points_the_chosen_tag_at_the_new_taf(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)

        response = client.post(f"/job/{job.id}/assign", data={
            "ruid": "deb4ed0e500304e0", "series": "Lichterkinder",
            "episode": "Halloween", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 303
        entry = teddycloud.tag_entries["deb4ed0e500304e0"]
        assert entry["source"] == "lib://Lichterkinder/Halloween.taf"
        assert entry["nocloud"] is True

    def test_the_model_speaks_the_language_chosen(self, client, runner, teddycloud):
        job, page = encode_and_assign_page(client, runner)

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
            "language": "en"}, follow_redirects=False)

        assert 'name="language"' in page.text
        assert teddycloud.custom_tonies()[0]["language"] == "en-us"

    def test_without_a_choice_the_model_speaks_german(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
            "language": "nonsense"}, follow_redirects=False)

        assert teddycloud.custom_tonies()[0]["language"] == "de-de"

    def test_carries_the_edited_track_names_into_the_model(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner, titles=["Erster", "Zweiter"])

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        assert teddycloud.custom_tonies()[0]["tracks"] == ["Erster", "Zweiter"]

    def test_takes_the_cover_from_the_album_folder(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        # What reaches teddyCloud is the cover already cut for the round
        # sticker, not the file from the album folder.
        assert len(teddycloud.images) == 1
        assert next(iter(teddycloud.images.values())).startswith(b"\xff\xd8\xff")

    def test_refuses_a_tag_teddycloud_does_not_know(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        response = client.post(f"/job/{job.id}/assign", data={
            "ruid": "ffffffffffffffff", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 400


class TestCoverCrop:
    def test_renders_the_cover_as_the_sticker_will_cut_it(self, client):
        response = client.get(f"/album/{first_album_id(client)}/crop?zoom=1.5&x=0.5&size=64")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_zooming_changes_the_picture(self, client, archive):
        from PIL import Image

        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        image = Image.new("RGB", (300, 300), (255, 0, 0))
        image.paste(Image.new("RGB", (60, 60), (0, 0, 255)), (120, 120))
        image.save(folder / "cover.jpg", format="JPEG")
        identifier = first_album_id(client)

        wide = client.get(f"/album/{identifier}/crop?zoom=1&size=48").content
        close = client.get(f"/album/{identifier}/crop?zoom=2.5&size=48").content

        assert wide != close

    def test_offers_every_image_in_the_folder_to_choose_from(self, client, archive):
        # The reader collects them all and ranks them; the page used to show
        # only the first, which for a scene rip is a photo of the disc.
        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        write_jpeg(folder / "back.jpg", colour=(200, 30, 30))

        page = client.get(f"/album/{first_album_id(client)}").text

        assert page.count('class="pick') == 2

    def test_the_chosen_crop_is_what_reaches_teddycloud(self, client, runner, teddycloud):
        job, _page = encode_and_assign_page(client, runner)
        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        import io as _io

        from PIL import Image

        stored = next(iter(teddycloud.images.values()))
        with Image.open(_io.BytesIO(stored)) as image:
            assert image.size[0] == image.size[1]  # square, ready for the circle


class TestDuplicateAudio:
    def test_warns_before_a_second_model_for_the_same_recording(self, client, runner, teddycloud):
        # Two models with the same audio_id and hash fight each other for
        # teddyCloud's automatic matching.
        publish_album(client, runner, teddycloud)

        _job, page = encode_and_assign_page(client, runner, episode="Halloween zwei")

        assert "Dieses Audio gibt es schon" in page.text
        assert "TS-0001" in page.text

    def test_says_nothing_when_the_recording_is_new(self, client, runner):
        _job, page = encode_and_assign_page(client, runner)

        assert "Dieses Audio gibt es schon" not in page.text


class TestAssignDoesNotLoop:
    def test_an_old_unknown_tag_is_not_reported_as_fresh(self, client, box):
        # The page reloads when a tag teddyCloud does not know turns up. If the
        # watcher remembers one forever, that reload never stops and the page
        # jumps back to the top every few seconds.
        from datetime import datetime, timedelta, timezone

        box.handle("TagInvalid", "E00403500EEDFFFF")
        box._state.unknown_since = datetime.now(timezone.utc) - timedelta(hours=2)

        assert client.get("/api/box").json()["unknown_ruid"] is None

    def test_the_page_remembers_what_it_already_reloaded_for(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=ffffed0e500304e0").text

        assert "alreadyTried" in client.get(page_script(page, "assign.js")).text
        assert "kennt ihn nicht" in page


class TestAlbumMarkers:
    def test_an_album_made_into_a_tonie_says_so(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert "hat einen Tonie" in client.get("/").text

    def test_deleting_the_tonie_takes_the_marker_away(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["tonie_model"])
        # The name match would find it again; this is about the record.
        teddycloud.custom_models[0]["episodes"] = "etwas ganz anderes"

        client.post(f"/tonies/{ruid}/delete")

        assert "hat einen Tonie" not in client.get("/").text

    def test_nothing_is_new_on_the_first_look(self, client):
        assert 'class="pill new"' not in client.get("/").text

    def test_an_album_copied_in_later_is_new(self, client, archive):
        client.get("/")
        later = archive / "Lichterkinder-Schlaflieder-2021"
        later.mkdir()
        (later / "01-guter_mond.flac").write_bytes(b"x")
        client.app.state.catalogue.forget()

        page = client.get("/").text

        assert page.count('class="pill new"') == 1

    def test_the_list_works_without_teddycloud(self, make_site):
        class Offline:
            def __getattr__(self, name):
                def refuse(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud is away")
                return refuse

        offline = make_site(client_factory=Offline)
        assert offline.get("/").status_code == 200

    def test_job_states_read_in_german(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/").text

        assert "fertig" in page and ">done<" not in page


class TestParts:
    def _encode(self, client, **overrides):
        data = {"series": "Lichterkinder", "episode": "Halloween",
                "order": "1,2,3", "selected": ["1", "2", "3"],
                "titles": ["Eins", "Zwei", "Drei"]}
        data.update(overrides)
        return client.post(f"/album/{first_album_id(client)}/encode", data=data,
                           follow_redirects=False)

    def test_one_part_is_the_old_single_encode(self, client, runner, teddycloud):
        job = finish_job(runner, self._encode(client))

        assert job.result.episode == "Halloween"

    def test_parts_become_separate_tafs(self, client, runner, teddycloud):
        response = self._encode(client, parts="2")
        page = client.get(response.headers["location"]).text
        for job in runner.all():
            assert job.done.wait(5)

        targets = sorted(target for target, _sources in teddycloud.encoded)
        assert len(targets) == 2
        assert all("Teil" in target for target in targets)
        assert "In Teilen" in page

    def test_every_track_lands_in_exactly_one_part(self, client, runner, teddycloud):
        self._encode(client, parts="2")
        for job in runner.all():
            assert job.done.wait(5)

        names = [name for job in runner.all() for name in job.result.tracks]
        assert sorted(names) == ["Drei", "Eins", "Zwei"]

    def test_too_many_tracks_for_one_taf_is_refused_with_the_reason(self, client, monkeypatch):
        # teddyCloud would take them and quietly glue the rest onto the last
        # chapter; the form has to stop it first.
        import tonie_studio.parts as parts

        monkeypatch.setattr(parts, "MOST_CHAPTERS", 2)

        response = self._encode(client, parts="1")

        assert response.status_code == 400
        assert "Teile" in response.text

    def test_a_part_already_there_asks_once_for_all_of_them(self, client, runner, teddycloud):
        from tonie_studio.encode import library_path

        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files[library_path("Lichterkinder", "Halloween - Teil 1 von 2")] = b"old"

        page = self._encode(client, parts="2").text

        assert f'name="replace" value="{library_path("Lichterkinder", "Halloween - Teil 1 von 2")}"' in page


class TestHomePageWithoutTeddyCloud:
    def test_the_list_does_not_wait_for_a_teddycloud_that_does_not_answer(
            self, settings, runner, tags, box):
        # Refusing at once is the easy case; a server that never answers holds
        # a request for the whole timeout, three times over with retries.
        import threading
        import time

        released = threading.Event()

        class Silent:
            def custom_tonies(self):
                released.wait(10)
                return []

            def __getattr__(self, name):
                def refuse(*args, **kwargs):
                    raise TeddyCloudError("away")
                return refuse

        app = create_app(settings, client_factory=Silent, runner=runner, watcher=box)
        try:
            with TestClient(app) as client:
                started = time.monotonic()
                assert client.get("/").status_code == 200
                assert time.monotonic() - started < 2
        finally:
            released.set()


class TestJobErrorsInTheReadersLanguage:
    def test_a_failed_job_says_why_in_the_readers_language(self, client, runner):
        from tonie_studio.encode import EncodeError

        def fails(progress):
            raise EncodeError("encode.no_tracks")

        job_id = runner.submit("encode", "/X.taf", fails)
        assert runner.get(job_id).done.wait(5)
        client.cookies.set("lang", "de")

        assert client.get(f"/api/job/{job_id}").json()["error"] == "Bitte mindestens einen Track auswählen."
        assert "Bitte mindestens einen Track" in client.get(f"/job/{job_id}").text

    def test_an_error_nobody_described_is_framed_as_a_failure(self, client, runner):
        # Raw text from ffmpeg or teddyCloud says what broke, not that the job did.
        def fails(progress):
            raise RuntimeError("ffmpeg died")

        job_id = runner.submit("encode", "/X.taf", fails)
        assert runner.get(job_id).done.wait(5)

        assert client.get(f"/api/job/{job_id}").json()["error"] == "Das ging nicht: ffmpeg died"


class TestGroupStatus:
    def test_answers_every_job_of_a_group_at_once(self, client, runner):
        response = client.post(f"/album/{first_album_id(client)}/encode", data={
            "series": "S", "episode": "E", "order": "1,2,3", "selected": ["1", "2", "3"],
            "titles": ["a", "b", "c"], "parts": "2",
        }, follow_redirects=False)
        group = response.headers["location"].rsplit("/", 1)[1]

        answer = client.get(f"/api/group/{group}").json()

        assert len(answer["jobs"]) == 2
        assert {job["id"] for job in answer["jobs"]} == {job.id for job in runner.all()}

    def test_an_unknown_group_is_refused(self, client):
        assert client.get("/api/group/nope").status_code == 404


class TestAssignNeverPicksAnOccupiedSticker:
    def test_with_no_free_sticker_nothing_is_preselected(self, client, runner, teddycloud):
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign").text

        assert checked_ruid(page) is None

    def test_an_occupied_sticker_asks_before_it_is_overwritten(self, client, runner, teddycloud):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
        job, _page = encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign").text
        radio = page[page.index('value="7ab4ed0e500304e0"'):]
        radio = radio[:radio.index(">")]

        assert "data-confirm=" in radio


class TestCoverIndexOutOfRange:
    def test_a_negative_index_is_no_cover(self, client):
        identifier = first_album_id(client)

        assert client.get(f"/album/{identifier}/cover", params={"index": -1}).status_code == 404
        assert client.get(f"/album/{identifier}/cover", params={"index": -5}).status_code == 404
        assert client.get(f"/album/{identifier}/crop", params={"index": -5}).status_code == 404


class TestTickingAndFilters:
    """Albums are ticked for a batch over what the search and the filters leave in view."""

    def test_one_album_needs_no_ticking_all(self, client):
        assert 'id="tickshown"' not in client.get("/").text

    def test_several_albums_can_be_ticked_and_filtered(self, client):
        upload_series(client, episodes=2)

        page = client.get("/").text

        assert 'id="tickshown"' in page and 'id="untick"' in page
        assert 'data-filter="notonie"' in page

    def test_each_row_says_whether_it_has_a_tonie_and_is_new(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        upload_series(client, episodes=2)

        page = client.get("/").text

        assert page.count('data-tonie="yes"') == 1
        assert page.count('data-tonie="no"') == 2
        assert 'data-new="' in page
        assert page.count('data-tags="notonie') == 2

    def test_the_page_says_from_how_many_albums_setting_up_is_slow(self, client):
        from tonie_studio.pages.albums import SLOW_PICK

        assert f'"slowPick": {SLOW_PICK}' in client.get("/").text


class TestTonieRecognisedByItsTracks:
    """A tonie whose name says nothing about its album is still known by its tracks."""

    def _tonie_under_another_name(self, client, runner, teddycloud, settings):
        from tonie_studio.album import read_album

        album = read_album(next(settings.archive_dir.iterdir()))
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_models[0]
        model["tracks"] = [track.title for track in album.tracks]
        model["series"], model["episodes"] = "Ganz", "Anders"
        (settings.data_dir / "albums-made.json").unlink()

    def test_the_batch_page_shows_it_without_writing_it_down(self, client, runner, teddycloud,
                                                              settings):
        # Opening a page is no decision: two albums with the same track names
        # match one tonie, and a guess written down here would mark the wrong
        # one done for good. It is recorded once a tonie is made from it.
        from tonie_studio.albumnotes import made

        self._tonie_under_another_name(client, runner, teddycloud, settings)

        batch = client.get("/batch", params={"ids": first_album_id(client)}).text

        assert "hat schon einen Tonie" in batch
        assert made(settings.data_dir) == {}
        assert "hat einen Tonie" not in client.get("/").text

    def test_a_guess_by_name_is_not_written_down(self, client, runner, teddycloud, settings):
        from tonie_studio.albumnotes import made

        publish_album(client, runner, teddycloud)
        (settings.data_dir / "albums-made.json").unlink()

        client.get("/batch", params={"ids": first_album_id(client)})

        assert made(settings.data_dir) == {}


class TestNotInTheLibrary:
    """Each row says whether its album is already encoded, from a copy of the library."""

    def _rows(self, client):
        import re

        page = client.get("/").text
        return dict((ident, state) for state, ident in re.findall(
            r'data-library="(\w+)"[^>]*>\s*<input type="checkbox" name="ids" value="(\w+)"', page))

    def test_an_encoded_album_is_in_it_and_the_others_are_not(self, make_site, settings, runner, teddycloud,
                                                             box, tags, monkeypatch):
        from tonie_studio import web
        from tonie_studio.planning import expected_targets

        monkeypatch.setattr(web, "LIBRARY_TTL", 0)
        client = make_site()
        upload_series(client, episodes=2)
        uploads = [(ident, summary) for ident, summary, _origin
                   in client.app.state.catalogue.albums() if ident.startswith("u")]
        (encoded, summary), (missing, _summary) = uploads
        for target in expected_targets(summary, settings.archive_dir, settings.upload_dir):
            folder = target.rsplit("/", 1)[0] or "/"
            if folder not in teddycloud.dirs:
                teddycloud.create_dir(folder)
            teddycloud.files[target] = b"taf"
            teddycloud.taf_headers[target] = {"valid": True}

        eventually(lambda: self._rows(client).get(encoded) == "yes")
        assert self._rows(client)[missing] == "no"

    def test_without_a_copy_of_the_library_nothing_is_claimed(self, make_site, teddycloud):
        from tonie_studio.teddycloud import TeddyCloudError

        def unreachable(*args, **kwargs):
            raise TeddyCloudError("no answer")

        # Before the app starts, which is when it first asks for the library.
        teddycloud.list_dir = unreachable
        client = make_site()
        assert set(self._rows(client).values()) == {"unknown"}
        assert "notinlibrary" not in client.get("/").text

    def test_offers_the_filter(self, client):
        upload_series(client, episodes=2)

        assert 'data-filter="notinlibrary"' in client.get("/").text
