"""Tests for the pages of the album list, uploads, one album's page, its encode and its
assignment."""

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from web_helpers import (album_page_after_upload, encode_and_assign_page, finish_job,
                         first_album_id, page_script, publish_album, write_jpeg)

from tonie_studio.album import TrackTags
from tonie_studio.jobs import JobState
from tonie_studio.settings import Settings
from tonie_studio.teddycloud import TeddyCloudError
from tonie_studio.web import create_app


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

    def test_survives_an_archive_that_is_not_mounted(self, settings, teddycloud, runner, tags):
        settings.archive_dir = Path("/nowhere-at-all")
        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner)

        with TestClient(app) as client:
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
    def test_offers_karaoke_takes_unticked(self, client, settings, archive, teddycloud, runner, box):
        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        (folder / "04-karaoke.flac").write_bytes(b"\x04" * 8)

        with patch("tonie_studio.album._read_tags",
                   side_effect=lambda p: TrackTags(
                       title="Lied (Karaoke Version)" if "karaoke" in p.name else "Lied")):
            app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)
            with TestClient(app) as fresh:
                page = fresh.get(f"/album/{first_album_id(fresh)}").text

        assert page.count('name="selected"') == 4
        # "checked>" is the rendered attribute; the word alone also appears in
        # the script below the form.
        assert page.count("checked>") == 3

    def test_offers_a_way_to_drop_them_all_at_once(self, client):
        assert 'data-pick="all"' in client.get(f"/album/{first_album_id(client)}").text


class TestOccupiedName:
    def test_asks_instead_of_failing_inside_the_job(self, client, teddycloud):
        # The old behaviour sent you to a progress page that then showed an
        # error. An occupied name is a question for the form.
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
        before = client.get("/").text.count('class="album"')

        client.post("/upload", files=[("files", ("01-neu.mp3", b"\x01" * 8, "audio/mpeg"))],
                    follow_redirects=False)

        assert client.get("/").text.count('class="album"') == before + 1

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

    def test_offers_a_folder_picker(self, client):
        assert "webkitdirectory" in client.get("/").text

    def test_lists_the_upload_next_to_the_archive(self, client):
        client.post("/upload", files=[("files", ("01-eins.mp3", b"\x01" * 8, "audio/mpeg"))],
                    follow_redirects=False)

        # Uploads lead the list: what was just added is what one is looking for.
        page = client.get("/").text
        assert page.count('class="album"') == 2

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

    def test_the_list_works_without_teddycloud(self, settings, runner, tags, box):
        class Offline:
            def __getattr__(self, name):
                def refuse(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud is away")
                return refuse

        app = create_app(settings, client_factory=Offline, runner=runner, watcher=box)
        with TestClient(app) as offline:
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

        assert 'name="replace" value="*"' in page


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

    def test_an_error_nobody_described_is_shown_as_it_came(self, client, runner):
        def fails(progress):
            raise RuntimeError("ffmpeg died")

        job_id = runner.submit("encode", "/X.taf", fails)
        assert runner.get(job_id).done.wait(5)

        assert client.get(f"/api/job/{job_id}").json()["error"] == "ffmpeg died"


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
