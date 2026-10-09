"""Tests for turning a list of audio files into one taf in the library.

The steps run against a fake teddyCloud that enforces the same rules as the
real one, so a test that passes here would not have been rejected there.
"""

import pytest
from fake_teddycloud import FakeTeddyCloud

from tonie_studio.encode import (STAGING_ROOT, EncodeError, delete_taf, encode_files, library_path,
                                 list_tafs, remove_orphaned_staging, taf_exists)
from tonie_studio.teddycloud import TeddyCloudError


@pytest.fixture
def teddycloud():
    return FakeTeddyCloud()


@pytest.fixture
def sources(tmp_path):
    """Three tracks on disk, distinguishable by their contents."""
    paths = []
    for index, name in enumerate(["03-third.flac", "01-first.flac", "02-second.mp3"], start=1):
        path = tmp_path / name
        path.write_bytes(bytes([index]) * 10)
        paths.append(path)
    return paths


class TestEncodeFiles:
    def test_produces_the_taf_at_the_requested_path(self, teddycloud, sources):
        result = encode_files(teddycloud, sources, "/Lichterkinder/Gemeinsam.taf")

        assert result.target == "/Lichterkinder/Gemeinsam.taf"
        assert "/Lichterkinder/Gemeinsam.taf" in teddycloud.files

    def test_keeps_the_order_it_was_given(self, teddycloud, sources):
        # The caller has already decided the track order; the file names must
        # not be allowed to reorder it behind its back.
        # The fixture is named so that alphabetical order would differ from the
        # order it is passed in. The fake concatenates what it encodes, so the
        # taf's contents show which order reached the encoder.
        encode_files(teddycloud, sources, "/a.taf")

        assert teddycloud.files["/a.taf"] == bytes([1]) * 10 + bytes([2]) * 10 + bytes([3]) * 10

    def test_stages_under_names_that_cannot_be_misread(self, teddycloud, sources):
        # A name travels through a multipart header, a urlencoded form and two
        # filesystems. Numbering them removes every question of encoding, and
        # the extension stays because ffmpeg picks its decoder by it.
        encode_files(teddycloud, sources, "/a.taf")

        _, encoded = teddycloud.encoded[0]
        assert [source.rsplit("/", 1)[1] for source in encoded] == [
            "001.flac", "002.flac", "003.mp3",
        ]

    def test_creates_the_folder_the_taf_goes_into(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/Serie/Folge/Teil.taf")

        assert "/Serie/Folge" in teddycloud.dirs

    def test_reports_the_identity_teddycloud_gave_the_audio(self, teddycloud, sources):
        # audio_id and hash are what links a custom model to this taf, and
        # teddyCloud invents the audio_id during the encode, so it can only be
        # read back afterwards.
        result = encode_files(teddycloud, sources, "/a.taf")

        assert result.audio_id == 445848115
        assert result.sha1 == "0d7e5ac7de7b3cdfa865f97081225e26cf6c39d2"

    def test_reports_the_source_path_for_the_tag_entry(self, teddycloud, sources):
        result = encode_files(teddycloud, sources, "/Serie/Folge.taf")

        assert result.source == "lib://Serie/Folge.taf"


class TestStaging:
    def test_leaves_nothing_behind(self, teddycloud, sources):
        # The staging root itself stays: it is one obvious folder rather than a
        # create and delete around every job, and it is where an interrupted
        # run's leftovers are looked for.
        encode_files(teddycloud, sources, "/a.taf")

        assert teddycloud.dirs == {"/", STAGING_ROOT}
        assert not [path for path in teddycloud.files if path.startswith(STAGING_ROOT)]

    def test_cleans_up_when_the_encode_fails(self, teddycloud, sources):
        teddycloud.encode_fails = "ffmpeg died"

        with pytest.raises(TeddyCloudError):
            encode_files(teddycloud, sources, "/a.taf")

        assert not [path for path in teddycloud.files if path.startswith(STAGING_ROOT)]
        assert not [path for path in teddycloud.dirs if path.startswith(f"{STAGING_ROOT}/")]

    def test_gives_every_job_its_own_folder(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/a.taf")
        encode_files(teddycloud, sources, "/b.taf")

        assert teddycloud.encoded[0][1][0] != teddycloud.encoded[1][1][0]

    def test_survives_a_staging_root_that_already_exists(self, teddycloud, sources):
        teddycloud.create_dir(STAGING_ROOT)

        encode_files(teddycloud, sources, "/a.taf")

        assert "/a.taf" in teddycloud.files


class TestRefusals:
    def test_refuses_to_overwrite_an_existing_taf(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"

        with pytest.raises(EncodeError) as excinfo:
            encode_files(teddycloud, sources, "/a.taf")

        assert "exists" in str(excinfo.value)

    def test_uploads_nothing_when_it_refuses(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"

        with pytest.raises(EncodeError):
            encode_files(teddycloud, sources, "/a.taf")

        assert list(teddycloud.files) == ["/a.taf"]

    def test_refuses_an_empty_track_list(self, teddycloud):
        with pytest.raises(EncodeError):
            encode_files(teddycloud, [], "/a.taf")

    def test_refuses_a_target_that_is_not_a_taf(self, teddycloud, sources):
        with pytest.raises(EncodeError):
            encode_files(teddycloud, sources, "/a.mp3")

    def test_refuses_a_source_that_is_not_there(self, teddycloud, sources, tmp_path):
        with pytest.raises(EncodeError) as excinfo:
            encode_files(teddycloud, sources + [tmp_path / "gone.flac"], "/a.taf")

        assert "gone.flac" in str(excinfo.value)


class TestProgress:
    def test_reports_each_upload_and_the_encode(self, teddycloud, sources):
        seen = []

        encode_files(teddycloud, sources, "/a.taf", progress=lambda *args: seen.append(args))

        assert [step for step, _, _ in seen] == ["upload", "upload", "upload", "encode"]
        assert seen[0][1:] == (1, 3)
        assert seen[-1][1:] == (3, 3)


class TestRemoveOrphanedStaging:
    def test_removes_what_an_interrupted_run_left(self, teddycloud):
        teddycloud.create_dir(STAGING_ROOT)
        teddycloud.create_dir(f"{STAGING_ROOT}/abandoned")
        teddycloud.files[f"{STAGING_ROOT}/abandoned/001.flac"] = b"x"

        assert remove_orphaned_staging(teddycloud) == 1

        assert teddycloud.dirs == {"/", STAGING_ROOT}
        assert teddycloud.files == {}

    def test_ignores_the_parent_link_teddycloud_lists(self, teddycloud):
        # The listing contains "..", and deleting that would walk out of the
        # staging area into the library.
        teddycloud.create_dir(STAGING_ROOT)
        teddycloud.create_dir("/Lichterkinder")

        remove_orphaned_staging(teddycloud)

        assert "/Lichterkinder" in teddycloud.dirs

    def test_does_nothing_when_no_staging_area_exists(self, teddycloud):
        assert remove_orphaned_staging(teddycloud) == 0


class TestLibraryPath:
    def test_builds_a_path_from_series_and_episode(self):
        assert library_path("Lichterkinder", "Gemeinsam sind wir stark") == (
            "/Lichterkinder/Gemeinsam sind wir stark.taf"
        )

    def test_puts_a_taf_without_a_series_at_the_top(self):
        assert library_path("", "Gemeinsam") == "/Gemeinsam.taf"

    def test_keeps_the_umlauts_the_library_already_holds(self):
        assert library_path("", "Bobo Siebenschl\u00e4fer") == "/Bobo Siebenschl\u00e4fer.taf"

    def test_turns_a_separator_into_something_harmless(self):
        assert library_path("A/B", "C\\D") == "/A-B/C-D.taf"

    def test_collapses_whitespace_and_trims(self):
        assert library_path("  ", " Gemeinsam   sind  wir ") == "/Gemeinsam sind wir.taf"

    def test_refuses_an_empty_episode(self):
        with pytest.raises(EncodeError):
            library_path("Lichterkinder", "   ")


class TestReplacing:
    def test_reports_whether_a_taf_is_in_the_way(self, teddycloud):
        teddycloud.files["/a.taf"] = b"older"

        assert taf_exists(teddycloud, "/a.taf") is True
        assert taf_exists(teddycloud, "/b.taf") is False

    def test_replaces_the_old_taf_when_asked(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.files["/a.taf"] != b"older"

    def test_still_refuses_without_being_asked(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"

        with pytest.raises(EncodeError):
            encode_files(teddycloud, sources, "/a.taf")

        assert teddycloud.files["/a.taf"] == b"older"


class TestEncodeProgress:
    def test_reports_the_playing_time_rather_than_a_file_count(self, teddycloud, sources):
        seen = []

        encode_files(teddycloud, sources, "/a.taf",
                     progress=lambda *args: seen.append(args), total_seconds=600)

        assert seen[-1] == ("encode", 600, 600)

    def test_turns_a_growing_taf_into_a_position(self, teddycloud):
        import threading

        from tonie_studio.encode import ENCODE_BYTES_PER_SECOND, _watch_growth

        teddycloud.files["/a.taf"] = b"x" * int(ENCODE_BYTES_PER_SECOND * 120)
        seen = []
        stop = threading.Event()

        import tonie_studio.encode as encode_module
        original = encode_module.GROWTH_POLL
        encode_module.GROWTH_POLL = 0.01
        try:
            thread = threading.Thread(
                target=_watch_growth,
                args=(teddycloud, "/a.taf", 600.0, lambda *a: seen.append(a), stop))
            thread.start()
            _eventually(lambda: seen)
            stop.set()
            thread.join(2)
        finally:
            encode_module.GROWTH_POLL = original

        assert seen[0] == ("encode", 120, 600)

    def test_never_claims_to_be_finished(self, teddycloud):
        # The encode says when it is done. A bar sitting at 100 percent while
        # ffmpeg still runs is worse than one that stops a second short.
        import threading

        from tonie_studio.encode import ENCODE_BYTES_PER_SECOND, _watch_growth

        teddycloud.files["/a.taf"] = b"x" * int(ENCODE_BYTES_PER_SECOND * 9999)
        seen = []
        stop = threading.Event()

        import tonie_studio.encode as encode_module
        encode_module.GROWTH_POLL = 0.01
        try:
            thread = threading.Thread(
                target=_watch_growth,
                args=(teddycloud, "/a.taf", 600.0, lambda *a: seen.append(a), stop))
            thread.start()
            _eventually(lambda: seen)
            stop.set()
            thread.join(2)
        finally:
            encode_module.GROWTH_POLL = 3.0

        assert seen[0] == ("encode", 599, 600)

    def test_says_nothing_without_a_playing_time(self, teddycloud, sources):
        seen = []

        encode_files(teddycloud, sources, "/a.taf", progress=lambda *args: seen.append(args))

        assert [step for step, _, _ in seen] == ["upload", "upload", "upload", "encode"]


def _eventually(condition, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


class TestStagingRootAbsent:
    def test_asks_the_parent_instead_of_the_missing_folder(self, teddycloud):
        # teddyCloud answers a listing of a directory that is not there by
        # closing the connection, not with a 404. Asking for it fills the log
        # with warnings about a server that is fine.
        asked = []
        original = teddycloud.list_dir
        teddycloud.list_dir = lambda path="/", special=None: (
            asked.append(path) or original(path, special))

        assert remove_orphaned_staging(teddycloud) == 0

        assert STAGING_ROOT not in asked
        assert "/" in asked

    def test_still_clears_it_when_it_is_there(self, teddycloud):
        teddycloud.create_dir(STAGING_ROOT)
        teddycloud.create_dir(f"{STAGING_ROOT}/abandoned")

        assert remove_orphaned_staging(teddycloud) == 1


class TestLibraryListing:
    def test_finds_tafs_in_every_series_folder(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/Lichterkinder/Eins.taf")
        encode_files(teddycloud, sources, "/Zwei.taf")

        found = [taf.path for taf in list_tafs(teddycloud)]

        assert found == ["/Lichterkinder/Eins.taf", "/Zwei.taf"]

    def test_reports_what_a_taf_is(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/Eins.taf")

        taf = list_tafs(teddycloud)[0]

        assert taf.source == "lib://Eins.taf"
        assert taf.name == "Eins"
        assert taf.chapters == 3
        assert taf.size > 0

    def test_leaves_the_staging_area_out_of_it(self, teddycloud, sources):
        teddycloud.create_dir(STAGING_ROOT)
        teddycloud.files[f"{STAGING_ROOT}/leftover.taf"] = b"x"
        encode_files(teddycloud, sources, "/Echt.taf")

        assert [taf.name for taf in list_tafs(teddycloud)] == ["Echt"]

    def test_deletes_one(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/Weg.taf")

        delete_taf(teddycloud, "/Weg.taf")

        assert list_tafs(teddycloud) == []

    def test_refuses_to_delete_something_that_is_not_a_taf(self, teddycloud):
        teddycloud.files["/wichtig.json"] = b"{}"

        with pytest.raises(EncodeError):
            delete_taf(teddycloud, "/wichtig.json")

        assert "/wichtig.json" in teddycloud.files
