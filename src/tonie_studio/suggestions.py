"""Suggesting an album's names and cover from MusicBrainz and iTunes.

An album from the archive is named after its tags or its folder, and pictured
by whatever image lies in the folder; some have no picture at all, or only a
small one. Both places know most releases with their artist, year, number of
tracks and a large cover, and answer a plain search without a key. A person
picks one suggestion; its names fill the album's form and its cover becomes
the album's first choice. The archive is read-only, so the cover is kept in
this app's data, once, under a name made from its address.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote

import requests
from PIL import Image

from .album import Album
from .i18n import Problem
from .pictures import PICTURE_ERRORS
from .podcast import LARGEST_FEED, LARGEST_IMAGE, FeedError, Fetch, download
from .podcast_search import ANSWER_WITHIN
from .store import LOCK, changing_json, read_json

log = logging.getLogger(__name__)

MUSICBRAINZ = "https://musicbrainz.org/ws/2/release/"
ITUNES = "https://itunes.apple.com/search"
# The front cover of a release, 500 pixels wide; the archive redirects to the file.
COVER_ART = "https://coverartarchive.org/release/{id}/front-500"
# iTunes names a size in its picture addresses and serves any size asked for.
ITUNES_SIZE = re.compile(r"/\d+x\d+bb\.")
ITUNES_LARGE = "/1000x1000bb."

# Results per place; more is a list nobody reads to the end.
RESULTS = 8

# The places asked, as Suggestion.where names them.
IN_MUSICBRAINZ, IN_ITUNES = "musicbrainz", "itunes"

# How much the title, the artist and the number of tracks count for a match.
TITLE_WEIGHT, ARTIST_WEIGHT, TRACKS_WEIGHT = 0.6, 0.3, 0.1

# Where picks and their covers are kept, beside the app's other data.
PICKS_FILE = "album-picks.json"
COVERS = "album-covers"
# What keep_pick names a cover: never a path, so a stored name cannot reach another file.
COVER_NAME = re.compile(r"[0-9a-f]{20}\.(jpg|png|webp)")
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


class SuggestionProblem(Problem):
    """A suggestion that could not be taken, such as a cover that is no picture."""


@dataclass
class Suggestion:
    title: str
    artist: str
    # Four digits, or "" when the place does not say.
    year: str
    tracks: Optional[int]
    # The address of a large cover; "" where the place has none.
    cover: str
    # One of IN_MUSICBRAINZ, IN_ITUNES.
    where: str
    # How well it matches what was asked, from 0 to 1.
    score: float = 0.0


@dataclass
class Pick:
    """What a person took for an album: the names for its form, and a cover if it had one."""

    series: str
    episode: str
    cover: Optional[Path]


def suggest(artist: str, title: str, tracks: int, fetch: Fetch = download
            ) -> Tuple[List[Suggestion], List[str]]:
    """What both places know of the album, best match first, and the places that did not answer.

    A place that fails is named, not raised: what the other found is still worth showing.
    """
    artist, title = artist.strip(), title.strip()
    if not (artist or title):
        return [], []
    # iTunes keeps audiobooks apart from music albums, so it is asked twice.
    places = ((IN_MUSICBRAINZ, _musicbrainz), (IN_ITUNES, _itunes_albums),
              (IN_ITUNES, _itunes_books))
    with ThreadPoolExecutor(len(places)) as pool:
        asked = [(where, pool.submit(ask, artist, title, fetch)) for where, ask in places]
    found: List[Suggestion] = []
    failed: List[str] = []
    for where, answer in asked:
        try:
            found += answer.result()
        except (FeedError, requests.RequestException, ValueError, KeyError, TypeError) as error:
            log.warning("album suggestions from %s failed: %s", where, error)
            if where not in failed:
                failed.append(where)
    for suggestion in found:
        suggestion.score = _score(suggestion, artist, title, tracks)
    # Of equal matches the one with a cover first: a cover is what most come for.
    return sorted(found, key=lambda suggestion: (-suggestion.score, not suggestion.cover)), failed


def _musicbrainz(artist: str, title: str, fetch: Fetch) -> List[Suggestion]:
    terms = [f'release:"{_unquoted(title)}"'] if title else []
    if artist:
        terms.append(f'artist:"{_unquoted(artist)}"')
    answer = json.loads(fetch(f"{MUSICBRAINZ}?query={quote(' AND '.join(terms))}&fmt=json"
                              f"&limit={RESULTS}", LARGEST_FEED, timeout=ANSWER_WITHIN))
    found = []
    for release in answer.get("releases") or []:
        credit = "".join(f"{part.get('name') or ''}{part.get('joinphrase') or ''}"
                         for part in release.get("artist-credit") or [])
        front = (release.get("cover-art-archive") or {}).get("front")
        found.append(Suggestion(
            title=release.get("title") or "", artist=credit.strip(),
            year=(release.get("date") or "")[:4], tracks=release.get("track-count"),
            cover=COVER_ART.format(id=release["id"]) if front else "", where=IN_MUSICBRAINZ))
    return found


def _itunes_albums(artist: str, title: str, fetch: Fetch) -> List[Suggestion]:
    return _itunes(artist, title, "media=music&entity=album", fetch)


def _itunes_books(artist: str, title: str, fetch: Fetch) -> List[Suggestion]:
    return _itunes(artist, title, "media=audiobook&entity=audiobook", fetch)


def _itunes(artist: str, title: str, kind: str, fetch: Fetch) -> List[Suggestion]:
    term = quote(f"{artist} {title}".strip())
    answer = json.loads(fetch(f"{ITUNES}?term={term}&{kind}&country=de&limit={RESULTS}",
                              LARGEST_FEED, timeout=ANSWER_WITHIN))
    found = []
    for result in answer.get("results") or []:
        artwork = result.get("artworkUrl100") or ""
        found.append(Suggestion(
            title=result.get("collectionName") or "", artist=result.get("artistName") or "",
            year=(result.get("releaseDate") or "")[:4], tracks=result.get("trackCount"),
            cover=ITUNES_SIZE.sub(ITUNES_LARGE, artwork), where=IN_ITUNES))
    return found


def _unquoted(text: str) -> str:
    """``text`` fit for a quoted MusicBrainz search term, which a quote inside would end."""
    return text.replace('"', " ").replace("\\", " ")


def _score(suggestion: Suggestion, artist: str, title: str, tracks: int) -> float:
    score = TITLE_WEIGHT * _alike(suggestion.title, title)
    if artist:
        score += ARTIST_WEIGHT * _alike(suggestion.artist, artist)
    if tracks and suggestion.tracks == tracks:
        score += TRACKS_WEIGHT
    return round(score, 3)


def _alike(one: str, other: str) -> float:
    return SequenceMatcher(None, _plain(one), _plain(other)).ratio()


def _plain(text: str) -> str:
    """Lower case without accents or punctuation, for comparing names written differently."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    letters = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", letters).split())


# --- picks ----------------------------------------------------------------

def keep_pick(data_dir: Path, album_id: str, series: str, episode: str, cover_url: str,
              fetch: Optional[Fetch] = download) -> Pick:
    """Keep what a person took for ``album_id``, fetching its cover when it has one.

    A cover that cannot be had, or is no picture, raises SuggestionProblem
    and keeps nothing, not even the names: half a pick would be a surprise.
    """
    name = _keep_cover(data_dir, cover_url, fetch) if cover_url else ""
    with changing_json(data_dir / PICKS_FILE, {}) as picks:
        picks[album_id] = {"series": series, "episode": episode, "cover": name}
    return Pick(series=series, episode=episode, cover=_cover_path(data_dir, name))


def picked(data_dir: Path, album_id: str) -> Optional[Pick]:
    """What was taken for ``album_id``, or None."""
    with LOCK:
        entry = read_json(data_dir / PICKS_FILE, {}).get(album_id)
    if not isinstance(entry, dict):
        return None
    return Pick(series=str(entry.get("series") or ""), episode=str(entry.get("episode") or ""),
                cover=_cover_path(data_dir, str(entry.get("cover") or "")))


def with_pick(album: Album, data_dir: Path, album_id: str) -> Album:
    """The album with its picked cover as the first choice; the album itself without one."""
    pick = picked(data_dir, album_id)
    if pick is None or pick.cover is None:
        return album
    rest = [path for path in album.cover_candidates if path != pick.cover]
    return replace(album, cover=pick.cover, cover_candidates=[pick.cover] + rest)


def _keep_cover(data_dir: Path, url: str, fetch: Fetch) -> str:
    try:
        data = fetch(url, LARGEST_IMAGE)
    except (FeedError, requests.RequestException) as error:
        raise SuggestionProblem("suggest.no_cover", detail=str(error)) from error
    try:
        with Image.open(io.BytesIO(data)) as picture:
            kind = FORMATS.get(picture.format or "")
            picture.verify()
    except PICTURE_ERRORS as error:
        raise SuggestionProblem("suggest.not_a_picture") from error
    if kind is None:
        raise SuggestionProblem("suggest.not_a_picture")
    name = f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.{kind}"
    path = data_dir / COVERS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return name


def _cover_path(data_dir: Path, name: str) -> Optional[Path]:
    if not COVER_NAME.fullmatch(name):
        return None
    path = data_dir / COVERS / name
    return path if path.is_file() else None
