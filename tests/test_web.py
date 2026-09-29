"""Tests for the web layer.

The teddyCloud client is the fake, so an encode started from a page really
runs: it uploads, encodes and cleans up against the in-memory server, and the
test can look at what arrived.
"""

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

    Image.new("RGB", size, colour).save(path, format="JPEG")
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
    start = page.index('href="/album/') + len('href="/album/')
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
        import tonie_studio.web as web

        web._forget_catalogue()
        scans = []
        original = web._scan_albums
        monkeypatch.setattr(web, "_scan_albums",
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
        page = client.get(response.headers["location"]).text
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

        page = client.get(response.headers["location"]).text
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

        assert "1 von 2 Tags bespielt" in page
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

        import tonie_studio.web as web
        original = web.build_sheet
        try:
            web.build_sheet = lambda stickers, target: (
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
                   '{"function_group":15,"function":16067,"field6":"E00403500EEDC9A9"}')

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
