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
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    return tmp_path / "archive"


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
def client(settings, teddycloud, runner, tags):
    app = create_app(settings, client_factory=lambda: teddycloud, runner=runner)
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
    def test_lists_the_album_in_the_archive(self, client):
        page = client.get("/").text

        assert "Lichterkinder-Halloween-WEB-FLAC-2023-WALKMAN" in page

    def test_says_how_many_tracks_it_has(self, client):
        assert "3 Tracks" in client.get("/").text

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
        assert response.content == b"\xff\xd8\xff"

    def test_refuses_an_id_that_names_nothing(self, client):
        assert client.get("/album/a0000deadbeef").status_code == 404


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

        assert "Hochgeladen" in client.get("/").text


class TestStartup:
    def test_clears_staging_that_an_earlier_run_abandoned(self, settings, teddycloud, runner, tags):
        teddycloud.create_dir("/_tonie-studio-staging")
        teddycloud.create_dir("/_tonie-studio-staging/abandoned")
        teddycloud.files["/_tonie-studio-staging/abandoned/001.flac"] = b"x"

        app = create_app(settings, client_factory=lambda: teddycloud, runner=runner)
        with TestClient(app) as started:
            _eventually(lambda: teddycloud.files == {})
            assert started.get("/health").status_code == 200

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

        assert list(teddycloud.images.values()) == [b"\xff\xd8\xff"]

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
        assert response.content == b"\xff\xd8\xff"

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
