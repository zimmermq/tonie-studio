"""Tests for turning a list of audio files into one taf in the library.

The steps run against a fake teddyCloud that enforces the same rules as the
real one, so a test that passes here would not have been rejected there.
"""

import pytest
from web_helpers import eventually
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

    def test_a_cleanup_that_fails_does_not_hide_why_the_encode_failed(self, teddycloud, sources):
        # The cleanup runs after the failure; its own trouble is logged, and
        # the error that reaches the page is still the one worth knowing.
        teddycloud.encode_fails = "ffmpeg died"

        def refuse(path, special=None):
            raise TeddyCloudError("teddyCloud refused /api/fileDelete", refused=True)

        teddycloud.delete = refuse

        with pytest.raises(TeddyCloudError) as raised:
            encode_files(teddycloud, sources, "/a.taf")

        assert "ffmpeg died" in str(raised.value)

    def test_a_progress_report_that_fails_does_not_fail_the_encode(self, teddycloud, sources):
        def broken(step, done, total):
            raise RuntimeError("the page went away")

        result = encode_files(teddycloud, sources, "/a.taf", progress=broken)

        assert result.target == "/a.taf" and "/a.taf" in teddycloud.files

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

    def test_spares_what_an_encode_of_this_run_is_using(self, teddycloud, tmp_path):
        # The sweep runs once teddyCloud answers, which can be after the first
        # encode has begun uploading into its own staging folder.
        import threading

        from tonie_studio.encode import encode_files

        source = tmp_path / "01.flac"
        source.write_bytes(b"x")
        uploading, swept = threading.Event(), threading.Event()
        original = teddycloud.upload

        def upload(*args, **kwargs):
            uploading.set()
            swept.wait(5)
            return original(*args, **kwargs)

        teddycloud.upload = upload
        teddycloud.create_dir("/Neu")
        worker = threading.Thread(target=encode_files, args=(teddycloud, [source], "/Neu/X.taf"))
        worker.start()
        assert uploading.wait(5)

        removed = remove_orphaned_staging(teddycloud)
        swept.set()
        worker.join(5)

        assert removed == 0
        assert "/Neu/X.taf" in teddycloud.files

    def test_does_nothing_when_no_staging_area_exists(self, teddycloud):
        assert remove_orphaned_staging(teddycloud) == 0


class TestLibraryPath:
    def test_builds_a_path_from_series_and_episode(self):
        assert library_path("Lichterkinder", "Gemeinsam sind wir stark") == (
            "/Lichterkinder/Gemeinsam sind wir stark.taf"
        )

    def test_puts_a_taf_without_a_series_at_the_top(self):
        assert library_path("", "Gemeinsam") == "/Gemeinsam.taf"

    def test_a_name_of_wide_characters_stays_inside_the_file_name_limit(self):
        path = library_path("", "\U0001F984" * 100)

        name = path.rsplit("/", 1)[-1]
        assert len(name.encode("utf-8")) <= 255
        assert name.endswith(".taf") and "\ufffd" not in name

    def test_a_latin_name_is_cut_where_it_always_was(self):
        assert library_path("", "a" * 150) == "/" + "a" * 120 + ".taf"

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


class TestReplacingCarriesTheModelAlong:
    """A model names its audio by audio_id and hash; new audio has new ones."""

    OLD = {"audioId": 111, "sha1Hash": "aa" * 20, "valid": True}

    def _models(self, teddycloud):
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.taf_headers["/a.taf"] = dict(self.OLD)
        teddycloud.custom_models += [
            {"model": "TS-0001", "series": "S", "episodes": "E",
             "audio_id": [111], "hash": ["AA" * 20]},
            {"model": "TS-0002", "series": "S", "episodes": "F",
             "audio_id": [222], "hash": ["BB" * 20]},
        ]

    def test_the_model_of_the_old_audio_names_the_new(self, teddycloud, sources):
        # Left on the old pair, teddyCloud's library no longer matches the new
        # taf to its tonie, and neither does find_model_by_audio.
        self._models(teddycloud)

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        new = teddycloud.taf_headers["/a.taf"]
        model = next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")
        assert model["audio_id"] == [new["audioId"]]
        assert model["hash"] == [new["sha1Hash"].upper()]

    def test_other_models_are_left_alone(self, teddycloud, sources):
        self._models(teddycloud)

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        other = next(m for m in teddycloud.custom_models if m["model"] == "TS-0002")
        assert (other["audio_id"], other["hash"]) == ([222], ["BB" * 20])

    def test_a_model_of_several_parts_keeps_the_others(self, teddycloud, sources):
        self._models(teddycloud)
        teddycloud.custom_models[0].update(audio_id=[111, 333], hash=["AA" * 20, "CC" * 20])

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        new = teddycloud.taf_headers["/a.taf"]
        first = next(m for m in teddycloud.custom_models if m["model"] == "TS-0001")
        assert first["audio_id"] == [new["audioId"], 333]
        assert first["hash"] == [new["sha1Hash"].upper(), "CC" * 20]


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
            eventually(lambda: seen)
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
            eventually(lambda: seen)
            stop.set()
            thread.join(2)
        finally:
            encode_module.GROWTH_POLL = 3.0

        assert seen[0] == ("encode", 599, 600)

    def test_says_nothing_without_a_playing_time(self, teddycloud, sources):
        seen = []

        encode_files(teddycloud, sources, "/a.taf", progress=lambda *args: seen.append(args))

        assert [step for step, _, _ in seen] == ["upload", "upload", "upload", "encode"]



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

    def test_knows_where_each_chapter_starts_and_its_title(self, teddycloud):
        teddycloud.files["/Eins.taf"] = b"taf"
        teddycloud.taf_headers["/Eins.taf"] = {"valid": True, "trackSeconds": [0, 95, 230]}
        teddycloud.taf_info["/Eins.taf"] = {"model": "10000001", "tracks": ["A", "B", "C"]}

        taf = list_tafs(teddycloud)[0]

        assert (taf.starts, taf.tracks) == ([0, 95, 230], ["A", "B", "C"])

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


class TestAFailedListingIsNotAMissingFile:
    def test_teddycloud_failing_to_list_is_passed_on(self, teddycloud):
        # Read as "no taf", a hiccup would plan a rebuild of a tonie that plays
        # fine, or refuse a publish saying the audio is gone.
        teddycloud.create_dir("/Lichterkinder")
        teddycloud.files["/Lichterkinder/Halloween.taf"] = b"taf"
        original = teddycloud.list_dir

        def hiccup(path, special=None):
            if path == "/Lichterkinder":
                raise TeddyCloudError("teddyCloud at x is unreachable")
            return original(path, special=special)

        teddycloud.list_dir = hiccup

        with pytest.raises(TeddyCloudError):
            taf_exists(teddycloud, "/Lichterkinder/Halloween.taf")

    def test_a_folder_that_is_not_there_is_still_a_plain_no(self, teddycloud):
        assert not taf_exists(teddycloud, "/Gibtsnicht/Halloween.taf")


class TestReplacingKeepsTheOldUntilTheNewIsThere:
    def test_a_failed_encode_leaves_the_old_taf(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.encode_fails = "ffmpeg died"

        with pytest.raises(TeddyCloudError):
            encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.files["/a.taf"] == b"older"

    def test_nothing_is_left_beside_it_afterwards(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert [path for path in teddycloud.files if path.endswith(".taf")] == ["/a.taf"]

    def test_if_only_the_final_encode_fails_the_stickers_play_the_new_one(self, teddycloud, sources):
        # Old taf already gone, new one under its temporary name: a sticker
        # that played the old one would otherwise play nothing.
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag("7ab4ed0e500304e0", source="lib://a.taf")
        original = teddycloud.encode

        def final_fails(target, sources, special="library"):
            if target == "/a.taf":
                raise TeddyCloudError("ffmpeg died")
            return original(target, sources, special)

        teddycloud.encode = final_fails

        with pytest.raises(EncodeError):
            encode_files(teddycloud, sources, "/a.taf", replace=True)

        kept = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]
        assert kept != "lib://a.taf" and taf_exists(teddycloud, "/" + kept[len("lib://"):])


    @pytest.mark.parametrize("dies_at", ["delete", "final_encode"])
    def test_a_process_killed_half_way_leaves_every_sticker_playing(self, teddycloud, sources,
                                                                     dies_at):
        # A rollout kills a running replace. Whatever moment it hits, every
        # sticker has to point at audio that exists, or the checkup offers to
        # empty it and to delete the only copy as a leftover.
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag("7ab4ed0e500304e0", source="lib://a.taf")
        original_delete, original_encode = teddycloud.delete, teddycloud.encode

        class Killed(BaseException):
            pass

        def delete(path, special=None):
            original_delete(path, special=special)
            if dies_at == "delete" and path == "/a.taf":
                raise Killed()

        def encode(target, sources, special="library"):
            if dies_at == "final_encode" and target == "/a.taf":
                raise Killed()
            return original_encode(target, sources, special)

        teddycloud.delete, teddycloud.encode = delete, encode

        with pytest.raises(Killed):
            encode_files(teddycloud, sources, "/a.taf", replace=True)

        playing = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]
        assert taf_exists(teddycloud, "/" + playing[len("lib://"):])


class TestThreadsKeepTheirOwnClient:
    """A requests.Session is not shared between the threads an encode runs on."""

    def test_the_growth_watcher_asks_through_a_twin(self, teddycloud, sources):
        twins = []
        teddycloud.twin = lambda: twins.append(1) or teddycloud

        encode_files(teddycloud, sources, "/a.taf", progress=lambda *args: None,
                     total_seconds=600)

        assert twins == [1]

    def test_an_encode_job_gets_its_client_when_it_runs(self, teddycloud, sources):
        from tonie_studio.album import Track
        from tonie_studio.publishing import Details, encode_job

        made = []
        tracks = [(Track(number=n, path=path, title=path.stem), path.stem)
                  for n, path in enumerate(sources, 1)]
        work = encode_job(lambda: made.append(1) or teddycloud, tracks, "/a.taf",
                          Details(series="S", episode="E"))

        assert made == []
        work(lambda *args: None)
        assert made == [1]


class TestChaptersOfOneFile:
    """A track that is a chapter of a longer file is cut out before the upload."""

    def _cutting(self, monkeypatch, cuts):
        def cut(source, start, end, target, **kwargs):
            cuts.append((source.name, start, end, target.suffix))
            target.write_bytes(f"{start}-{end}".encode())
        monkeypatch.setattr("tonie_studio.splitting.cut", cut)

    def test_each_chapter_is_uploaded_as_its_own_file(self, teddycloud, tmp_path, monkeypatch):
        from tonie_studio.album import Track
        from tonie_studio.publishing import Details, encode_job

        book = tmp_path / "buch.m4b"
        book.write_bytes(b"the whole book")
        cuts, uploaded = [], []
        self._cutting(monkeypatch, cuts)
        upload = teddycloud.upload

        def watching(stream, filename, path="/", special=None):
            data = stream.read()
            uploaded.append(data)
            import io
            return upload(io.BytesIO(data), filename, path=path, special=special)
        teddycloud.upload = watching
        tracks = [(Track(path=book, number=1, title="Eins", start=0, end=600), "Eins"),
                  (Track(path=book, number=2, title="Zwei", start=600, end=None), "Zwei")]

        outcome = encode_job(lambda: teddycloud, tracks, "/Buch.taf",
                             Details(series="S", episode="E"))(lambda *args: None)

        assert cuts == [("buch.m4b", 0, 600, ".m4a"), ("buch.m4b", 600, None, ".m4a")]
        assert uploaded == [b"0-600", b"600-None"]
        assert outcome.tracks == ["Eins", "Zwei"]

    def test_the_cut_files_are_gone_afterwards(self, teddycloud, tmp_path, monkeypatch):
        from tonie_studio.album import Track
        from tonie_studio.publishing import Details, encode_job

        book = tmp_path / "buch.mp3"
        book.write_bytes(b"book")
        made = []

        def cut(source, start, end, target, **kwargs):
            made.append(target)
            target.write_bytes(b"part")
        monkeypatch.setattr("tonie_studio.splitting.cut", cut)
        tracks = [(Track(path=book, number=1, title="Eins", start=0, end=60), "Eins")]

        encode_job(lambda: teddycloud, tracks, "/Buch.taf",
                   Details(series="S", episode="E"))(lambda *args: None)

        assert made and not any(path.exists() for path in made)

    def test_whole_files_are_not_cut(self, teddycloud, sources, monkeypatch):
        from tonie_studio.album import Track
        from tonie_studio.publishing import Details, encode_job

        cuts = []
        self._cutting(monkeypatch, cuts)
        tracks = [(Track(number=n, path=path, title=path.stem), path.stem)
                  for n, path in enumerate(sources, 1)]

        encode_job(lambda: teddycloud, tracks, "/a.taf",
                   Details(series="S", episode="E"))(lambda *args: None)

        assert cuts == []


class TestTheCopyBesideIsNeverSomeoneElses:
    def test_a_copy_an_earlier_replace_left_playing_survives_the_next(self, teddycloud, sources):
        # A replace whose final encode failed leaves the stickers on its copy;
        # trying again must not delete that copy before anything plays instead.
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.files["/a.new.taf"] = b"what the stickers play"
        teddycloud.add_tag("7ab4ed0e500304e0", source="lib://a.new.taf")

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.files["/a.new.taf"] == b"what the stickers play"
        assert sorted(path for path in teddycloud.files if path.endswith(".taf")) == [
            "/a.new.taf", "/a.taf"]


class TestMissingFoldersAreNotAskedFor:
    """teddyCloud closes the connection when asked to list a folder that is not there."""

    def _listed(self, teddycloud):
        asked = []
        original = teddycloud.list_dir

        def listing(path="/", special=None):
            asked.append(path)
            return original(path, special)

        teddycloud.list_dir = listing
        return asked

    def test_a_taf_in_a_missing_folder_is_known_absent_from_its_parent(self, teddycloud):
        asked = self._listed(teddycloud)

        assert not taf_exists(teddycloud, "/Andy Weir/Der Astronaut - Teil 1 von 3.taf")
        assert "/Andy Weir" not in asked

    def test_nested_folders_are_checked_from_the_top(self, teddycloud):
        teddycloud.create_dir("/Serie")
        asked = self._listed(teddycloud)

        assert not taf_exists(teddycloud, "/Serie/Staffel 2/Folge.taf")
        assert "/Serie/Staffel 2" not in asked

    def test_a_taf_that_is_there_is_still_found(self, teddycloud, sources):
        encode_files(teddycloud, sources, "/Lichterkinder/Eins.taf")

        assert taf_exists(teddycloud, "/Lichterkinder/Eins.taf")


S1, S2 = "7ab4ed0e500304e0", "7ab4ed0e500304e1"


def _during_the_final_encode(teddycloud, target, action):
    """Run ``action`` while the replace encodes ``target`` again under its own name."""
    original = teddycloud.encode

    def encode(path, sources, special="library"):
        if path == target:
            action()
        return original(path, sources, special)

    teddycloud.encode = encode


def _plays_audio_that_is_there(teddycloud, ruid):
    source = teddycloud.tag_entries[ruid]["source"]
    return source.startswith("lib://") and taf_exists(teddycloud, "/" + source[len("lib://"):])


class TestStickersChangedDuringAReplace:
    """A replace runs for minutes; stickers can be copied and moved while it does."""

    def test_a_sticker_copied_from_the_copy_beside_plays_the_new_taf(self, teddycloud, sources):
        from tonie_studio.tonie import copy_tag

        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag(S1, source="lib://a.taf")
        teddycloud.add_tag(S2)
        _during_the_final_encode(teddycloud, "/a.taf", lambda: copy_tag(teddycloud, S1, S2))

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.tag_entries[S1]["source"] == "lib://a.taf"
        assert teddycloud.tag_entries[S2]["source"] == "lib://a.taf"
        assert [path for path in teddycloud.files if path.endswith(".taf")] == ["/a.taf"]

    def test_a_sticker_moved_away_stays_forgotten(self, teddycloud, sources):
        from tonie_studio.tonie import move_tag

        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag(S1, source="lib://a.taf")
        teddycloud.add_tag(S2)
        _during_the_final_encode(teddycloud, "/a.taf", lambda: move_tag(teddycloud, S1, S2))
        written = []
        original_set_tag = teddycloud.set_tag
        teddycloud.set_tag = lambda ruid, **fields: (written.append(ruid),
                                                     original_set_tag(ruid, **fields))

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert S1 not in teddycloud.tag_entries
        assert written.count(S1) == 1  # pointed at the copy beside, never back
        assert teddycloud.tag_entries[S2]["source"] == "lib://a.taf"
        assert _plays_audio_that_is_there(teddycloud, S2)

    def test_a_sticker_given_other_audio_meanwhile_keeps_it(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag(S1, source="lib://a.taf")

        def reassign():
            teddycloud.tag_entries[S1]["source"] = "lib://b.taf"

        _during_the_final_encode(teddycloud, "/a.taf", reassign)

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.tag_entries[S1]["source"] == "lib://b.taf"


class TestAnInterruptedReplaceIsRepaired:
    """A replace stopped during its final encode leaves stickers on its copy beside."""

    ASIDE = "/S/a.new-0badf00d.taf"

    def _left_behind(self, teddycloud, target_size=10):
        teddycloud.create_dir("/S")
        teddycloud.files[self.ASIDE] = b"n" * 10
        teddycloud.taf_headers[self.ASIDE] = {"valid": True, "trackSeconds": [0, 5]}
        if target_size:
            teddycloud.files["/S/a.taf"] = b"n" * target_size
            teddycloud.taf_headers["/S/a.taf"] = {"valid": True, "trackSeconds": [0, 5]}
        teddycloud.add_tag(S1, source="lib://S/a.new-0badf00d.taf")

    def test_the_next_replace_takes_the_stickers_over(self, teddycloud, sources):
        self._left_behind(teddycloud)

        encode_files(teddycloud, sources, "/S/a.taf", replace=True)

        assert teddycloud.tag_entries[S1]["source"] == "lib://S/a.taf"
        assert sorted(path for path in teddycloud.files if path.endswith(".taf")) == ["/S/a.taf"]

    def test_the_next_replace_takes_them_over_at_the_top_of_the_library(self, teddycloud, sources):
        teddycloud.files["/a.taf"] = b"older"
        teddycloud.files["/a.new-0badf00d.taf"] = b"newer"
        teddycloud.add_tag(S1, source="lib://a.new-0badf00d.taf")

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert teddycloud.tag_entries[S1]["source"] == "lib://a.taf"
        assert [path for path in teddycloud.files if path.endswith(".taf")] == ["/a.taf"]

    def test_recovery_points_the_stickers_back_and_deletes_the_copy(self, teddycloud):
        from tonie_studio.encode import recover_asides

        self._left_behind(teddycloud)

        assert recover_asides(teddycloud) == 1

        assert teddycloud.tag_entries[S1]["source"] == "lib://S/a.taf"
        assert self.ASIDE not in teddycloud.files

    def test_recovery_deletes_a_copy_nothing_plays(self, teddycloud):
        from tonie_studio.encode import recover_asides

        self._left_behind(teddycloud)
        teddycloud.tag_entries[S1]["source"] = "lib://S/a.taf"

        assert recover_asides(teddycloud) == 1

        assert self.ASIDE not in teddycloud.files

    def test_recovery_keeps_the_copy_when_the_target_is_gone(self, teddycloud):
        from tonie_studio.encode import recover_asides

        self._left_behind(teddycloud, target_size=0)

        assert recover_asides(teddycloud) == 0

        assert teddycloud.tag_entries[S1]["source"] == "lib://S/a.new-0badf00d.taf"
        assert self.ASIDE in teddycloud.files

    def test_recovery_keeps_the_copy_when_the_target_is_half_written(self, teddycloud):
        # teddyCloud lists the header of a taf it is still writing as valid.
        from tonie_studio.encode import recover_asides

        self._left_behind(teddycloud, target_size=4)

        assert recover_asides(teddycloud) == 0

        assert teddycloud.tag_entries[S1]["source"] == "lib://S/a.new-0badf00d.taf"
        assert self.ASIDE in teddycloud.files

    def test_recovery_leaves_a_replace_that_is_running_alone(self, teddycloud, sources):
        from tonie_studio.encode import recover_asides

        teddycloud.files["/a.taf"] = b"older"
        teddycloud.add_tag(S1, source="lib://a.taf")
        seen = {}
        original = teddycloud.encode

        def encode(path, staged, special="library"):
            original(path, staged, special)
            if path == "/a.taf":
                seen["recovered"] = recover_asides(teddycloud)

        teddycloud.encode = encode

        encode_files(teddycloud, sources, "/a.taf", replace=True)

        assert seen["recovered"] == 0
        assert teddycloud.tag_entries[S1]["source"] == "lib://a.taf"
        assert [path for path in teddycloud.files if path.endswith(".taf")] == ["/a.taf"]


class TestTheListedSpellingIsUsed:
    """teddyCloud's filesystem holds a composed and a decomposed umlaut as two names."""

    NFD_FOLDER, NFD = "/Ba\u0308ren", "/Ba\u0308ren/Ga\u0308ste.taf"
    NFC = "/B\u00e4ren/G\u00e4ste.taf"

    def _decomposed(self, teddycloud):
        teddycloud.create_dir(self.NFD_FOLDER)
        teddycloud.files[self.NFD] = b"older"
        teddycloud.taf_headers[self.NFD] = {"valid": True, "audioId": 1, "sha1Hash": "aa" * 20,
                                            "trackSeconds": [0]}

    def test_an_existing_taf_is_named_as_it_is_listed(self, teddycloud):
        from tonie_studio.encode import existing_taf

        self._decomposed(teddycloud)

        found = existing_taf(teddycloud, self.NFC)

        assert found.source == "lib://Ba\u0308ren/Ga\u0308ste.taf"
        assert found.target == self.NFD

    def test_a_replace_writes_over_the_listed_file(self, teddycloud, sources):
        self._decomposed(teddycloud)
        teddycloud.add_tag(S1, source="lib://Ba\u0308ren/Ga\u0308ste.taf")

        encode_files(teddycloud, sources, self.NFC, replace=True)

        assert [path for path in teddycloud.files if path.endswith(".taf")] == [self.NFD]
        assert teddycloud.files[self.NFD] != b"older"
        assert teddycloud.tag_entries[S1]["source"] == "lib://Ba\u0308ren/Ga\u0308ste.taf"


class TestTheProgressWatcherOutlivesAHiccup:
    def test_keeps_watching_after_teddycloud_fails_to_answer(self, teddycloud, caplog):
        import threading

        import tonie_studio.encode as encode_module
        from tonie_studio.encode import ENCODE_BYTES_PER_SECOND, _watch_growth

        teddycloud.files["/a.taf"] = b"x" * int(ENCODE_BYTES_PER_SECOND * 120)
        original = teddycloud.list_dir
        failures = {"left": 3}

        def flaky(path="/", special=None):
            if failures["left"]:
                failures["left"] -= 1
                raise TeddyCloudError("teddyCloud at x is unreachable")
            return original(path, special)

        teddycloud.list_dir = flaky
        seen = []
        stop = threading.Event()
        poll = encode_module.GROWTH_POLL
        encode_module.GROWTH_POLL = 0.01
        try:
            thread = threading.Thread(
                target=_watch_growth,
                args=(teddycloud, "/a.taf", 600.0, lambda *a: seen.append(a), stop))
            thread.start()
            eventually(lambda: seen)
            stop.set()
            thread.join(2)
        finally:
            encode_module.GROWTH_POLL = poll

        assert seen[0] == ("encode", 120, 600)
        assert len([record for record in caplog.records if record.levelname == "WARNING"]) == 1
