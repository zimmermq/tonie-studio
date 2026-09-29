"""Tests for turning a list of audio files into one taf in the library.

The steps run against a fake teddyCloud that enforces the same rules as the
real one, so a test that passes here would not have been rejected there.
"""

import pytest

from fake_teddycloud import FakeTeddyCloud
from tonie_studio.encode import (
    STAGING_ROOT,
    EncodeError,
    encode_files,
    library_path,
    remove_orphaned_staging,
)
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
