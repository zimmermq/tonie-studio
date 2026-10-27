"""Splitting one long audio file into chapters: where they start, and cutting them out.

An audiobook often comes as a single file. The box skips between chapters,
and teddyCloud makes a chapter of every file it encodes, so the file is cut
into one file per chapter first. Where the chapters start comes from the file
itself (an m4b or m4a with chapters, an MP3 with ID3 chapter frames), from a
CUE sheet beside it, or from times a person types.

Cutting copies the audio as it is (``ffmpeg -c copy``): it takes seconds even
for a long book and loses nothing, since teddyCloud encodes it once anyway. A
cut lands on the nearest frame of the audio, a few hundredths of a second
from the time asked for.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import mutagen
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

from .i18n import Problem

log = logging.getLogger(__name__)

# A time as a person types it: [h:]mm:ss[.fraction], then the title.
MARK = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d+))?(?:\s*[-\t]\s*|\s+|$)(.*)$")

# A CUE sheet's lines this reads; INDEX 01 is where a track's audio starts,
# in minutes, seconds and frames of 1/75 second.
CUE_TRACK = re.compile(r"^\s*TRACK\s+\d+", re.IGNORECASE)
CUE_TITLE = re.compile(r'^\s*TITLE\s+"(.*)"\s*$', re.IGNORECASE)
CUE_INDEX = re.compile(r"^\s*INDEX\s+01\s+(\d+):(\d{2}):(\d{2})\s*$", re.IGNORECASE)
CUE_FRAMES = 75

# The containers that hold MP4 audio; ffmpeg picks its muxer by the target's
# extension and knows ".m4a" but writes ".m4b" as a video container.
MP4_SUFFIXES = (".m4a", ".m4b", ".mp4", ".aac")


# The step an encode reports while it cuts chapters out, with chapters done and their number.
CUT_STEP = "cut"


class SplitProblem(Problem):
    """Chapter times that cannot be read, or a cut that failed."""


@dataclass
class Chapter:
    # Seconds from the start of the file.
    start: float
    # "" when the source gives none.
    title: str


def parse_marks(text: str) -> List[Chapter]:
    """The chapters in ``text``, one per line as "12:34 Title", in playing order.

    A first time after the start gets a chapter before it, so nothing at the
    beginning is cut away. Raises SplitProblem naming the first line that is
    not a time or comes before the line above it.
    """
    chapters: List[Chapter] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        found = MARK.match(line)
        if not found or int(found.group(3)) > 59 or (found.group(1) and int(found.group(2)) > 59):
            raise SplitProblem("split.bad_line", line=number, text=line.strip())
        hours, minutes, seconds, fraction, title = found.groups()
        start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        if fraction:
            start += float(f"0.{fraction}")
        if chapters and start <= chapters[-1].start:
            raise SplitProblem("split.not_ascending", line=number, text=line.strip())
        chapters.append(Chapter(start, title.strip()))
    if chapters and chapters[0].start > 0:
        chapters.insert(0, Chapter(0, ""))
    return chapters


def cue_chapters(text: str) -> List[Chapter]:
    """The tracks of a CUE sheet as chapters, each from its INDEX 01."""
    chapters: List[Chapter] = []
    title, in_track = "", False
    for line in text.splitlines():
        if CUE_TRACK.match(line):
            title, in_track = "", True
            continue
        if not in_track:
            continue
        named = CUE_TITLE.match(line)
        if named:
            title = named.group(1).strip()
            continue
        index = CUE_INDEX.match(line)
        if index:
            minutes, seconds, frames = (int(part) for part in index.groups())
            chapters.append(Chapter(minutes * 60 + seconds + frames / CUE_FRAMES, title))
            in_track = False
    return chapters


def embedded_chapters(path: Path) -> List[Chapter]:
    """The chapters the file names itself, in playing order; [] for a file with none.

    An m4b or m4a keeps them in its chapter list, an MP3 in ID3 CHAP frames.
    A file this cannot read has none rather than failing the album.
    """
    try:
        if path.suffix.lower() in MP4_SUFFIXES:
            return [Chapter(float(chapter.start), (chapter.title or "").strip())
                    for chapter in MP4(path).chapters or []]
        if path.suffix.lower() == ".mp3":
            frames = sorted(ID3(path).getall("CHAP"), key=lambda frame: frame.start_time)
            return [Chapter(frame.start_time / 1000, _chap_title(frame)) for frame in frames]
    except (ID3NoHeaderError, mutagen.MutagenError, ValueError, OSError) as error:
        log.debug("no chapters read from %s: %s", path, error)
    return []


def _chap_title(frame) -> str:
    title = frame.sub_frames.get("TIT2")
    return str(title.text[0]).strip() if title and title.text else ""


def segments(chapters: Sequence[Chapter], duration: Optional[float]
             ) -> List[Tuple[float, Optional[float], str]]:
    """``(start, end, title)`` of each chapter; the last ends with the file, None when unknown.

    A chapter starting at or past the end of the file is left out.
    """
    kept = [chapter for chapter in chapters if duration is None or chapter.start < duration]
    found = []
    for index, chapter in enumerate(kept):
        end = kept[index + 1].start if index + 1 < len(kept) else duration
        found.append((chapter.start, end, chapter.title))
    return found


def cut_suffix(source: Path) -> str:
    """The extension a part cut from ``source`` gets, which decides ffmpeg's muxer."""
    suffix = source.suffix.lower()
    return ".m4a" if suffix in MP4_SUFFIXES else suffix


def cut(source: Path, start: float, end: Optional[float], target: Path,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Copy the audio of ``source`` from ``start`` to ``end`` (None: to the end) into ``target``.

    Raises SplitProblem with what ffmpeg said when it fails.
    """
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}"]
    if end is not None:
        command += ["-to", f"{end:.3f}"]
    command += ["-i", str(source), "-map", "0:a:0", "-c", "copy", str(target)]
    try:
        done = run(command, capture_output=True)
    except OSError as error:
        raise SplitProblem("split.cut_failed", file=source.name, detail=str(error)) from error
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
        raise SplitProblem("split.cut_failed", file=source.name, detail=detail)


@contextmanager
def chapters_cut(parts: Sequence[Tuple[Path, Optional[float], Optional[float]]],
                 progress: Optional[Callable] = None) -> Iterator[List[Path]]:
    """The files to encode for ``parts``, each ``(file, start, end)``, in the same order.

    A whole file (``start`` None) is used as it is; a chapter is cut into a
    temporary file, all of which are deleted when the block ends, however it
    ends. ``progress`` hears CUT_STEP with the chapters cut so far and their number.
    """
    chapters = sum(1 for _path, start, _end in parts if start is not None)
    if not chapters:
        yield [path for path, _start, _end in parts]
        return
    with tempfile.TemporaryDirectory(prefix="tonie-chapters-") as folder:
        paths: List[Path] = []
        done = 0
        for index, (path, start, end) in enumerate(parts, start=1):
            if start is None:
                paths.append(path)
                continue
            target = Path(folder) / f"{index:03d}{cut_suffix(path)}"
            cut(path, start, end, target)
            paths.append(target)
            done += 1
            if progress:
                progress(CUT_STEP, done, chapters)
        yield paths
