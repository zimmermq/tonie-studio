"""Reads a folder of audio files into the album that will become one taf.

Everything here is a best guess presented for correction, never a decision. The
archive holds retail rips with complete tags next to scene releases that carry
the artist only in the file name and none at all in the tags, so each field has
a chain of sources and the caller gets told which one answered.

Order matters more than any name: each track becomes a chapter and the Toniebox
skips between them, so a wrong order is a wrong product. The number in the file
name decides first because a scene rip is named ``NN-artist-title`` and that
number is the one thing every release agrees on; tags come second, a playlist
third, and plain alphabetical order last.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import mutagen

from .splitting import Chapter, cue_chapters, embedded_chapters, parse_marks, segments

log = logging.getLogger(__name__)

# How the tracks were put in order, as ``Album.ordering`` reports it. The page
# names each one in the reader's language under ``ordering.<value>``.
BY_FILE_NUMBER = "file_number"
BY_TRACK_TAG = "track_tag"
BY_PLAYLIST = "playlist"
BY_NAME = "name"
ORDERINGS = (BY_FILE_NUMBER, BY_TRACK_TAG, BY_PLAYLIST, BY_NAME)

# What can be wrong with an album folder, as keys of the reader's language.
WARN_NO_AUDIO = "album.warn.no_audio"
WARN_NO_TITLES = "album.warn.no_titles"
WARN_NO_COVER = "album.warn.no_cover"
WARN_ARTEFACT_COVER = "album.warn.artefact_cover"

# What teddyCloud's ffmpeg build accepts as encoder input. Offering anything
# else only produces a failure minutes into an encode.
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".aac", ".m4a", ".m4b", ".wav", ".flac", ".ogg", ".opus",
    ".aiff", ".aif", ".wma", ".ac3", ".dts", ".mp4",
})


def is_audio_name(name: Optional[str]) -> bool:
    """Whether a file of this name is audio an album can hold, by its suffix."""
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS

# How many albums are read at once for a batch. Enough to hide the share's
# latency; more would only queue on the same disk.
ALBUM_READERS = 6

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

# Names a cover is usually given, best first.
COVER_NAMES = ("cover", "folder", "front", "albumart", "album", "artwork")

# Rank of an image that pictures the release rather than its cover.
ARTEFACT_RANK = 2

# How deep below the archive root an album folder may sit.
MAX_DEPTH = 3

# A leading track number in a file name: "03-", "03 ", "03.".
FILENAME_NUMBER = re.compile(r"^(\d{1,3})\s*[-._)]\s*|^(\d{1,3})\s+")

# Tokens a scene folder name appends after the album title.
TECHNICAL_TOKENS = frozenset({
    "WEB", "CD", "CDS", "CDM", "CDA", "VINYL", "TAPE", "DVD", "DAB",
    "FLAC", "MP3", "WAV", "AAC", "OGG", "APE", "WV",
    "KHZ", "KBPS", "BIT", "RETAIL", "PROPER", "REPACK", "REISSUE",
    "REMASTERED", "LIMITED", "DIGIPAK", "DELUXE", "PROMO", "BOOTLEG",
    "VA", "OST", "SINGLE", "EP", "LP", "ALBUM",
})

YEAR = re.compile(r"^(19|20)\d{2}$")

# Marks a track that is a second take of another one rather than its own piece.
# Half of the Lichterkinder albums are karaoke versions of their own first half,
# and nobody wants both on one tonie. Matched on the tag, which spells it out.
EXTRA_MARKERS = re.compile(
    r"\b(karaoke|instrumental|playback|mitsing|version ohne gesang)\b",
    re.IGNORECASE,
)

# German transliterations a scene file name uses where the tag has the letter.
TRANSLITERATIONS = {
    "\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss", "&": "and",
}


@dataclass
class TrackTags:
    """The handful of tag fields this app has a use for."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    albumartist: Optional[str] = None
    date: Optional[str] = None
    track: Optional[int] = None
    disc: Optional[int] = None
    duration: Optional[float] = None


@dataclass
class Track:
    """One audio file, in the position it will hold inside the taf."""

    path: Path
    number: int
    title: str
    artist: Optional[str] = None
    duration: Optional[float] = None
    # A karaoke or instrumental take. Offered unticked rather than hidden: it
    # is a guess from the title, and sometimes the karaoke half is the point.
    extra: bool = False
    # A chapter of a longer file: where it starts and ends in it, in seconds.
    # None for a whole file; ``end`` None also for a last chapter whose file
    # has no known length, which then runs to the end of the file.
    start: Optional[float] = None
    end: Optional[float] = None


@dataclass
class Album:
    """A folder read as one release, with everything the encode step needs."""

    path: Path
    title: str
    artist: Optional[str]
    year: Optional[str]
    tracks: List[Track]
    cover: Optional[Path]
    cover_candidates: List[Path] = field(default_factory=list)
    # Which rule produced the track order, so the UI can say so.
    ordering: str = "name"
    warnings: List[str] = field(default_factory=list)

    @property
    def duration(self) -> Optional[float]:
        """Total playing time, or None when any track's length is unknown."""
        if not self.tracks or any(track.duration is None for track in self.tracks):
            return None
        return sum(track.duration for track in self.tracks)


@dataclass
class AlbumSummary:
    """One row of the album picker, cheap enough to build for a whole archive.

    Carries what the list renders rather than only the folder name, because
    `Lichterkinder-Tanz- und Partylieder-16BIT-44-KHZ-WEB-FLAC-2021-WALKMAN`
    is not what anyone wants to read. Only the first track's tags are consulted
    for that: sixteen albums must not mean five hundred file reads per page.
    """

    id: str
    path: Path
    name: str
    track_count: int
    has_cover: bool
    title: str = ""
    artist: Optional[str] = None
    year: Optional[str] = None
    cover: Optional[Path] = None


def album_id(root: Path, path: Path) -> str:
    """A stable identifier for an album folder, safe to put in a URL.

    The folder name cannot serve as the identifier: a macOS share hands out
    decomposed umlauts, a browser sends composed ones back, and the two are
    different strings for the same folder. Normalising before hashing makes
    both spellings arrive at the same id.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    normalised = unicodedata.normalize("NFC", str(relative))
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]


def list_albums(
    root: Path, max_depth: int = MAX_DEPTH, describe: bool = True,
) -> List[AlbumSummary]:
    """Find every folder below ``root`` that directly holds audio files.

    A folder that only contains other album folders is not itself an album, so
    a release split into discs shows up as its discs rather than as one entry
    that would encode in an undefined order.

    ``describe=False`` skips reading the first track's tags. Looking a folder
    up by its id needs nothing but the directory names, and over a network
    share opening one file per album is what makes that lookup slow.
    """
    summaries = []
    for directory in _walk(root, max_depth):
        entries = _entries(directory)
        audio = [path for path in entries if _is_audio(path)]
        if not audio:
            continue
        covers = _cover_candidates(entries) if describe else []
        if describe:
            first = _read_tags(_first_file(audio)) or TrackTags()
            title, artist, year = _describe(directory, [first], first)
        else:
            title, artist, year = "", None, None
        summaries.append(AlbumSummary(
            id=album_id(root, directory),
            path=directory,
            name=str(directory.relative_to(root)),
            track_count=len(audio),
            has_cover=bool(covers) if describe else False,
            title=title,
            artist=artist,
            year=year,
            cover=covers[0] if covers else None,
        ))
    return sorted(summaries, key=lambda summary: _natural_key(summary.name))


def cover_candidates(path: Path) -> List[Path]:
    """Every image in the folder, most likely to be artwork first.

    Separate from `read_album` because serving a preview needs the picture and
    nothing else. Going through `read_album` would read the tags of all sixty
    tracks of an album to draw one thumbnail.
    """
    return _cover_candidates(_entries(path))


def read_albums(paths: Sequence[Path]) -> List[Album]:
    """``read_album`` for each path, ALBUM_READERS at a time, in the order given.

    Over a network share reading one album is about a second, nearly all of
    it waiting on file opens, so several readers at once cost the share little
    and cut a batch of twenty from twenty seconds to a few.
    """
    with ThreadPoolExecutor(max_workers=ALBUM_READERS, thread_name_prefix="album") as pool:
        return list(pool.map(read_album, paths))


def read_album(path: Path) -> Album:
    """Read one folder into an album, guessing every field it does not state."""
    entries = _entries(path)
    files = [item for item in entries if _is_audio(item)]
    warnings: List[str] = []

    tags = {file: _read_tags(file) or TrackTags() for file in files}
    ordered, ordering = _order(files, tags, path)

    tracks = [_track(file, index, tags[file]) for index, file in enumerate(ordered, start=1)]
    tracks = _chaptered(tracks, entries)

    if not tracks:
        warnings.append(WARN_NO_AUDIO)
    elif not any(tags[file].title for file in files):
        warnings.append(WARN_NO_TITLES)

    candidates = _cover_candidates(entries)
    if not candidates:
        warnings.append(WARN_NO_COVER)
    elif _cover_rank(candidates[0]) == ARTEFACT_RANK:
        warnings.append(WARN_ARTEFACT_COVER)

    first = tags[_first_file(files)] if files else TrackTags()
    title, artist, year = _describe(path, tags.values(), first)

    return Album(
        path=path,
        title=title,
        artist=artist,
        year=year,
        tracks=tracks,
        cover=candidates[0] if candidates else None,
        cover_candidates=candidates,
        ordering=ordering,
        warnings=warnings,
    )


# --- chapters -------------------------------------------------------------

# Only a file at least this long is asked for chapters, in seconds: reading
# every song of every album for chapters it never has would slow each album
# page, and a file this short is a song, not a book.
LONG_FILE_SECONDS = 15 * 60


def _chaptered(tracks: List[Track], entries) -> List[Track]:
    """The tracks with each long file that names its chapters read as one track per chapter.

    The chapters come from the file itself or, for the only audio file of
    the folder, from a CUE sheet beside it.
    """
    single = len(tracks) == 1
    found: List[Track] = []
    for track in tracks:
        chapters: List[Chapter] = []
        if (track.duration or 0) >= LONG_FILE_SECONDS:
            chapters = embedded_chapters(track.path)
            if len(chapters) < 2 and single:
                chapters = _cue_beside(entries)
        found += _split_track(track, chapters) if len(chapters) >= 2 else [track]
    for number, track in enumerate(found, start=1):
        track.number = number
    return found


def with_marks(album: Album, text: str) -> Album:
    """The album with its only file split at the times in ``text``; see parse_marks.

    An album of several files, or no marks, is returned as it is. Raises
    SplitProblem for marks that cannot be read.
    """
    if not text.strip() or len({track.path for track in album.tracks}) != 1:
        return album
    first = album.tracks[0]
    ends = [track.end for track in album.tracks if track.end is not None]
    whole = Track(path=first.path, number=1, title=album.title or first.title,
                  artist=first.artist,
                  duration=max(ends) if ends and len(album.tracks) > 1 else first.duration)
    tracks = _split_track(whole, parse_marks(text))
    for number, track in enumerate(tracks, start=1):
        track.number = number
    return Album(path=album.path, title=album.title, artist=album.artist, year=album.year,
                 tracks=tracks, cover=album.cover, cover_candidates=album.cover_candidates,
                 ordering=album.ordering, warnings=album.warnings)


def _split_track(track: Track, chapters: Sequence[Chapter]) -> List[Track]:
    """One track per chapter of the file ``track`` is; a chapter without a title is numbered."""
    return [Track(path=track.path, number=0, title=title or f"{track.title} {index}",
                  artist=track.artist, duration=end - start if end is not None else None,
                  start=start, end=end)
            for index, (start, end, title) in enumerate(segments(chapters, track.duration),
                                                        start=1)]


def _cue_beside(entries) -> List[Chapter]:
    """The chapters of a CUE sheet in the folder; [] without one.

    CUE sheets come in UTF-8 or, from older rippers, Windows-1252.
    """
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() == ".cue":
            data = entry.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")
            return cue_chapters(text)
    return []


# --- ordering -------------------------------------------------------------


def _order(files, tags, directory: Path):
    """Return the files in playing order, plus the name of the rule that won."""
    if not files:
        return [], BY_NAME

    numbers = [_filename_number(file.name) for file in files]
    if all(number is not None for number in numbers) and len(set(numbers)) == len(numbers):
        return [file for _, file in sorted(zip(numbers, files))], BY_FILE_NUMBER

    keys = [(tags[file].disc or 1, tags[file].track) for file in files]
    if all(track is not None for _, track in keys) and len(set(keys)) == len(keys):
        return [file for _, file in sorted(zip(keys, files))], BY_TRACK_TAG

    playlist = _playlist_order(directory, files)
    if playlist:
        return playlist, BY_PLAYLIST

    return files, BY_NAME


def _filename_number(name: str) -> Optional[int]:
    match = FILENAME_NUMBER.match(name)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _playlist_order(directory: Path, files):
    """Order the files by a playlist, but only if it names every one of them.

    A playlist listing a subset is a partial rip or a sampler and says nothing
    about the rest, so it is ignored rather than merged with another rule.
    """
    by_name = {unicodedata.normalize("NFC", file.name): file for file in files}

    for playlist in sorted(directory.glob("*.m3u")) + sorted(directory.glob("*.m3u8")):
        try:
            # Scene playlists are frequently cp1252; nothing here depends on
            # bytes that decode differently, so a lenient decode is enough.
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        ordered = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = unicodedata.normalize("NFC", Path(line.replace("\\", "/")).name)
            if name in by_name:
                ordered.append(by_name[name])

        if len(ordered) == len(files) and len(set(ordered)) == len(files):
            return ordered
    return None


# --- naming ---------------------------------------------------------------


def _track(path: Path, number: int, tags: TrackTags) -> Track:
    title = _track_title(path, tags)
    return Track(path=path, number=number, title=title, artist=tags.artist,
                 duration=tags.duration, extra=bool(EXTRA_MARKERS.search(title)))


def _track_title(path: Path, tags: TrackTags) -> str:
    """The track's name: its tag, or what the file name can be talked into."""
    if tags.title:
        return tags.title.strip()

    stem = FILENAME_NUMBER.sub("", path.stem, count=1)
    stem = _strip_artist(stem, tags.artist)
    return _humanise(stem)


def _strip_artist(stem: str, artist: Optional[str]) -> str:
    """Drop the artist that a scene file name carries between number and title.

    Only an artist that matches the one in the tags is removed. Guessing from
    the dashes alone would eat the first word of every title that has one.
    """
    if not artist or "-" not in stem:
        return stem

    wanted = _slug(artist)
    parts = stem.split("-")
    for index in range(1, len(parts)):
        if _slug("-".join(parts[:index])) == wanted:
            return "-".join(parts[index:])
    return stem


def _slug(text: str) -> str:
    """Reduce a name to what survives a scene file name unchanged."""
    text = text.lower()
    for character, replacement in TRANSLITERATIONS.items():
        text = text.replace(character, replacement)
    return "".join(character for character in text if character.isalnum())


def _humanise(stem: str) -> str:
    """Turn a file name stem into something readable, changing as little as possible."""
    text = stem.replace("_", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return stem
    # Only the first letter: German capitalises nouns, so title case would be
    # wrong more often than it is right.
    return text[0].upper() + text[1:]


# A folder holding one disc of a release: "CD1", "cd 2", "Disc_3", "Disk-04".
DISC_FOLDER = re.compile(r"^(?:cd|disc|disk)[\s_-]*0*(\d{1,2})$", re.IGNORECASE)


def disc_number(folder_name: str) -> Optional[int]:
    """Which disc a folder holds, when its name says it is one disc of a release."""
    match = DISC_FOLDER.match(folder_name.strip())
    return int(match.group(1)) if match else None


def _first_file(files: Sequence[Path]) -> Path:
    """The track whose tags name the album: the first by file name."""
    return min(files, key=lambda path: _natural_key(path.name))


def name_from_file(name: str) -> str:
    """What a lone audio file is called, read from its name like a track title."""
    return _track_title(Path(name), TrackTags())


def _describe(path: Path, all_tags, first: TrackTags):
    """The album's title, artist and year, from the tags or from the folder name.

    The title is the album tag of ``first``, the first track by file name,
    even where other tracks name another album: the album list reads that
    track alone, and the list and the album's own pages have to call one
    album the same. A folder of tracks from several albums is a mix with
    no name of its own, so the first track's is as good as any.

    The discs of one release carry the same album tag, and would read as the
    same album twice: the same title, the same taf, so the second could never
    be made. A disc folder's title therefore says which disc it is, and its
    release is read from the folder above it.
    """
    tags = list(all_tags)
    title = (first.album or "").strip() or None
    artist = _common(tag.albumartist for tag in tags) or _common(tag.artist for tag in tags)
    year = _year(_common(tag.date for tag in tags))
    disc = disc_number(path.name)
    release = path.parent if disc else path

    if not (title and artist and year):
        parsed_title, parsed_artist, parsed_year = _parse_folder_name(release.name)
        title, artist, year = title or parsed_title, artist or parsed_artist, year or parsed_year

    if disc and not re.search(rf"\b(?:cd|disc|disk)[\s_-]*0*{disc}\b", title or "", re.IGNORECASE):
        title = f"{title} (CD {disc})"
    return title, artist, year


def _common(values) -> Optional[str]:
    """The one value every track agrees on, or None where they differ.

    A compilation has a different artist on every track, and calling one of
    them the album artist would be a lie.
    """
    distinct = {value.strip() for value in values if value and value.strip()}
    return distinct.pop() if len(distinct) == 1 else None


def _year(date: Optional[str]) -> Optional[str]:
    if not date:
        return None
    match = re.search(r"(19|20)\d{2}", date)
    return match.group(0) if match else None


def _parse_folder_name(name: str):
    """Read ``Artist-Album-TECH-YEAR-GROUP``, the shape scene releases use.

    The year is the anchor: without one there is no way to tell an artist from
    a title, so the whole name becomes the title rather than half of it.
    """
    parts = [part.strip() for part in name.split("-")]
    years = [index for index, part in enumerate(parts) if YEAR.match(part)]

    if not years or years[-1] < 2:
        return _humanise(name.replace(".", " ")), None, _year(name)

    year_at = years[-1]
    artist = parts[0]
    middle = parts[1:year_at]
    while middle and _is_technical(middle[-1]):
        middle.pop()

    title = "-".join(middle).strip() if middle else artist
    return title or artist, artist, parts[year_at]


def _is_technical(token: str) -> bool:
    token = token.strip().upper()
    if not token:
        return True
    if token in TECHNICAL_TOKENS or token.isdigit():
        return True
    return bool(re.fullmatch(r"\d+(BIT|KHZ|KBPS)", token))


# --- files ----------------------------------------------------------------


def _cover_candidates(entries) -> List[Path]:
    """Every image in the folder, most likely to be artwork first.

    A scene release ships proof photos of the disc and rip logs rendered as
    images. They are pictures of the release, not of its cover, so they rank
    below anything else rather than being excluded: sometimes they are all
    there is.
    """
    images = [path for path in entries if _is_image(path)]
    return sorted(images, key=lambda path: (_cover_rank(path), _natural_key(path.name)))


def _cover_rank(path: Path) -> int:
    stem = path.stem.lower()
    if stem in COVER_NAMES or any(stem.startswith(name) for name in COVER_NAMES):
        return 0
    if stem.endswith("proof") or ".log" in stem or stem.endswith("log"):
        return ARTEFACT_RANK
    return 1


def _entries(directory: Path) -> List[Path]:
    """The directory's contents in natural order, empty if it cannot be read.

    An archive on a network share regularly has one folder that answers with
    ENOENT or a permission error. Losing that folder is acceptable; losing the
    listing it appears in is not.
    """
    try:
        return sorted(directory.iterdir(), key=lambda item: _natural_key(item.name))
    except OSError as error:
        log.warning("cannot read %s: %s", directory, error)
        return []


def _walk(root: Path, max_depth: int):
    """Yield the directories below root, deepest allowed level included."""
    if max_depth <= 0:
        return

    for entry in _entries(root):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        yield entry
        yield from _walk(entry, max_depth - 1)


def _is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def _is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def _natural_key(name: str):
    """Sort so that a track 10 follows a track 9 rather than a track 1."""
    normalised = unicodedata.normalize("NFC", name).lower()
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", normalised)
    ]


def _read_tags(path: Path) -> Optional[TrackTags]:
    """Read one file's tags, or None when it has none this app understands."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as error:  # mutagen raises a different type per format
        log.debug("cannot read tags of %s: %s", path, error)
        return None
    if audio is None:
        return None

    tags: Dict = audio.tags or {}
    return TrackTags(
        title=_first(tags.get("title")),
        artist=_first(tags.get("artist")),
        album=_first(tags.get("album")),
        albumartist=_first(tags.get("albumartist")),
        date=_first(tags.get("date")) or _first(tags.get("year")),
        track=_leading_number(_first(tags.get("tracknumber"))),
        disc=_leading_number(_first(tags.get("discnumber"))),
        duration=getattr(getattr(audio, "info", None), "length", None),
    )


def _first(values) -> Optional[str]:
    if not values:
        return None
    value = values[0] if isinstance(values, list) else values
    return str(value).strip() or None


def _leading_number(value: Optional[str]) -> Optional[int]:
    """Read "03" and "3/30" alike, which is how tracknumber is written."""
    if not value:
        return None
    match = re.match(r"\d+", value)
    return int(match.group(0)) if match else None
