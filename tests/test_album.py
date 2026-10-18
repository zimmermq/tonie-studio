"""Tests for reading an album folder.

The tests build folders of empty files and feed the tag reader from a table, so
they never touch a real decoder: what is under test is the precedence between
the sources of a name, a number and an order, not mutagen.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from tonie_studio.album import (BY_FILE_NUMBER, BY_NAME, BY_PLAYLIST, BY_TRACK_TAG,
                                WARN_ARTEFACT_COVER, WARN_NO_COVER, TrackTags, album_id,
                                list_albums, read_album)


@pytest.fixture
def tags():
    """Let a test declare tags per file name; unnamed files read as untagged."""
    table = {}

    def reader(path: Path):
        return table.get(path.name)

    with patch("tonie_studio.album._read_tags", side_effect=reader):
        yield table


def _album(root: Path, name: str, files) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    for filename in files:
        (directory / filename).write_bytes(b"")
    return directory


class TestTrackOrder:
    def test_orders_by_the_number_in_the_file_name(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["10-b.flac", "02-a.flac", "01-c.flac"])

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["01-c.flac", "02-a.flac", "10-b.flac"]
        assert album.ordering == BY_FILE_NUMBER

    def test_numbers_the_tracks_consecutively_from_one(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["05-a.flac", "09-b.flac"])

        album = read_album(directory)

        assert [track.number for track in album.tracks] == [1, 2]

    def test_falls_back_to_the_track_tag_when_names_carry_no_number(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["beta.flac", "alpha.flac"])
        tags["alpha.flac"] = TrackTags(track=2)
        tags["beta.flac"] = TrackTags(track=1)

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["beta.flac", "alpha.flac"]
        assert album.ordering == BY_TRACK_TAG

    def test_sorts_by_disc_before_track(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["a.flac", "b.flac", "c.flac"])
        tags["a.flac"] = TrackTags(disc=2, track=1)
        tags["b.flac"] = TrackTags(disc=1, track=2)
        tags["c.flac"] = TrackTags(disc=1, track=1)

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["c.flac", "b.flac", "a.flac"]

    def test_falls_back_to_the_playlist_when_nothing_else_orders(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["beta.flac", "alpha.flac"])
        (directory / "playlist.m3u").write_text("beta.flac\nalpha.flac\n")

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["beta.flac", "alpha.flac"]
        assert album.ordering == BY_PLAYLIST

    def test_ignores_a_playlist_that_does_not_cover_every_track(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["beta.flac", "alpha.flac"])
        (directory / "playlist.m3u").write_text("beta.flac\n")

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["alpha.flac", "beta.flac"]
        assert album.ordering == BY_NAME

    def test_ignores_duplicate_numbers_rather_than_guessing(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "01-b.flac"])

        album = read_album(directory)

        assert album.ordering == BY_NAME


class TestTrackTitle:
    def test_prefers_the_title_tag(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-x-y.flac"])
        tags["01-x-y.flac"] = TrackTags(title="Alle Kinder haben Tr\u00e4ume")

        album = read_album(directory)

        assert album.tracks[0].title == "Alle Kinder haben Tr\u00e4ume"

    def test_builds_a_title_from_the_file_name_when_the_tag_is_missing(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["03-gemeinsam_sind_wir_stark.flac"])

        album = read_album(directory)

        assert album.tracks[0].title == "Gemeinsam sind wir stark"

    def test_drops_the_artist_the_scene_puts_in_the_file_name(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["03-lichterkinder-gemeinsam_sind_wir_stark.flac"])
        tags["03-lichterkinder-gemeinsam_sind_wir_stark.flac"] = TrackTags(artist="Lichterkinder")

        album = read_album(directory)

        assert album.tracks[0].title == "Gemeinsam sind wir stark"

    def test_recognises_the_artist_through_the_transliteration_the_scene_uses(self, tmp_path, tags):
        name = "01-francesco_wilking_and_moritz_kraemer-der_tee_von_eugenia.flac"
        directory = _album(tmp_path, "album", [name])
        tags[name] = TrackTags(artist="Francesco Wilking & Moritz Kr\u00e4mer")

        album = read_album(directory)

        assert album.tracks[0].title == "Der tee von eugenia"

    def test_keeps_a_dashed_name_when_no_artist_matches(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-laing-muecken_nerven_leute.flac"])

        album = read_album(directory)

        assert album.tracks[0].title == "Laing-muecken nerven leute"


class TestExtraTracks:
    def test_marks_a_karaoke_version(self, tmp_path, tags):
        # Half of these albums are karaoke takes of their own first half.
        directory = _album(tmp_path, "album", ["01-a.flac", "16-b.flac"])
        tags["01-a.flac"] = TrackTags(title="Alle Kinder haben Tr\u00e4ume")
        tags["16-b.flac"] = TrackTags(title="Alle Kinder haben Tr\u00e4ume (Karaoke Version)")

        album = read_album(directory)

        assert [track.extra for track in album.tracks] == [False, True]

    def test_marks_an_instrumental_and_a_playback(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "02-b.flac"])
        tags["01-a.flac"] = TrackTags(title="Lied (Instrumental)")
        tags["02-b.flac"] = TrackTags(title="Lied - Playback")

        assert all(track.extra for track in read_album(directory).tracks)

    def test_does_not_mark_a_title_that_merely_contains_the_letters(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac"])
        tags["01-a.flac"] = TrackTags(title="Karaokemaschine kaputt")

        assert read_album(directory).tracks[0].extra is False


class TestAlbumMetadata:
    def test_prefers_the_tags_over_the_folder_name(self, tmp_path, tags):
        directory = _album(tmp_path, "Lichterkinder-Gemeinsam-WEB-FLAC-2017-WALKMAN", ["01-a.flac"])
        tags["01-a.flac"] = TrackTags(
            album="Gemeinsam sind wir stark", albumartist="Lichterkinder", date="2017-09-22",
        )

        album = read_album(directory)

        assert (album.title, album.artist, album.year) == (
            "Gemeinsam sind wir stark", "Lichterkinder", "2017",
        )

    def test_reads_the_artist_from_the_track_tag_when_every_track_agrees(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "02-b.flac"])
        tags["01-a.flac"] = TrackTags(artist="Lichterkinder")
        tags["02-b.flac"] = TrackTags(artist="Lichterkinder")

        album = read_album(directory)

        assert album.artist == "Lichterkinder"

    def test_calls_a_compilation_what_it_is(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "02-b.flac"])
        tags["01-a.flac"] = TrackTags(artist="Laing")
        tags["02-b.flac"] = TrackTags(artist="Erdmoebel")

        album = read_album(directory)

        assert album.artist is None

    def test_reads_the_folder_name_when_nothing_is_tagged(self, tmp_path, tags):
        directory = _album(
            tmp_path, "Lichterkinder-Halloween Lieder-16BIT-44-KHZ-WEB-FLAC-2023-WALKMAN",
            ["01-a.flac"],
        )

        album = read_album(directory)

        assert (album.title, album.artist, album.year) == (
            "Halloween Lieder", "Lichterkinder", "2023",
        )

    def test_keeps_the_whole_folder_name_when_it_follows_no_pattern(self, tmp_path, tags):
        directory = _album(tmp_path, "Unter.Meinem.Bett", ["01-a.flac"])

        album = read_album(directory)

        assert album.title == "Unter Meinem Bett"

    def test_sums_the_playing_time(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "02-b.flac"])
        tags["01-a.flac"] = TrackTags(duration=175.7)
        tags["02-b.flac"] = TrackTags(duration=191.0)

        album = read_album(directory)

        assert album.duration == pytest.approx(366.7)

    def test_leaves_the_playing_time_unknown_when_a_track_is_unknown(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "02-b.flac"])
        tags["01-a.flac"] = TrackTags(duration=175.7)

        album = read_album(directory)

        assert album.duration is None


class TestCover:
    def test_picks_the_file_named_like_a_cover(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "scan.jpg", "cover.jpg"])

        album = read_album(directory)

        assert album.cover.name == "cover.jpg"

    def test_ranks_a_scene_proof_below_anything_else(self, tmp_path, tags):
        # A "proof" is a photo of the disc and a ".log.jpg" is a rendered rip
        # log; both are images and neither is artwork.
        directory = _album(tmp_path, "album", ["01-a.flac", "x-proof.jpg", "y.log.jpg", "art.png"])

        album = read_album(directory)

        assert album.cover.name == "art.png"
        assert [path.name for path in album.cover_candidates] == ["art.png", "x-proof.jpg", "y.log.jpg"]

    def test_says_so_when_the_only_image_is_a_scene_artefact(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "x-proof.jpg"])

        album = read_album(directory)

        assert album.cover.name == "x-proof.jpg"
        assert WARN_ARTEFACT_COVER in album.warnings

    def test_reports_no_cover_rather_than_an_arbitrary_file(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac"])

        album = read_album(directory)

        assert album.cover is None
        assert WARN_NO_COVER in album.warnings


class TestWhatCountsAsATrack:
    def test_takes_only_what_teddycloud_can_encode(self, tmp_path, tags):
        directory = _album(tmp_path, "album", ["01-a.flac", "notes.txt", "cover.jpg", "02-b.mp3"])

        album = read_album(directory)

        assert [track.path.name for track in album.tracks] == ["01-a.flac", "02-b.mp3"]

    def test_reads_an_empty_folder_without_failing(self, tmp_path, tags):
        directory = _album(tmp_path, "any weird - der Astronaut ", [])

        album = read_album(directory)

        assert album.tracks == []
        assert album.warnings


class TestListAlbums:
    def test_finds_every_folder_that_holds_audio(self, tmp_path, tags):
        _album(tmp_path, "one", ["01-a.flac"])
        _album(tmp_path, "two/cd1", ["01-a.flac"])
        _album(tmp_path, "empty", ["readme.txt"])

        found = {summary.name for summary in list_albums(tmp_path)}

        assert found == {"one", "two/cd1"}

    def test_counts_the_tracks_and_finds_the_cover(self, tmp_path, tags):
        _album(tmp_path, "one", ["01-a.flac", "02-b.flac", "cover.jpg"])

        summary = list_albums(tmp_path)[0]

        assert (summary.track_count, summary.has_cover) == (2, True)
        assert summary.cover.name == "cover.jpg"

    def test_describes_the_album_for_the_picker(self, tmp_path, tags):
        # The picker shows a readable title, not the scene folder name, so the
        # summary has to carry what the list renders.
        _album(tmp_path, "Lichterkinder-Tanz- und Partylieder-WEB-FLAC-2021-WALKMAN", ["01-a.flac"])
        tags["01-a.flac"] = TrackTags(
            album="Tanz- und Partylieder", albumartist="Lichterkinder", date="2021-04-01",
        )

        summary = list_albums(tmp_path)[0]

        assert (summary.title, summary.artist, summary.year) == (
            "Tanz- und Partylieder", "Lichterkinder", "2021",
        )

    def test_reads_only_the_first_track_for_the_listing(self, tmp_path, tags):
        # Sixteen albums must not mean five hundred tag reads on every page
        # load, so the listing settles for what the first file says.
        _album(tmp_path, "one", ["01-a.flac", "02-b.flac", "03-c.flac"])
        tags["01-a.flac"] = TrackTags(album="Von Track eins", albumartist="Wer auch immer")

        summary = list_albums(tmp_path)[0]

        assert summary.title == "Von Track eins"

    def test_falls_back_to_the_folder_name_when_nothing_is_tagged(self, tmp_path, tags):
        _album(tmp_path, "Lichterkinder-Schlaflieder-WEB-FLAC-2021-WALKMAN", ["01-a.flac"])

        summary = list_albums(tmp_path)[0]

        assert (summary.title, summary.artist, summary.year) == (
            "Schlaflieder", "Lichterkinder", "2021",
        )

    def test_sorts_by_name(self, tmp_path, tags):
        _album(tmp_path, "beta", ["01-a.flac"])
        _album(tmp_path, "alpha", ["01-a.flac"])

        assert [summary.name for summary in list_albums(tmp_path)] == ["alpha", "beta"]

    def test_skips_a_folder_that_only_holds_other_albums(self, tmp_path, tags):
        _album(tmp_path, "series/season 1", ["01-a.flac"])

        assert [summary.name for summary in list_albums(tmp_path)] == ["series/season 1"]

    def test_keeps_going_past_a_folder_it_cannot_read(self, tmp_path, tags, monkeypatch):
        # One unreadable folder on a network share must not cost the whole
        # archive listing.
        _album(tmp_path, "good", ["01-a.flac"])
        (tmp_path / "broken").mkdir()

        readable = Path.iterdir

        def guarded(self):
            if self.name == "broken":
                raise PermissionError(str(self))
            return readable(self)

        monkeypatch.setattr(Path, "iterdir", guarded)

        assert [summary.name for summary in list_albums(tmp_path)] == ["good"]


class TestCheapListing:
    def test_finds_the_same_albums_without_reading_tags(self, tmp_path, tags, monkeypatch):
        # Resolving an id needs directory names only. Over a share, opening one
        # file per album is what makes every cover request slow.
        _album(tmp_path, "one", ["01-a.flac"])
        _album(tmp_path, "two", ["01-a.flac"])

        def refuse(path):
            raise AssertionError("no tag read may happen here")

        monkeypatch.setattr("tonie_studio.album._read_tags", refuse)
        summaries = list_albums(tmp_path, describe=False)

        assert [s.name for s in summaries] == ["one", "two"]

    def test_gives_the_same_ids_either_way(self, tmp_path, tags):
        _album(tmp_path, "one", ["01-a.flac"])

        assert [s.id for s in list_albums(tmp_path, describe=False)] == \
               [s.id for s in list_albums(tmp_path)]


class TestAlbumId:
    def test_survives_the_round_trip_through_a_url(self, tmp_path):
        # The name cannot be the identifier: macOS hands out decomposed
        # umlauts, a browser sends back composed ones, and the two are
        # different strings for the same folder.
        directory = tmp_path / "Bobo_Siebenschl\u00e4fer"

        assert album_id(tmp_path, directory).isalnum()

    def test_differs_per_folder(self, tmp_path):
        assert album_id(tmp_path, tmp_path / "a") != album_id(tmp_path, tmp_path / "b")

    def test_is_the_same_for_both_umlaut_spellings(self, tmp_path):
        composed = tmp_path / "Siebenschl\u00e4fer"
        decomposed = tmp_path / "Siebenschla\u0308fer"

        assert album_id(tmp_path, composed) == album_id(tmp_path, decomposed)


class TestReadingSeveralAlbums:
    """Over a network share each album costs about a second, mostly waiting; a batch reads several at once."""

    def test_reads_them_side_by_side_and_keeps_their_order(self, tmp_path, monkeypatch):
        import threading
        import time

        import tonie_studio.album as album_module

        running, most = [0], [0]
        lock = threading.Lock()

        def slow_read(path):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return path.name

        monkeypatch.setattr(album_module, "read_album", slow_read)
        paths = [tmp_path / f"album-{number}" for number in range(8)]

        read = album_module.read_albums(paths)

        assert read == [path.name for path in paths]
        assert most[0] > 1


def _flac(path, seconds=3, **tags):
    """A FLAC file of nothing but its header, tagged: enough for mutagen to read."""
    import struct

    from mutagen.flac import FLAC

    rate, channels, bits = 44100, 2, 16
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | (rate * seconds)
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + struct.pack(">Q", packed) + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, len(info)]) + info)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


class TestReadingRealTags:
    """Every other test stands in for the tag reader; this one reads a real file."""

    def test_reads_names_numbers_and_length(self, tmp_path):
        from tonie_studio.album import _read_tags

        path = _flac(tmp_path / "03.flac", title="Papagei", artist="Lichterkinder",
                     album="Tierische Tierlieder", tracknumber="3/30", discnumber="1",
                     date="2021")

        tags = _read_tags(path)

        assert (tags.title, tags.artist, tags.album, tags.track, tags.disc, tags.date) == (
            "Papagei", "Lichterkinder", "Tierische Tierlieder", 3, 1, "2021")
        assert tags.duration == pytest.approx(3.0)

    def test_a_file_without_tags_reads_as_empty(self, tmp_path):
        from tonie_studio.album import _read_tags

        tags = _read_tags(_flac(tmp_path / "01.flac"))

        assert (tags.title, tags.track) == (None, None)

    def test_a_file_that_is_no_audio_has_no_tags(self, tmp_path):
        from tonie_studio.album import _read_tags

        (tmp_path / "01.flac").write_bytes(b"not audio")

        assert _read_tags(tmp_path / "01.flac") is None
