"""Tests for splitting one long audio file into chapters."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tonie_studio.splitting import (Chapter, SplitProblem, cue_chapters, cut, embedded_chapters,
                                    parse_marks, segments)


def _ffmpeg_runs() -> bool:
    """Whether an ffmpeg is there that starts; a broken install counts as none."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
    except OSError:
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_runs(), reason="needs a working ffmpeg")


class TestParseMarks:
    def test_reads_times_and_titles(self):
        assert parse_marks("00:00 Intro\n12:34 Kapitel 2\n1:02:03.5 Kapitel 3") == [
            Chapter(0, "Intro"), Chapter(754, "Kapitel 2"), Chapter(3723.5, "Kapitel 3")]

    def test_a_dash_or_tab_after_the_time_is_not_part_of_the_title(self):
        assert parse_marks("0:00 - Eins\n5:00\tZwei") == [Chapter(0, "Eins"), Chapter(300, "Zwei")]

    def test_empty_lines_are_skipped(self):
        assert parse_marks("\n0:00 Eins\n\n  \n5:00 Zwei\n") == [Chapter(0, "Eins"),
                                                                  Chapter(300, "Zwei")]

    def test_a_first_mark_after_the_start_gets_a_chapter_before_it(self):
        # What comes before the first mark would otherwise be cut away.
        assert parse_marks("0:30 Eins\n5:00 Zwei") == [Chapter(0, ""), Chapter(30, "Eins"),
                                                       Chapter(300, "Zwei")]

    def test_a_line_without_a_time_is_refused_with_its_number(self):
        with pytest.raises(SplitProblem) as refused:
            parse_marks("0:00 Eins\nKapitel ohne Zeit")

        assert refused.value.key == "split.bad_line" and refused.value.params["line"] == 2

    def test_marks_out_of_order_are_refused(self):
        with pytest.raises(SplitProblem) as refused:
            parse_marks("0:00 Eins\n5:00 Zwei\n4:00 Drei")

        assert refused.value.key == "split.not_ascending" and refused.value.params["line"] == 3

    def test_seconds_past_59_are_refused(self):
        with pytest.raises(SplitProblem):
            parse_marks("0:75 Eins")

    def test_nothing_is_no_chapters(self):
        assert parse_marks("  \n ") == []


class TestCue:
    CUE = '''PERFORMER "Erzähler"
TITLE "Das Hörbuch"
FILE "hoerbuch.mp3" MP3
  TRACK 01 AUDIO
    TITLE "Anfang"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Mitte"
    INDEX 00 10:59:50
    INDEX 01 11:00:37
'''

    def test_reads_each_track_where_its_index_01_starts(self):
        # A CD frame is 1/75 of a second; INDEX 00 is the pause before a track.
        found = cue_chapters(self.CUE)

        assert [chapter.title for chapter in found] == ["Anfang", "Mitte"]
        assert [chapter.start for chapter in found] == [0, pytest.approx(660 + 37 / 75)]

    def test_a_track_without_a_title_gets_none(self):
        assert cue_chapters('FILE "a.mp3" MP3\n TRACK 01 AUDIO\n  INDEX 01 00:00:00\n') == [
            Chapter(0, "")]


class TestSegments:
    def test_each_chapter_ends_where_the_next_begins(self):
        assert segments([Chapter(0, "Eins"), Chapter(300, "Zwei")], 900) == [
            (0, 300, "Eins"), (300, 900, "Zwei")]

    def test_the_last_ends_with_the_file_when_its_length_is_unknown(self):
        assert segments([Chapter(0, "Eins"), Chapter(300, "Zwei")], None) == [
            (0, 300, "Eins"), (300, None, "Zwei")]

    def test_marks_past_the_end_of_the_file_are_left_out(self):
        assert segments([Chapter(0, "Eins"), Chapter(950, "Zu spät")], 900) == [
            (0, 900, "Eins")]


class TestCut:
    def test_cuts_without_encoding_again(self, tmp_path):
        ran = []

        def run(command, **kwargs):
            ran.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        cut(Path("/archive/buch.m4b"), 30.0, 90.5, tmp_path / "001.m4a", run=run)

        command = ran[0]
        assert command[0] == "ffmpeg"
        assert command[command.index("-ss") + 1] == "30.000"
        assert command[command.index("-to") + 1] == "90.500"
        assert command[command.index("-i") + 1] == "/archive/buch.m4b"
        assert command[command.index("-c") + 1] == "copy"
        assert command[-1] == str(tmp_path / "001.m4a")

    def test_the_last_chapter_runs_to_the_end(self, tmp_path):
        ran = []

        def run(command, **kwargs):
            ran.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        cut(Path("/archive/buch.mp3"), 30.0, None, tmp_path / "002.mp3", run=run)

        assert "-to" not in ran[0]

    def test_a_failure_says_what_ffmpeg_said(self, tmp_path):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"Invalid data found")

        with pytest.raises(SplitProblem) as refused:
            cut(Path("/archive/kaputt.mp3"), 0.0, 10.0, tmp_path / "001.mp3", run=run)

        assert refused.value.key == "split.cut_failed"
        assert "Invalid data found" in refused.value.params["detail"]

    @needs_ffmpeg
    def test_a_real_file_is_cut_to_the_chapter(self, tmp_path):
        source = tmp_path / "ton.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=6", "-c:a", "libmp3lame", str(source)],
                       check=True)

        cut(source, 2.0, 4.0, tmp_path / "001.mp3")

        from mutagen.mp3 import MP3

        assert abs(MP3(tmp_path / "001.mp3").info.length - 2.0) < 0.2


class TestEmbeddedChapters:
    @needs_ffmpeg
    def test_reads_the_chapters_of_an_audiobook(self, tmp_path):
        meta = tmp_path / "chapters.txt"
        meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=3000\n"
                        "title=Erstes\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=3000\nEND=6000\n"
                        "title=Zweites\n")
        book = tmp_path / "buch.m4b"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=6", "-i", str(meta), "-map_metadata", "1",
                        "-map_chapters", "1", "-c:a", "aac", "-f", "mp4", str(book)], check=True)

        found = embedded_chapters(book)

        assert [chapter.title for chapter in found] == ["Erstes", "Zweites"]
        assert found[0].start == 0 and abs(found[1].start - 3.0) < 0.1

    def test_a_file_without_chapters_has_none(self, tmp_path):
        plain = tmp_path / "plain.mp3"
        plain.write_bytes(b"not really audio")

        assert embedded_chapters(plain) == []
