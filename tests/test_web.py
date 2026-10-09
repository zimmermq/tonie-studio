"""Tests for the web layer.

The teddyCloud client is the fake, so an encode started from a page really
runs: it uploads, encodes and cleans up against the in-memory server, and the
test can look at what arrived.
"""

import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from fake_teddycloud import FakeTeddyCloud
from tonie_studio.teddycloud import TeddyCloudError
from tonie_studio.album import TrackTags
from tonie_studio.boxstate import BoxWatcher
from tonie_studio.jobs import JobRunner, JobState
from tonie_studio.settings import Settings
from tonie_studio.web import create_app


@pytest.fixture
def tags():
    """No tags anywhere; names come from the file names."""
    with patch("tonie_studio.album._read_tags", return_value=TrackTags()):
        yield


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / "archive" / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
    folder.mkdir(parents=True)
    for index, name in enumerate(["01-kleine_monster.flac", "02-suesses.flac", "03-geister.flac"], 1):
        (folder / name).write_bytes(bytes([index]) * 8)
    # A real image: the cover is cropped and rendered on its way to a sticker,
    # so a three byte stand-in would only prove that nothing reads it.
    _write_jpeg(folder / "cover.jpg")
    return tmp_path / "archive"


def _write_jpeg(path, size=(600, 600), colour=(10, 120, 200)):
    from PIL import Image

    # Marked in the corner, not flat: cropping and blurring both leave a single
    # colour untouched, so a flat cover would hide whether either happened.
    picture = Image.new("RGB", size, colour)
    picture.paste(Image.new("RGB", (size[0] // 6, size[1] // 6), (240, 200, 20)), (0, 0))
    picture.save(path, format="JPEG")
    return path


@pytest.fixture
def settings(tmp_path, archive):
    return Settings(
        teddycloud_url="http://teddycloud.test",
        archive_dir=archive,
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def teddycloud():
    cloud = FakeTeddyCloud()
    # Two tags the box has already read; nothing is assigned yet.
    cloud.add_tag("7ab4ed0e500304e0")
    cloud.add_tag("deb4ed0e500304e0")
    return cloud


@pytest.fixture
def runner():
    runner = JobRunner()
    yield runner
    runner.shutdown()


@pytest.fixture
def box():
    """A watcher that never opens a connection; tests feed it events."""
    watcher = BoxWatcher("http://teddycloud.test")
    watcher.start = lambda: None
    return watcher


@pytest.fixture
def client(settings, teddycloud, runner, tags, box):
    app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)
    with TestClient(app) as client:
        yield client


def _album_id(client):
    """The id the picker gave the one album in the archive."""
    page = client.get("/").text
    start = page.index('name="ids" value="') + len('name="ids" value="')
    return page[start:page.index('"', start)]


def _eventually(condition, timeout=5.0):
    """Wait for something a background thread does, without sleeping blindly."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _finish(runner, response):
    """Wait for the job a redirect points at and return it."""
    job_id = response.headers["location"].rsplit("/", 1)[1]
    job = runner.get(job_id)
    assert job.done.wait(5), "the encode did not finish"
    return job


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
        identifier = _album_id(client)

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
        page = client.get(f"/album/{_album_id(client)}").text

        assert 'name="series" value="Lichterkinder"' in page
        assert 'name="episode" value="Halloween"' in page

    def test_lists_every_track_in_order(self, client):
        page = client.get(f"/album/{_album_id(client)}").text

        assert page.index("Kleine monster") < page.index("Suesses") < page.index("Geister")

    def test_serves_the_cover(self, client):
        response = client.get(f"/album/{_album_id(client)}/cover")

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
                page = fresh.get(f"/album/{_album_id(fresh)}").text

        assert page.count('name="selected"') == 4
        # "checked>" is the rendered attribute; the word alone also appears in
        # the script below the form.
        assert page.count("checked>") == 3

    def test_offers_a_way_to_drop_them_all_at_once(self, client):
        assert 'data-pick="all"' in client.get(f"/album/{_album_id(client)}").text


class TestOccupiedName:
    def test_asks_instead_of_failing_inside_the_job(self, client, teddycloud):
        # The old behaviour sent you to a progress page that then showed an
        # error. An occupied name is a question for the form.
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"older"

        response = client.post(f"/album/{_album_id(client)}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        assert response.status_code == 200
        assert "Dort liegt schon ein Taf" in response.text
        assert teddycloud.files["/Lichterkinder/Halloween.taf"] == b"older"

    def test_replaces_it_when_that_is_what_was_asked(self, client, teddycloud, runner):
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"older"

        response = client.post(f"/album/{_album_id(client)}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1", "selected": ["1"], "titles": ["a"],
            "replace": "/Lichterkinder/Halloween.taf",
        }, follow_redirects=False)

        assert response.status_code == 303
        _finish(runner, response)
        assert teddycloud.files["/Lichterkinder/Halloween.taf"] != b"older"


class TestEncoding:
    def test_encodes_the_selected_tracks_into_one_taf(self, client, teddycloud, runner):
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "Lichterkinder", "episode": "Halloween",
            "order": "1,2,3", "selected": ["1", "2", "3"],
            "titles": ["Kleine Monster", "Suesses", "Geister"],
        }, follow_redirects=False)

        job = _finish(runner, response)
        assert job.state == JobState.DONE
        assert "/Lichterkinder/Halloween.taf" in teddycloud.files

    def test_encodes_in_the_order_the_page_submitted(self, client, teddycloud, runner):
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Umsortiert",
            "order": "3,1,2", "selected": ["1", "2", "3"],
            "titles": ["Geister", "Kleine Monster", "Suesses"],
        }, follow_redirects=False)

        _finish(runner, response)
        assert teddycloud.files["/Umsortiert.taf"] == bytes([3]) * 8 + bytes([1]) * 8 + bytes([2]) * 8

    def test_leaves_out_what_was_unticked(self, client, teddycloud, runner):
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Nur zwei",
            "order": "1,2,3", "selected": ["1", "3"],
            "titles": ["Kleine Monster", "Suesses", "Geister"],
        }, follow_redirects=False)

        _finish(runner, response)
        assert teddycloud.files["/Nur zwei.taf"] == bytes([1]) * 8 + bytes([3]) * 8

    def test_keeps_the_names_aligned_to_the_reordered_tracks(self, client, runner):
        # The titles are submitted in row order, so the second name belongs to
        # whatever track ended up second, not to track two.
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Namen",
            "order": "3,1", "selected": ["1", "3"],
            "titles": ["Geister", "Kleine Monster"],
        }, follow_redirects=False)

        assert _finish(runner, response).result["tracks"] == ["Geister", "Kleine Monster"]

    def test_refuses_when_nothing_is_ticked(self, client):
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Leer", "order": "1,2,3", "titles": ["a", "b", "c"],
        }, follow_redirects=False)

        assert response.status_code == 400

    def test_refuses_an_episode_that_is_only_whitespace(self, client):
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "Lichterkinder", "episode": "   ",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        assert response.status_code == 400

    def test_reports_a_failed_encode_instead_of_swallowing_it(self, client, teddycloud, runner):
        teddycloud.encode_fails = "ffmpeg died"
        identifier = _album_id(client)

        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Kaputt",
            "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)

        job = _finish(runner, response)
        assert job.state == JobState.FAILED
        assert "ffmpeg died" in job.error


class TestJobPage:
    def test_reports_the_state_as_json(self, client, runner):
        identifier = _album_id(client)
        response = client.post(f"/album/{identifier}/encode", data={
            "series": "", "episode": "Status", "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)
        job = _finish(runner, response)

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

        identifier = _album_id(client)
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
        page = _album_page_after_upload(client, response)
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

        page = _album_page_after_upload(client, response)
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


class TestStartup:
    def test_clears_staging_that_an_earlier_run_abandoned(self, settings, teddycloud, runner, tags):
        teddycloud.create_dir("/_tonie-studio-staging")
        teddycloud.create_dir("/_tonie-studio-staging/abandoned")
        teddycloud.files["/_tonie-studio-staging/abandoned/001.flac"] = b"x"

        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner)
        with TestClient(app) as started:
            _eventually(lambda: teddycloud.files == {})
            assert started.get("/health").status_code == 200

    def test_clears_uploads_nobody_came_back_for(self, settings, teddycloud, runner, tags, box):
        import os, time

        stale = settings.upload_dir / "abandoned"
        stale.mkdir(parents=True)
        (stale / "01-track.mp3").write_bytes(b"x" * 64)
        long_ago = time.time() - 30 * 86400
        os.utime(stale, (long_ago, long_ago))

        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)
        with TestClient(app):
            _eventually(lambda: not stale.exists())

    def test_starts_while_teddycloud_is_unreachable(self, settings, runner, tags):
        # An unreachable teddyCloud answers only after DNS timeouts and
        # retries. Startup must not wait for that, or the readiness probe stays
        # down for an outage this app can serve pages through.
        import threading as _threading

        blocked = _threading.Event()

        class Hanging:
            def list_dir(self, path="/", special=None):
                blocked.wait(10)
                raise TeddyCloudError("teddyCloud is unreachable")

        app = create_app(settings, client_factory=Hanging, runner=runner)
        try:
            with TestClient(app) as started:
                assert started.get("/health").status_code == 200
        finally:
            blocked.set()


def _encode_and_assign_page(client, runner, **overrides):
    """Encode the archive album, then return its assignment page."""
    data = {
        "series": "Lichterkinder", "episode": "Halloween",
        "order": "1,2", "selected": ["1", "2"],
        "titles": ["Kleine Monster", "Suesses"],
    }
    data.update(overrides)
    response = client.post(f"/album/{_album_id(client)}/encode", data=data,
                           follow_redirects=False)
    job = _finish(runner, response)
    return job, client.get(f"/job/{job.id}/assign")


class TestAssignPage:
    def test_offers_the_tags_the_box_knows(self, client, runner):
        _job, page = _encode_and_assign_page(client, runner)

        assert page.status_code == 200
        assert "7ab4ed0e500304e0" in page.text and "deb4ed0e500304e0" in page.text

    def test_prefills_what_was_typed_on_the_album_page(self, client, runner):
        _job, page = _encode_and_assign_page(client, runner)

        assert 'name="series" value="Lichterkinder"' in page.text
        assert 'name="episode" value="Halloween"' in page.text

    def test_says_a_fresh_sticker_has_to_meet_the_box_first(self, client, runner):
        _job, page = _encode_and_assign_page(client, runner)

        assert "Toniebox" in page.text

    def test_reports_a_sticker_the_box_does_not_know(self, client, runner, box):
        # A blank NFC sticker reaches teddyCloud as a tag it has never seen.
        box.handle("TagInvalid", "E00403500EEDFFFF")

        assert client.get("/api/box").json()["unknown_ruid"] == "ffffed0e500304e0"

    def test_preselects_the_tag_that_was_just_held_against_the_box(self, client, runner):
        job, _page = _encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=deb4ed0e500304e0").text

        marked = page.split('value="deb4ed0e500304e0"')[1].split(">")[0]
        assert "checked" in marked
        assert "gerade aufgelegt" in page

    def test_ignores_a_stale_link_to_a_tag_that_is_not_there(self, client, runner):
        job, _page = _encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=ffffffffffffffff").text

        assert "gerade aufgelegt" not in page

    def test_refuses_before_the_encode_finished(self, client, runner, teddycloud):
        teddycloud.encode_fails = "ffmpeg died"
        response = client.post(f"/album/{_album_id(client)}/encode", data={
            "series": "", "episode": "Kaputt", "order": "1", "selected": ["1"], "titles": ["a"],
        }, follow_redirects=False)
        job = _finish(runner, response)

        assert client.get(f"/job/{job.id}/assign").status_code == 409


class TestAssign:
    def test_points_the_chosen_tag_at_the_new_taf(self, client, runner, teddycloud):
        job, _page = _encode_and_assign_page(client, runner)

        response = client.post(f"/job/{job.id}/assign", data={
            "ruid": "deb4ed0e500304e0", "series": "Lichterkinder",
            "episode": "Halloween", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 303
        entry = teddycloud.tag_entries["deb4ed0e500304e0"]
        assert entry["source"] == "lib://Lichterkinder/Halloween.taf"
        assert entry["nocloud"] is True

    def test_carries_the_edited_track_names_into_the_model(self, client, runner, teddycloud):
        job, _page = _encode_and_assign_page(client, runner, titles=["Erster", "Zweiter"])

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        assert teddycloud.custom_tonies()[0]["tracks"] == ["Erster", "Zweiter"]

    def test_takes_the_cover_from_the_album_folder(self, client, runner, teddycloud):
        job, _page = _encode_and_assign_page(client, runner)

        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        # What reaches teddyCloud is the cover already cut for the round
        # sticker, not the file from the album folder.
        assert len(teddycloud.images) == 1
        assert next(iter(teddycloud.images.values())).startswith(b"\xff\xd8\xff")

    def test_refuses_a_tag_teddycloud_does_not_know(self, client, runner):
        job, _page = _encode_and_assign_page(client, runner)

        response = client.post(f"/job/{job.id}/assign", data={
            "ruid": "ffffffffffffffff", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 400


class TestToniesPage:
    def test_counts_what_is_recorded(self, client, runner, teddycloud):
        job, _page = _encode_and_assign_page(client, runner)
        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "Lichterkinder",
            "episode": "Halloween", "category": "music",
        }, follow_redirects=False)

        page = client.get("/tonies").text

        assert "1 von 2 Sticker bespielt" in page
        assert "Lichterkinder - Halloween" in page

    def test_lists_free_tags_too(self, client):
        assert "frei" in client.get("/tonies").text


def _publish(client, runner, teddycloud):
    """Encode and assign, so a custom model with a cover exists."""
    job, _page = _encode_and_assign_page(client, runner)
    client.post(f"/job/{job.id}/assign", data={
        "ruid": "7ab4ed0e500304e0", "series": "Lichterkinder",
        "episode": "Halloween", "category": "music",
    }, follow_redirects=False)
    return next(iter(teddycloud.images))


def _jpeg(size=(400, 400)):
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


class TestSheetPage:
    def test_says_so_while_no_model_has_a_cover(self, client):
        assert "Noch kein Modell" in client.get("/sheet").text

    def test_lists_a_published_tonie(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert "Lichterkinder" in page and "TS-0001" in page

    def test_passes_the_cover_through_from_teddycloud(self, client, runner, teddycloud):
        # The browser cannot reach teddyCloud's own address, so the image has to
        # come through this app.
        name = _publish(client, runner, teddycloud)

        response = client.get(f"/cover/custom/{name}")

        assert response.status_code == 200
        assert response.content.startswith(b"\xff\xd8\xff")

    def test_refuses_a_name_that_climbs_out_of_the_image_folder(self, client):
        assert client.get("/cover/custom/..%2Fconfig").status_code in (400, 404)

    def test_serves_the_cover_the_way_the_chosen_mode_prints_it(self, client, runner, teddycloud):
        # Pointing at "ganzes Cover" and being shown the edge to edge version
        # answers a question nobody asked, so each mode renders its own picture.
        name = _publish(client, runner, teddycloud)

        fill = client.get(f"/cover/custom/{name}?mode=fill&size=128")
        fit = client.get(f"/cover/custom/{name}?mode=fit&size=128")

        assert fill.status_code == fit.status_code == 200
        assert fill.content != fit.content

    def test_an_unknown_mode_still_gives_the_cover(self, client, runner, teddycloud):
        name = _publish(client, runner, teddycloud)

        response = client.get(f"/cover/custom/{name}?mode=nonsense")

        assert response.status_code == 200
        assert response.content.startswith(b"\xff\xd8\xff")


class TestSheet:
    def test_builds_a_pdf_for_the_chosen_count(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        # The fake stored a three byte "jpeg", which Pillow cannot read, so the
        # cover is replaced with a real one for this test.
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        response = client.post("/sheet", data={"models": ["TS-0001"], "counts": ["4"]})

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_the_back_side_doubles_what_goes_on_the_sheet(self, client, runner, teddycloud):
        # One text side per copy, so four tonies come off the sheet as four
        # pairs rather than four pictures and a guess.
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        with _captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["2"], "back": "on"})

        stickers = asked[-1]
        assert [sticker.count for sticker in stickers] == [2, 2]
        assert [sticker.back is None for sticker in stickers] == [True, False]

    def test_without_the_box_ticked_only_the_cover_is_printed(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        with _captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"]})

        assert all(sticker.back is None for sticker in asked[-1])

    def test_the_back_can_leave_the_track_names_off(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        with _captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"],
                                        "back": "on", "back_tracks": "on"})

        assert not asked[0][1].back.tracks  # the box is only ticked the second time
        assert asked[1][1].back.tracks

    def test_the_back_carries_the_year_and_the_playing_time(self, client, runner,
                                                            teddycloud, settings):
        # Both only exist because the app put them there: the year in the
        # catalogue's own release field, the playing time beside the app's data
        # because the format has nowhere for it.
        from tonie_studio.runtimes import remember

        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()
        model = next(entry for entry in teddycloud.custom_models
                     if entry["model"] == "TS-0001")
        model["release"] = "1609459200"  # 2021-01-01
        remember(settings.data_dir, "TS-0001", 3960)

        with _captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})

        notes = " ".join(asked[-1][1].back.notes)
        assert "2021" in notes
        assert "66 Minuten" in notes

    def test_a_tonie_recorded_before_says_less_rather_than_guessing(self, client, runner,
                                                                    teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        with _captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})

        notes = " ".join(asked[-1][1].back.notes)
        assert "Minuten" not in notes
        assert "TS-0001" in notes

    def test_the_page_offers_the_back_side(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="back"' in page and "Rückseite" in page

    def test_takes_an_uploaded_image_as_well(self, client):
        response = client.post(
            "/sheet",
            data={"models": [], "counts": []},
            files=[("files", ("extra.jpg", _jpeg(), "image/jpeg"))],
        )

        assert response.content.startswith(b"%PDF")

    def test_offers_the_choice_between_filling_and_fitting(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="modes"' in page
        assert "randlos" in page and "ganzes Cover" in page

    def test_passes_the_chosen_mode_through_per_sticker(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()
        seen = []

        import tonie_studio.pages.printing as web
        original = web.build_sheet
        try:
            web.build_sheet = lambda stickers, target, shift=(0.0, 0.0): (
                seen.extend(s.mode for s in stickers) or original(stickers, target))
            client.post("/sheet", data={
                "models": ["TS-0001"], "counts": ["2"], "modes": ["fit"],
            })
        finally:
            web.build_sheet = original

        assert seen == ["fit", "fit"] or seen == ["fit"]

    def test_refuses_a_sheet_with_nothing_on_it(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        response = client.post("/sheet", data={"models": ["TS-0001"], "counts": ["0"]})

        assert response.status_code == 400

    def test_leaves_no_pdf_behind_in_the_data_directory(self, client, settings):
        client.post("/sheet", files=[("files", ("extra.jpg", _jpeg(), "image/jpeg"))])

        assert list(settings.data_dir.glob("sheet-*.pdf")) == []


class TestTeddyCloudDown:
    @pytest.fixture
    def offline(self, settings, runner, tags):
        class Offline:
            def __getattr__(self, _name):
                def unreachable(*args, **kwargs):
                    raise TeddyCloudError("teddyCloud at http://teddycloud.test is unreachable")
                return unreachable

        app = create_app(settings, client_factory=Offline, runner=runner)
        with TestClient(app) as client:
            yield client

    def test_the_album_list_still_works(self, offline):
        # Browsing the archive needs nothing from teddyCloud.
        assert offline.get("/").status_code == 200

    def test_says_which_service_is_away(self, offline):
        response = offline.get("/tonies")

        assert response.status_code == 503
        assert "teddyCloud antwortet nicht" in response.text

    def test_the_sheet_page_says_it_too(self, offline):
        assert offline.get("/sheet").status_code == 503

    def test_health_stays_green_because_this_app_is_fine(self, offline):
        # Readiness must not follow teddyCloud, or its restart rolls this pod.
        assert offline.get("/health").status_code == 200


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


class TestCoverCrop:
    def test_renders_the_cover_as_the_sticker_will_cut_it(self, client):
        response = client.get(f"/album/{_album_id(client)}/crop?zoom=1.5&x=0.5&size=64")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_zooming_changes_the_picture(self, client, archive):
        from PIL import Image

        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        image = Image.new("RGB", (300, 300), (255, 0, 0))
        image.paste(Image.new("RGB", (60, 60), (0, 0, 255)), (120, 120))
        image.save(folder / "cover.jpg", format="JPEG")
        identifier = _album_id(client)

        wide = client.get(f"/album/{identifier}/crop?zoom=1&size=48").content
        close = client.get(f"/album/{identifier}/crop?zoom=2.5&size=48").content

        assert wide != close

    def test_offers_every_image_in_the_folder_to_choose_from(self, client, archive):
        # The reader collects them all and ranks them; the page used to show
        # only the first, which for a scene rip is a photo of the disc.
        folder = archive / "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN"
        _write_jpeg(folder / "back.jpg", colour=(200, 30, 30))

        page = client.get(f"/album/{_album_id(client)}").text

        assert page.count('class="pick') == 2

    def test_the_chosen_crop_is_what_reaches_teddycloud(self, client, runner, teddycloud):
        job, _page = _encode_and_assign_page(client, runner)
        client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "L", "episode": "H", "category": "music",
        }, follow_redirects=False)

        from PIL import Image
        import io as _io

        stored = next(iter(teddycloud.images.values()))
        with Image.open(_io.BytesIO(stored)) as image:
            assert image.size[0] == image.size[1]  # square, ready for the circle


class TestEditTonie:
    def test_shows_the_model_behind_a_tag(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert 'name="series" value="Lichterkinder"' in page
        assert "TS-0001" in page

    def test_says_so_when_there_is_nothing_to_edit(self, client):
        page = client.get("/tonies/7ab4ed0e500304e0").text

        assert "kein eigenes Modell" in page

    def test_saves_a_corrected_title(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        response = client.post("/tonies/7ab4ed0e500304e0", data={
            "series": "Lichterkinder", "episode": "Halloween Lieder", "category": "music",
        }, follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.custom_tonies()[0]["episodes"] == "Halloween Lieder"

    def test_keeps_the_audio_identity_across_an_edit(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        before = teddycloud.custom_tonies()[0]["audio_id"]

        client.post("/tonies/7ab4ed0e500304e0", data={
            "series": "Neu", "episode": "Neu", "category": "music",
        }, follow_redirects=False)

        assert teddycloud.custom_tonies()[0]["audio_id"] == before

    def test_replaces_the_cover_from_an_upload(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        client.post("/tonies/7ab4ed0e500304e0",
                    data={"series": "L", "episode": "H", "category": "music"},
                    files={"cover": ("neu.jpg", _jpeg(), "image/jpeg")},
                    follow_redirects=False)

        assert teddycloud.images["TS-0001.jpg"] == _jpeg()

    def test_removing_the_model_leaves_the_tonie_playing(self, client, runner, teddycloud):
        # The audio hangs off the tag, not off the model.
        _publish(client, runner, teddycloud)

        response = client.post("/tonies/7ab4ed0e500304e0/delete", follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.custom_tonies() == []
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"].startswith("lib://")

    def test_refuses_a_tag_teddycloud_does_not_know(self, client):
        assert client.get("/tonies/ffffffffffffffff").status_code == 404


class TestDuplicateAudio:
    def test_warns_before_a_second_model_for_the_same_recording(self, client, runner, teddycloud):
        # Two models with the same audio_id and hash fight each other for
        # teddyCloud's automatic matching.
        _publish(client, runner, teddycloud)

        _job, page = _encode_and_assign_page(client, runner, episode="Halloween zwei")

        assert "Dieses Audio gibt es schon" in page.text
        assert "TS-0001" in page.text

    def test_says_nothing_when_the_recording_is_new(self, client, runner):
        _job, page = _encode_and_assign_page(client, runner)

        assert "Dieses Audio gibt es schon" not in page.text


class TestPrintFromTonie:
    def test_offers_a_sticker_link_per_recorded_tonie(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        assert "/sheet?model=TS-0001" in client.get("/tonies").text

    def test_the_sheet_arrives_with_that_motif_asked_for(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/sheet?model=TS-0001").text

        assert 'value="1"' in page.split('name="counts"')[1][:80]

    def test_without_a_model_nothing_is_preselected(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'value="0"' in page.split('name="counts"')[1][:80]


class TestLibraryPage:
    def test_lists_the_tafs_with_their_size(self, client, runner, teddycloud):
        _encode_and_assign_page(client, runner)

        page = client.get("/library").text

        assert "Halloween" in page and re.search(r"\d (Byte|KB|MB)\b", page)

    def test_names_the_tonie_that_would_fall_silent(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/library").text

        assert "wird gespielt von" in page
        assert "Lichterkinder - Halloween" in page

    def test_deletes_one(self, client, runner, teddycloud):
        _encode_and_assign_page(client, runner)

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
        _publish(client, runner, teddycloud)
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
        _publish(client, runner, teddycloud)

        page = client.get("/radio").text

        assert "Vorhandene Radio-Tonies" not in page


class TestGermanSurface:
    def test_names_the_ordering_in_german(self, client):
        # "Reihenfolge nach filename number" is an internal identifier leaking
        # into a German sentence.
        page = client.get(f"/album/{_album_id(client)}").text

        assert "Reihenfolge nach Dateinummer" in page
        assert "file_number" not in page

    def test_writes_umlauts_rather_than_transliterating_them(self, client):
        for path in ("/", "/tonies", "/library", "/radio", "/sheet"):
            page = client.get(path).text
            for wrong in ("loeschen", "auswaehlen", "zurueck", "fuer ", "Zusaetzliche"):
                assert wrong not in page, f"{wrong!r} steht noch in {path}"


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
        job, _page = _encode_and_assign_page(client, runner)

        page = client.get(f"/job/{job.id}/assign?new=ffffed0e500304e0").text

        assert "alreadyTried" in page
        assert "kennt ihn nicht" in page


@contextmanager
def _captured_sheet():
    """Collect the stickers the route asks for, without rendering a PDF.

    The route answers with the file it just wrote, so the stand-in has to leave
    one behind or the response fails for a reason that has nothing to do with
    what is being tested.
    """
    asked = []

    def instead(stickers, target, shift=(0.0, 0.0)):
        asked.append(list(stickers))
        target.write_bytes(b"%PDF-1.4\n")
        return []

    with patch("tonie_studio.pages.printing.build_sheet", side_effect=instead):
        yield asked


class TestListeningPage:
    """The box's events end up in the log and on the page, with names."""

    def test_a_tonie_put_on_the_box_appears_as_heard(self, client, runner, teddycloud, box):
        _publish(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        uid = ruid_to_uid(ruid)

        box.handle("TagValid", uid)
        box.handle("playback", "started")

        page = client.get("/listening?period=7").text
        heard = page.split("Am meisten gehört", 1)[1].split("Lange nicht", 1)[0]
        assert "Lichterkinder" in heard
        assert "einmal aufgelegt" in heard

    def test_a_recorded_tonie_never_put_on_is_listed_as_quiet(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/listening").text

        assert "Lange nicht gehört" in page
        assert "nie aufgelegt" in page

    def test_says_since_when_it_has_been_listening(self, client, runner, teddycloud, box):
        # Numbers without their start date read as all-time totals.
        _publish(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        box.handle("TagValid", ruid_to_uid(ruid))

        assert "Aufgezeichnet seit" in client.get("/listening").text

    def test_an_unknown_period_falls_back_rather_than_failing(self, client):
        assert client.get("/listening?period=forever").status_code == 200

    def test_the_log_lives_on_the_data_volume(self, client, runner, teddycloud, box, settings):
        from tonie_studio.listening import FILE_NAME

        _publish(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        box.handle("TagValid", ruid_to_uid(ruid))
        box.flush()

        assert (settings.data_dir / FILE_NAME).exists()


def list_tags_of(teddycloud):
    from tonie_studio.tonie import list_tags

    return list_tags(teddycloud)


def ruid_to_uid(ruid):
    """The reverse of ruid_from_uid, as the box would report the tag."""
    return "".join(ruid[index:index + 2] for index in range(14, -2, -2)).upper()


class TestCheckupPage:
    def test_a_healthy_setup_says_so(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = True

        assert "Alles in Ordnung" in client.get("/checkup").text

    def test_repairs_a_garbled_name_and_nothing_else(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        model = teddycloud.custom_models[0]
        model["series"] = "Bobo SiebenschlÃ¤fer"
        before = {key: value for key, value in model.items() if key != "series"}

        response = client.post("/checkup/fix", data={"fix": "repair_model", "target": "TS-0001"},
                               follow_redirects=False)

        repaired_model = teddycloud.custom_models[0]
        assert response.status_code == 303
        assert repaired_model["series"] == "Bobo Siebenschläfer"
        assert {key: value for key, value in repaired_model.items() if key != "series"} == before

    def test_switches_off_the_cloud_fallback(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid]["nocloud"] = False

        client.post("/checkup/fix", data={"fix": "nocloud", "target": ruid})

        assert teddycloud.tag_entries[ruid]["nocloud"] is True

    def test_refuses_a_repair_that_is_no_longer_due(self, client, runner, teddycloud):
        # A page left open must not act on what it showed an hour ago.
        _publish(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = True
        played = next(e["source"] for e in teddycloud.tag_entries.values() if e["source"])
        path = "/" + played[len("lib://"):]

        response = client.post("/checkup/fix", data={"fix": "delete_taf", "target": path},
                               follow_redirects=False)

        assert response.headers["location"] == "/checkup?stale=1"
        assert path in teddycloud.files

    def test_deletes_a_taf_nothing_plays(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.files["/Lichterkinder/Alt.taf"] = b"x"
        teddycloud.taf_headers["/Lichterkinder/Alt.taf"] = {"valid": True}

        client.post("/checkup/fix", data={"fix": "delete_taf", "target": "/Lichterkinder/Alt.taf"})

        assert "/Lichterkinder/Alt.taf" not in teddycloud.files

    def test_an_unknown_repair_is_refused(self, client):
        response = client.post("/checkup/fix", data={"fix": "format_disk", "target": "/"},
                               follow_redirects=False)

        assert response.headers["location"] == "/checkup?stale=1"


class TestBackupPages:
    def test_downloads_a_backup(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        response = client.get("/backup/download")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]

    def test_a_preview_writes_nothing(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()

        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text

        assert teddycloud.custom_models == []
        assert "Jetzt wiederherstellen" in page

    def test_confirming_restores_and_forgets_the_upload(self, client, runner, teddycloud, settings):
        _publish(client, runner, teddycloud)
        data = client.get("/backup/download").content
        saved = [dict(model) for model in teddycloud.custom_models]
        teddycloud.custom_models.clear()

        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        client.post(f"/restore/{token}")

        assert teddycloud.custom_models == saved
        assert not (settings.data_dir / "restore" / f"{token}.zip").exists()

    def test_a_confirmation_cannot_be_replayed(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        client.post(f"/restore/{token}")

        assert client.post(f"/restore/{token}").status_code == 404

    def test_refuses_a_file_that_is_not_a_backup(self, client):
        response = client.post("/restore", files={"file": ("x.zip", b"nope", "application/zip")})

        assert response.status_code == 400

    def test_refuses_a_token_that_is_a_path(self, client):
        assert client.post("/restore/..%2F..%2Fetc").status_code in (400, 404)


class TestCopyTonie:
    def test_a_second_sticker_plays_the_same(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        played = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        free = next(r for r, e in teddycloud.tag_entries.items() if not e["source"])

        response = client.post(f"/tonies/{played}/copy", data={"target": free},
                               follow_redirects=False)

        assert response.status_code == 303
        assert teddycloud.tag_entries[free]["source"] == teddycloud.tag_entries[played]["source"]

    def test_the_page_offers_the_free_sticker(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        played = next(r for r, e in teddycloud.tag_entries.items() if e["source"])

        page = client.get(f"/tonies/{played}").text

        assert "Auf einen weiteren Sticker" in page and "frei - " in page


class TestAlbumMarkers:
    def test_an_album_made_into_a_tonie_says_so(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        assert "hat einen Tonie" in client.get("/").text

    def test_deleting_the_tonie_takes_the_marker_away(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
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
        _publish(client, runner, teddycloud)

        page = client.get("/").text

        assert "fertig" in page and ">done<" not in page


class TestOverviewSheet:
    def test_lists_the_tonies_on_stickers(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()

        response = client.get("/sheet/overview")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_a_model_on_no_sticker_is_left_out(self, client, runner, teddycloud):
        from tonie_studio.pages.printing import _overview_entries

        _publish(client, runner, teddycloud)
        teddycloud.custom_models.append({"model": "TS-0009", "series": "Nirgends"})

        titles = [entry.series for entry in _overview_entries(teddycloud, {})]

        assert "Nirgends" not in titles

    def test_nothing_to_list_is_said(self, client):
        assert client.get("/sheet/overview").status_code == 400


class TestCalibrationPages:
    def test_the_calibration_sheet_downloads(self, client):
        response = client.get("/sheet/calibration")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_a_measurement_with_a_decimal_comma_is_understood(self, client, settings):
        from tonie_studio.calibration import expected_first_centre, read_shift

        left, top = expected_first_centre()
        typed_left = f"{left + 1:.1f}".replace(".", ",")

        client.post("/sheet/calibration", data={"left_mm": typed_left, "top_mm": f"{top:.1f}"})

        assert read_shift(settings.data_dir)[0] == pytest.approx(-1.0, abs=0.06)

    def test_the_stored_shift_reaches_the_sheet(self, client, runner, teddycloud, settings):
        from tonie_studio.calibration import expected_first_centre

        _publish(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = _jpeg()
        left, top = expected_first_centre()
        client.post("/sheet/calibration", data={"left_mm": str(left + 2), "top_mm": str(top)})

        shifts = []

        def instead(stickers, target, shift=(0.0, 0.0)):
            shifts.append(shift)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.pages.printing.build_sheet", side_effect=instead):
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"]})

        assert shifts[-1][0] == pytest.approx(-2.0, abs=0.06)

    def test_a_reset_removes_the_shift(self, client, settings):
        from tonie_studio.calibration import expected_first_centre, read_shift

        left, top = expected_first_centre()
        client.post("/sheet/calibration", data={"left_mm": str(left + 2), "top_mm": str(top + 1)})

        client.post("/sheet/calibration/reset")

        assert read_shift(settings.data_dir) == pytest.approx((0.0, 0.0))

    def test_nonsense_is_refused(self, client):
        response = client.post("/sheet/calibration", data={"left_mm": "links", "top_mm": "2"})

        assert response.status_code == 400


class TestParts:
    def _encode(self, client, **overrides):
        data = {"series": "Lichterkinder", "episode": "Halloween",
                "order": "1,2,3", "selected": ["1", "2", "3"],
                "titles": ["Eins", "Zwei", "Drei"]}
        data.update(overrides)
        return client.post(f"/album/{_album_id(client)}/encode", data=data,
                           follow_redirects=False)

    def test_one_part_is_the_old_single_encode(self, client, runner, teddycloud):
        job = _finish(runner, self._encode(client))

        assert job.result["episode"] == "Halloween"

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

        names = [name for job in runner.all() for name in job.result["tracks"]]
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


class TestPodcastPages:
    @pytest.fixture
    def web(self):
        from test_podcast import FakeWeb

        return FakeWeb()

    @pytest.fixture
    def podcasts(self, settings, teddycloud, runner, tags, box, web):
        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner,
                         watcher=box, fetch=web)
        with TestClient(app) as client:
            yield client

    def test_subscribing_puts_the_episodes_on_the_sticker(self, podcasts, runner, teddycloud):
        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0", "keep": "2",
        }, follow_redirects=False)
        job = _finish(runner, response)

        assert job.error is None
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"].startswith("lib://Podcasts/")
        assert "Kakadu" in podcasts.get("/podcasts").text

    def test_a_wrong_address_is_said_on_the_form(self, podcasts, web):
        web.feed = b"<html>kein Feed</html>"

        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        })

        assert response.status_code == 400
        assert "nicht lesen" in response.text

    def test_a_failed_refresh_is_kept_on_the_subscription(self, podcasts, runner, teddycloud):
        teddycloud.encode_fails = "ffmpeg died"

        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False)
        _finish(runner, response)

        assert "ffmpeg died" in podcasts.get("/podcasts").text

    def test_unsubscribing_leaves_the_sticker_playing(self, podcasts, runner, teddycloud, settings):
        from tonie_studio.podcast import load_subscriptions

        _finish(runner, podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False))
        subscription = load_subscriptions(settings.data_dir)[0]
        playing = teddycloud.tag_entries["deb4ed0e500304e0"]["source"]

        podcasts.post(f"/podcasts/{subscription.id}/delete")

        assert load_subscriptions(settings.data_dir) == []
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == playing

    def test_a_finished_podcast_job_does_not_offer_to_assign(self, podcasts, runner):
        response = podcasts.post("/podcasts", data={
            "feed": "https://example.org/feed.xml", "ruid": "deb4ed0e500304e0",
        }, follow_redirects=False)
        job = _finish(runner, response)

        page = podcasts.get(f"/job/{job.id}").text

        assert "Zu den Podcasts" in page and "Auf einen Tonie legen" not in page


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


class TestRestoreLimits:
    def test_a_huge_upload_is_refused(self, client, monkeypatch):
        import tonie_studio.backup as backup
        import tonie_studio.pages.upkeep as upkeep

        monkeypatch.setattr(backup, "LARGEST_BACKUP", 1000)
        monkeypatch.setattr(upkeep, "LARGEST_BACKUP", 1000)

        response = client.post("/restore", files={"file": ("b.zip", b"x" * 5000, "application/zip")})

        assert response.status_code == 400

    def test_a_backup_without_a_date_still_shows(self, client, runner, teddycloud):
        import io
        import json
        import zipfile

        _publish(client, runner, teddycloud)
        original = zipfile.ZipFile(io.BytesIO(client.get("/backup/download").content))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as rebuilt:
            for name in original.namelist():
                data = original.read(name)
                if name == "manifest.json":
                    manifest = json.loads(data)
                    del manifest["created"]
                    data = json.dumps(manifest).encode()
                rebuilt.writestr(name, data)

        response = client.post("/restore", files={"file": ("b.zip", buffer.getvalue(), "application/zip")})

        assert response.status_code == 200


class TestCheckupSparesWork:
    def test_an_encoded_but_unassigned_taf_is_not_offered_for_deletion(self, client, runner):
        job, _page = _encode_and_assign_page(client, runner)

        page = client.get("/checkup").text

        assert job.label not in page


class TestSmallRefusals:
    @pytest.mark.parametrize("typed", ["nan", "inf", "-inf"])
    def test_a_measurement_that_is_not_a_length_is_refused(self, client, typed):
        response = client.post("/sheet/calibration", data={"left_mm": typed, "top_mm": "50"})

        assert response.status_code == 400

    def test_more_parts_than_tracks_is_said_in_german(self, client):
        response = client.post(f"/album/{_album_id(client)}/encode", data={
            "series": "S", "episode": "E", "order": "1,2,3", "selected": ["1", "2", "3"],
            "titles": ["a", "b", "c"], "parts": "5",
        })

        assert response.status_code == 400
        assert "Teile" in response.text and "tracks cannot" not in response.text

    def test_no_parts_at_all_is_refused_plainly(self, client):
        response = client.post(f"/album/{_album_id(client)}/encode", data={
            "series": "S", "episode": "E", "order": "1", "selected": ["1"],
            "titles": ["a"], "parts": "0",
        })

        assert response.status_code == 400 and "passen nicht" not in response.text


class TestListeningRefreshes:
    def test_the_page_carries_the_parts_it_swaps(self, client):
        page = client.get("/listening").text

        assert 'id="live"' in page and 'id="lists"' in page

    def test_asking_again_shows_a_second_listen(self, client, runner, teddycloud, box):
        # The page refreshes by fetching itself; a second placement has to be
        # in that answer, including the one still held back in its bundle.
        _publish(client, runner, teddycloud)
        ruid = next(tag.ruid for tag in list_tags_of(teddycloud) if tag.model)
        uid = ruid_to_uid(ruid)
        removed = ('{"function_group":15,"function":16067,"field6":"'
                   + uid[8:] + uid[:8] + '"}')

        box.handle("TagValid", uid)
        assert "einmal aufgelegt" in client.get("/listening?period=7").text

        box.handle("rtnl-raw-log2", removed)
        box.handle("TagValid", uid)

        assert "2-mal aufgelegt" in client.get("/listening?period=7").text



class TestBackupPageSizes:
    def test_lists_the_parts_and_their_total(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        page = client.get("/backup").text

        for part in ("Modelle", "Sticker", "Cover", "Spielzeiten", "Hörprotokoll", "zusammen etwa"):
            assert part in page

    def test_no_fixed_size_promise_is_left(self, client):
        assert "Megabyte" not in client.get("/backup").text



class TestRefusals:
    def test_a_refusal_is_a_page_in_the_readers_language(self, client):
        client.cookies.set("lang", "en")

        response = client.get("/job/nope")

        assert response.status_code == 404
        assert "There is no such job." in response.text and "<html" in response.text

    def test_german_is_the_default(self, client):
        assert "Diesen Auftrag gibt es nicht." in client.get("/job/nope").text

    def test_the_api_answers_json_in_the_readers_language(self, client):
        response = client.get("/api/job/nope", headers={"accept-language": "en-GB,en"})

        assert response.status_code == 404
        assert response.json() == {"detail": "There is no such job."}

    def test_a_cause_that_is_itself_translated_is_said_in_the_same_language(self):
        from tonie_studio.i18n import Translator
        from tonie_studio.podcast import FeedError
        from tonie_studio.pages.common import Refused

        refusal = Refused(400, "error.feed_unreadable", detail=FeedError("feed.no_title"))

        assert refusal.say(Translator("de")) == (
            "Dieser Feed l\u00e4sst sich nicht lesen: Der Feed hat keinen Titel.")
        assert refusal.say(Translator("en")) == "That feed cannot be read: The feed has no title."


class TestPrintedLanguage:
    def test_the_back_side_speaks_the_readers_language(self):
        from tonie_studio.i18n import Translator
        from tonie_studio.pages.printing import _back_of
        from tonie_studio.tonie import Model

        # The release is 2019-01-01 in the catalogue's seconds-since-1970 form.
        model = Model(id="TS-0001", series="S", episode="E", release="1546300800",
                      tracks=["a", "b"])

        assert _back_of(model, 1500, Translator("en")).notes[0] == "2019 - 2 tracks - 25 minutes"
        assert _back_of(model, 1500, Translator("de")).notes[0] == "2019 - 2 Tracks - 25 Minuten"
        assert _back_of(model, 1500, Translator("en")).language == "en"


def _album_page_after_upload(client, response):
    """The detailed page of the one album an upload made, reached the way a reader would."""
    batch_page = client.get(response.headers["location"]).text
    start = batch_page.index('href="/album/') + len('href="')
    return client.get(batch_page[start:batch_page.index('"', start)]).text


def _upload_series(client, episodes=3):
    """Upload a parent folder holding one subfolder per episode, as a browser sends it."""
    files = []
    for number in range(episodes, 0, -1):
        files.append(("files", (f"Serie/Folge {number}/01-anfang.mp3", bytes([number]) * 8, "audio/mpeg")))
        files.append(("files", (f"Serie/Folge {number}/02-ende.mp3", bytes([number]) * 9, "audio/mpeg")))
    return client.post("/upload", files=files, follow_redirects=False)


def _batch_ids(response):
    from urllib.parse import parse_qs, urlparse

    location = urlparse(response.headers["location"])
    assert location.path == "/batch"
    return parse_qs(location.query)["ids"]


def _run_batch(client, runner, ids, series=None, **extra):
    data = {"ids": ids}
    if series is not None:
        data["series"] = [series] * len(ids)
    data.update(extra)
    response = client.post("/batch", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    for job in runner.all():
        assert job.done.wait(5), "a batch encode did not finish"
    return response


class TestSeveralFolders:
    def test_a_folder_of_folders_becomes_one_album_each(self, client):
        response = _upload_series(client)

        assert response.status_code == 303
        assert len(_batch_ids(response)) == 3
        page = client.get("/").text
        assert "Folge 1" in page and "Folge 2" in page and "Folge 3" in page

    def test_the_batch_page_lists_the_episodes_in_order_with_their_stickers(self, client):
        ids = _batch_ids(_upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.index("Folge 1") < page.index("Folge 2")
        assert "7ab4ed0e500304e0" in page and "deb4ed0e500304e0" in page

    def test_says_which_episodes_will_have_no_sticker(self, client):
        ids = _batch_ids(_upload_series(client, episodes=3))

        page = client.get("/batch", params={"ids": ids}).text

        # Two free tags, three episodes.
        assert page.count("ohne Sticker") == 1

    def test_encodes_every_album_and_assigns_the_free_tags(self, client, runner, teddycloud):
        ids = _batch_ids(_upload_series(client, episodes=2))

        _run_batch(client, runner, ids, series="Serie")

        sources = sorted(entry["source"] for entry in teddycloud.tag_entries.values())
        assert sources == ["lib://Serie/Folge 1.taf", "lib://Serie/Folge 2.taf"]
        episodes = sorted(model["episodes"] for model in teddycloud.custom_tonies())
        assert episodes == ["Folge 1", "Folge 2"]

    def test_the_first_free_tag_gets_the_first_episode(self, client, runner, teddycloud):
        from tonie_studio.tonie import list_tags

        free = [tag.ruid for tag in list_tags(teddycloud) if not tag.assigned]
        ids = _batch_ids(_upload_series(client, episodes=2))

        _run_batch(client, runner, ids, series="Serie")

        assert teddycloud.tag_entries[free[0]]["source"] == "lib://Serie/Folge 1.taf"

    def test_an_episode_without_a_sticker_is_encoded_and_left_to_assign(self, client, runner,
                                                                        teddycloud):
        ids = _batch_ids(_upload_series(client, episodes=3))

        _run_batch(client, runner, ids, series="Serie")

        from tonie_studio.encode import library_path, taf_exists

        assert taf_exists(teddycloud, library_path("Serie", "Folge 3"))
        assert len(teddycloud.custom_tonies()) == 2
        third = next(job for job in runner.all() if job.label.endswith("Folge 3.taf"))
        assert third.state == JobState.DONE and not third.result.get("model")

    def test_a_sticker_taken_in_the_meantime_is_not_overwritten(self, client, runner, teddycloud):
        ids = _batch_ids(_upload_series(client, episodes=2))
        # Taken as the first encode is queued, after the plan was made.
        original = runner.submit

        def take_then_submit(*args, **kwargs):
            teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
            teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Zwei.taf"
            return original(*args, **kwargs)

        runner.submit = take_then_submit
        _run_batch(client, runner, ids, series="Serie")

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Andere/Folge.taf"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Andere/Zwei.taf"
        for job in runner.all():
            assert job.state == JobState.DONE and job.result.get("assign_error")
        note = client.get(f"/api/job/{runner.all()[0].id}").json()["note"]
        assert note and not note.startswith("batch.")

    def test_the_edited_names_are_used(self, client, runner, teddycloud):
        ids = _batch_ids(_upload_series(client, episodes=2))

        _run_batch(client, runner, ids, series="Bobo", episodes=["Im Zoo", "Am Meer"])

        models = {model["episodes"]: model for model in teddycloud.custom_tonies()}
        assert set(models) == {"Im Zoo", "Am Meer"}
        assert all(model["series"] == "Bobo" for model in models.values())

    def test_the_album_list_offers_to_pick_several(self, client):
        page = client.get("/").text

        assert 'name="ids"' in page and 'action="/batch"' in page

    def test_refuses_an_empty_batch(self, client):
        assert client.post("/batch", data={}).status_code == 400

    def test_the_result_lists_every_encode(self, client, runner):
        ids = _batch_ids(_upload_series(client, episodes=2))

        response = _run_batch(client, runner, ids, series="Serie")

        page = client.get(response.headers["location"]).text
        assert page.count("data-job=") == 2

    def test_an_encoded_album_without_a_sticker_only_gets_one(self, client, runner, teddycloud):
        # No free sticker, so the first round leaves tafs and no tonies - the
        # state a restart in the middle of placing leaves behind as well.
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = _batch_ids(_upload_series(client, episodes=2))
        _run_batch(client, runner, ids, series="Serie")
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")

        page = client.get("/batch", params={"ids": ids}).text
        with patch("tonie_studio.pages.common.encode_files",
                   side_effect=AssertionError("encoded again")):
            _run_batch(client, runner, ids, series="Serie", stickers="auto")

        assert page.count("schon encodiert, bekommt nur noch einen Sticker") == 2
        assert teddycloud.tag_entries["aaaaaaaaaaaaaaaa"]["source"] == "lib://Serie/Folge 1.taf"
        assert [model["episodes"] for model in teddycloud.custom_tonies()] == ["Folge 1"]

    def test_an_encoded_album_a_sticker_plays_is_skipped(self, client, runner, teddycloud, settings):
        ids = _batch_ids(_upload_series(client, episodes=2))
        _run_batch(client, runner, ids, series="Serie")
        # Neither recorded nor recognisable: only the taf and its sticker are left.
        (settings.data_dir / "albums-made.json").write_text("{}")
        for model in teddycloud.custom_models:
            model["episodes"], model["tracks"] = "anders", ["x", "y"]

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("schon encodiert und auf einem Sticker") == 2

    def test_a_taf_with_other_chapters_is_not_taken_for_the_album(self, client, runner, teddycloud):
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = _batch_ids(_upload_series(client, episodes=2))
        _run_batch(client, runner, ids, series="Serie")
        for header in teddycloud.taf_headers.values():
            header["trackSeconds"] = [1, 2, 3, 4, 5]

        page = client.get("/batch", params={"ids": ids}).text

        assert "nur noch einen Sticker" not in page
        assert page.count("schon encodiert (liegt in der Library), aber kein Sticker spielt es") == 2

    def test_episode_ten_comes_after_episode_two(self, client):
        files = [("files", (f"Serie/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (10, 2, 1)]
        ids = _batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.index('value="Folge 1"') < page.index('value="Folge 2"') < page.index('value="Folge 10"')

    def test_the_folder_holding_the_episodes_is_the_suggested_series(self, client):
        ids = _batch_ids(_upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count('name="series" value="Serie"') == 2

    def test_a_deeper_pick_keeps_only_series_and_episode(self, client):
        files = [("files", (f"Kinder/Serie/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (1, 2)]
        ids = _batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids})

        assert page.status_code == 200
        assert page.text.count('name="series" value="Serie"') == 2

    def test_albums_of_different_series_keep_their_own(self, client, runner, teddycloud):
        files = [("files", (f"{series}/Folge 1/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n, series in enumerate(("Bobo", "Lichterkinder"), 1)]
        ids = _batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids}).text
        _run_batch(client, runner, ids)

        assert 'value="Bobo"' in page and 'value="Lichterkinder"' in page
        sources = sorted(entry["source"] for entry in teddycloud.tag_entries.values())
        assert sources == ["lib://Bobo/Folge 1.taf", "lib://Lichterkinder/Folge 1.taf"]

    def test_a_skipped_album_still_sends_its_names(self, client, runner):
        # A disabled field is not submitted, which would hand the next album
        # the skipped one's name; read-only fields are.
        ids = _batch_ids(_upload_series(client, episodes=2))
        _run_batch(client, runner, ids, series="Serie")

        page = client.get("/batch", params={"ids": ids}).text
        rows = page.split('<div class="list">')[1]

        assert rows.count('name="episodes"') == 2 and rows.count("readonly") == 4
        assert 'name="episodes" value="Folge 1"' in rows and "disabled>" not in rows.split("<button")[0]

    def test_an_album_that_already_has_a_tonie_is_skipped(self, client, runner, teddycloud):
        # Made back then under names typed by hand, so its taf path differs
        # from what the batch would suggest; the tonie is what gives it away.
        ids = _batch_ids(_upload_series(client, episodes=2))
        _run_batch(client, runner, ids, series="Serie")
        from tonie_studio.encode import delete_taf, taf_exists

        for job in runner.all():
            delete_taf(teddycloud, job.label)
            assert not taf_exists(teddycloud, job.label)
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("hat schon einen Tonie") == 2
        assert "Nichts Neues zu encodieren" in page

    def test_says_before_the_click_that_stickers_are_written_at_once(self, client):
        ids = _batch_ids(_upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert "ohne weitere R\u00fcckfrage" in page
        assert "2 Alben encodieren und auf Sticker legen" in page
        assert "Freie Sticker sehen gleich aus" in page

    def test_the_button_does_not_promise_stickers_when_none_are_free(self, client, teddycloud):
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = _batch_ids(_upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert "2 Alben encodieren" in page and "auf Sticker legen" not in page
        assert "Freie Sticker sehen gleich aus" not in page


def _start_placing(client, runner, episodes=2):
    ids = _batch_ids(_upload_series(client, episodes=episodes))
    response = client.post("/batch", data={"ids": ids, "series": ["Serie"] * len(ids),
                                           "stickers": "place"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[1]


def _place(client, group, ruid):
    response = client.post(f"/api/batch/{group}/place", data={"ruid": ruid})
    assert response.status_code == 200, response.text
    return response.json()


class TestPlacingStickers:
    def test_nothing_is_assigned_until_a_sticker_is_placed(self, client, runner, teddycloud):
        group = _start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        assert all(not entry["source"] for entry in teddycloud.tag_entries.values())
        assert "Leg einen freien Sticker auf die Box" in client.get(f"/parts/{group}").text

    def test_each_placed_sticker_gets_the_next_episode(self, client, runner, teddycloud):
        group = _start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        first = _place(client, group, "deb4ed0e500304e0")
        second = _place(client, group, "7ab4ed0e500304e0")

        assert first["status"] == "assigned" and first["episode"] == "Folge 1"
        assert second["status"] == "assigned" and second["episode"] == "Folge 2"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Serie/Folge 2.taf"

    def test_a_sticker_placed_during_the_encode_is_filled_when_it_finishes(self, client, runner,
                                                                          teddycloud):
        import threading

        import tonie_studio.pages.common as common

        gate = threading.Event()
        original = common.encode_files

        def slow(*args, **kwargs):
            assert gate.wait(5)
            return original(*args, **kwargs)

        with patch("tonie_studio.pages.common.encode_files", side_effect=slow):
            group = _start_placing(client, runner)
            placed = _place(client, group, "deb4ed0e500304e0")
            assert placed["status"] == "reserved" and placed["episode"] == "Folge 1"
            gate.set()
            for job in runner.all():
                assert job.done.wait(5)

        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"

    def test_the_sticker_still_on_the_box_is_not_given_a_second_episode(self, client, runner,
                                                                         teddycloud):
        group = _start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        _place(client, group, "deb4ed0e500304e0")
        again = _place(client, group, "deb4ed0e500304e0")

        assert again["status"] == "assigned" and again["episode"] == "Folge 1"
        assert not teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]

    def test_a_sticker_that_plays_something_is_left_alone(self, client, runner, teddycloud):
        teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
        group = _start_placing(client, runner)

        answer = _place(client, group, "deb4ed0e500304e0")

        assert answer["status"] == "taken"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Andere/Folge.taf"

    def test_a_sticker_teddycloud_has_not_filed_yet_is_waited_for(self, client, runner):
        group = _start_placing(client, runner)

        assert _place(client, group, "ffffffffffffffff")["status"] == "unknown"

    def test_says_when_every_episode_has_its_sticker(self, client, runner, teddycloud):
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")
        group = _start_placing(client, runner, episodes=2)
        for job in runner.all():
            assert job.done.wait(5)
        _place(client, group, "deb4ed0e500304e0")
        _place(client, group, "7ab4ed0e500304e0")

        answer = _place(client, group, "aaaaaaaaaaaaaaaa")

        assert answer["status"] == "complete"
        assert not teddycloud.tag_entries["aaaaaaaaaaaaaaaa"]["source"]

    def test_the_batch_page_offers_both_ways(self, client):
        ids = _batch_ids(_upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert 'name="stickers" value="auto"' in page and 'name="stickers" value="place"' in page

    def test_an_unknown_batch_is_refused(self, client):
        assert client.post("/api/batch/nope/place", data={"ruid": "x"}).status_code == 404

    def test_an_encode_done_before_the_batch_is_set_up_still_gets_its_sticker(
            self, client, runner, teddycloud):
        # Each encode finishes before the next is even queued, so the first
        # ones settle before the request has registered the batch.
        original = runner.submit

        def submit_and_finish(*args, **kwargs):
            job_id = original(*args, **kwargs)
            assert runner.get(job_id).done.wait(5)
            return job_id

        runner.submit = submit_and_finish
        group = _start_placing(client, runner)

        answer = _place(client, group, "deb4ed0e500304e0")

        assert answer["status"] == "assigned"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"


class TestNavigation:
    def test_making_tonies_is_in_view_and_the_rest_in_two_menus(self, client):
        page = client.get("/").text
        header = page[page.index("<header"):page.index("</header>")]

        assert header.count('<details class="menu">') == 2
        assert header.index('href="/sheet"') < header.index("<details")
        assert all(f'href="{path}"' in header for path in
                   ("/", "/tonies", "/sheet", "/radio", "/podcasts", "/listening",
                    "/library", "/checkup", "/backup"))

    def test_marks_the_page_and_its_menu(self, client):
        page = client.get("/backup").text
        header = page[page.index("<header"):page.index("</header>")]

        assert '<summary aria-current="true">Pflege</summary>' in header
        assert 'href="/backup" aria-current="page"' in header
        assert header.count("aria-current") == 2

    def test_an_album_page_counts_as_albums(self, client):
        page = client.get(f"/album/{_album_id(client)}").text

        assert 'href="/" aria-current="page"' in page


class TestWalkthrough:
    """One path for a new tonie: choose, check, encode, sticker, print."""

    def test_an_album_in_the_list_leads_to_checking_it(self, client):
        identifier = _album_id(client)

        assert f'href="/batch?ids={identifier}"' in client.get("/").text

    def test_the_detailed_album_page_is_one_click_away(self, client):
        identifier = _album_id(client)

        page = client.get("/batch", params={"ids": [identifier]}).text

        assert f'href="/album/{identifier}"' in page
        assert client.get(f"/album/{identifier}").status_code == 200

    def test_one_uploaded_folder_goes_to_checking_too(self, client):
        response = client.post("/upload", files=[
            ("files", ("Album/01-eins.mp3", b"\x01" * 8, "audio/mpeg")),
        ], follow_redirects=False)

        assert response.headers["location"].startswith("/batch?ids=")

    def test_every_step_says_where_it_is(self, client, runner):
        identifier = _album_id(client)

        assert 'class="steps"' in client.get("/").text
        assert _current_step(client.get("/").text) == "1"
        assert _current_step(client.get("/batch", params={"ids": [identifier]}).text) == "2"
        group = _start_placing_archive(client, identifier)
        assert _current_step(client.get(f"/parts/{group}").text) in ("3", "4")

    def test_after_the_stickers_comes_printing_exactly_these(self, client, runner, teddycloud):
        ids = _batch_ids(_upload_series(client, episodes=2))
        response = client.post("/batch", data={"ids": ids, "series": ["Serie"] * 2,
                                               "stickers": "place"}, follow_redirects=False)
        group = response.headers["location"].rsplit("/", 1)[1]
        for job in runner.all():
            assert job.done.wait(5)
        _place(client, group, "deb4ed0e500304e0")
        _place(client, group, "7ab4ed0e500304e0")

        page = client.get(f"/parts/{group}").text
        models = sorted(model["model"] for model in teddycloud.custom_tonies())

        assert f'href="/sheet?model={models[0]}&amp;model={models[1]}"' in page
        assert models == [job["model"] for job in sorted(
            (client.get(f"/api/job/{job_id}").json() for job_id in _group_jobs(page)),
            key=lambda job: job["model"])]

    def test_the_sheet_takes_several_tonies_at_once(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]

        page = client.get("/sheet", params={"model": [model, "TS-9999"]}).text

        assert page.count('class="want" checked') == 1
        assert _current_step(page) == "5"

    def test_the_sheet_without_a_walkthrough_has_no_steps(self, client):
        assert 'class="steps"' not in client.get("/sheet").text

    def test_a_free_sticker_on_the_box_is_chosen_on_the_assignment_page(self, client, runner, box):
        job, _page = _encode_and_assign_page(client, runner)
        assert _checked_ruid(client.get(f"/job/{job.id}/assign").text) != "deb4ed0e500304e0"
        # The same sticker as uid; the tag index names it by its ruid.
        box.handle("TagValid", "E00403500EEDB4DE")

        page = client.get(f"/job/{job.id}/assign").text

        assert _checked_ruid(page) == "deb4ed0e500304e0"


def _current_step(page):
    marker = 'aria-current="step" data-step="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def _start_placing_archive(client, identifier):
    response = client.post("/batch", data={"ids": [identifier], "series": ["L"],
                                           "stickers": "place"}, follow_redirects=False)
    return response.headers["location"].rsplit("/", 1)[1]


def _group_jobs(page):
    import re

    return re.findall(r'data-job="([0-9a-f]+)"', page)


def _checked_ruid(page):
    import re

    for match in re.finditer(r'<input type="radio" name="ruid" value="([0-9a-f]+)"[^>]*>', page, re.S):
        if "checked" in match.group(0):
            return match.group(1)
    return None


class TestWalkthroughHints:
    def test_an_empty_album_list_says_why_and_what_to_do(self, settings, teddycloud, runner, tags, box,
                                                         tmp_path):
        import shutil

        shutil.rmtree(settings.archive_dir)
        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner, watcher=box)
        with TestClient(app) as empty:
            page = empty.get("/").text

        assert "Keine Alben gefunden" in page

    def test_the_album_list_explains_without_jargon(self, client):
        page = client.get("/").text
        intro = page[page.index('<p class="sub">'):page.index("</p>", page.index('<p class="sub">'))]

        assert "Taf" not in intro and "Library" not in intro

    def test_placing_says_there_is_no_need_to_wait(self, client, runner):
        ids = _batch_ids(_upload_series(client, episodes=2))
        response = client.post("/batch", data={"ids": ids, "series": ["S"] * 2, "stickers": "place"},
                               follow_redirects=False)

        page = client.get(response.headers["location"]).text

        assert "nicht warten" in page

    def test_the_last_way_of_handing_out_stickers_is_remembered(self, client, runner):
        ids = _batch_ids(_upload_series(client, episodes=2))
        assert 'value="place" checked' in client.get("/batch", params={"ids": ids}).text

        client.post("/batch", data={"ids": ids, "series": ["S"] * 2, "stickers": "auto"},
                    follow_redirects=False)
        page = client.get("/batch", params={"ids": ids}).text

        assert 'value="auto" checked' in page and 'value="place" checked' not in page

    def test_printing_for_the_first_time_points_at_the_calibration(self, client, runner, teddycloud):
        _publish(client, runner, teddycloud)

        assert "zum ersten Mal" in client.get("/sheet").text
        client.post("/sheet/calibration", data={"left_mm": "31.5", "top_mm": "50.5"})
        assert "zum ersten Mal" not in client.get("/sheet").text


class TestRecognisingTonies:
    def _album_with(self, client, *names):
        files = [("files", (f"Neu/Album/{n:02d}-{name}.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n, name in enumerate(names, 1)]
        response = client.post("/upload", files=files, follow_redirects=False)
        return response.headers["location"].split("ids=")[1]

    def test_an_album_whose_tracks_a_tonie_already_has_is_recognised(self, client, teddycloud):
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "Unter meinem Bett",
                                        "episodes": "Folge 1",
                                        "tracks": ["Der Tee", "Immer muss ich", "Sommer lang"]})
        identifier = self._album_with(client, "der_tee", "immer_muss_ich", "sommer_lang")

        page = client.get("/batch", params={"ids": [identifier]}).text

        assert "hat schon einen Tonie (TS-0042)" in page

    def test_one_track_left_out_still_counts(self, client, teddycloud):
        # Recorded without the advert at the start, as some tonies are.
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": ["Zwei", "Drei", "Vier", "Fuenf"]})
        identifier = self._album_with(client, "werbung", "zwei", "drei", "vier", "fuenf")

        assert "TS-0042" in client.get("/batch", params={"ids": [identifier]}).text

    def test_numbered_chapters_alone_prove_nothing(self, client, teddycloud):
        # Every audiobook calls its tracks "Kapitel 1" to "Kapitel n".
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": [f"Kapitel {n}" for n in range(1, 12)]})
        identifier = self._album_with(client, *[f"kapitel_{n}" for n in range(1, 14)])

        assert "TS-0042" not in client.get("/batch", params={"ids": [identifier]}).text

    def test_a_different_album_is_not_mistaken(self, client, teddycloud):
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": ["Eins", "Zwei", "Drei"]})
        identifier = self._album_with(client, "eins", "vier", "fuenf", "sechs")

        assert "TS-0042" not in client.get("/batch", params={"ids": [identifier]}).text


class TestLoadingHint:
    def test_choosing_albums_says_it_is_reading_them(self, client):
        page = client.get("/").text

        assert 'id="loading"' in page and "Lese" in page


class TestSpellingsOfAPath:
    def test_a_taf_played_under_the_other_spelling_of_an_umlaut_counts_as_played(
            self, client, runner, teddycloud):
        # A share hands back decomposed umlauts, teddyCloud keeps whatever it
        # was given: the same path can arrive in two spellings.
        import unicodedata

        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        files = [("files", (f"B\u00e4r/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (1, 2)]
        ids = _batch_ids(client.post("/upload", files=files, follow_redirects=False))
        _run_batch(client, runner, ids, series="B\u00e4r")
        sticker = next(iter(teddycloud.tag_entries))
        teddycloud.tag_entries[sticker]["source"] = unicodedata.normalize(
            "NFD", "lib://B\u00e4r/Folge 1.taf")

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("schon encodiert und auf einem Sticker") == 1
        assert page.count("schon encodiert, bekommt nur noch einen Sticker") == 1
