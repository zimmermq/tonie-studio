"""What the pages share: the context they are built with, the album catalogue,
encoding and publishing, and turning requests down in the reader's language.

Album ids carry a one letter prefix for the root they came from. Without it an
uploaded folder and an archive folder of the same name would hash to the same
id, and the wrong one would be encoded.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..album import AUDIO_EXTENSIONS, AlbumSummary, list_albums
from ..albumnotes import remember_made
from ..batch import Placing
from ..boxstate import BoxWatcher
from ..encode import EncodeError, encode_files, existing_taf
from ..i18n import COOKIE as LANG_COOKIE
from ..i18n import Problem, Translator, languages, negotiate
from ..jobs import JobRunner, JobState
from ..listening import ListeningLog
from ..podcast import Fetch
from ..runtimes import remember
from ..settings import Settings
from ..teddycloud import TeddyCloudClient
from ..thumbnails import crop
from ..tonie import DEFAULT_CATEGORY, list_tags, publish

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent.parent / "templates"

# Which root an album id belongs to.
ARCHIVE = "a"

UPLOAD = "u"

# Where teddyCloud serves the covers of custom models.
CUSTOM_IMG_PREFIX = "/custom_img/"

# How long a scan of the archive stays good enough to reuse. One page load is
# one listing plus a preview request per album, and on a network share each of
# those walks costs more than rendering the page does. Short enough that an
# album copied onto the share shows up on the next reload; an upload made here
# clears it outright.
CATALOGUE_TTL = 10.0


@dataclass
class Context:
    """What the pages share, built once by ``create_app``.

    The callables are closures over the app's own state; a page calls them
    rather than holding a copy, so a test that swaps a dependency in
    ``create_app`` reaches every page.
    """

    settings: Settings
    templates: Jinja2Templates
    jobs: JobRunner
    box: BoxWatcher
    listening: ListeningLog
    client: Callable[[], TeddyCloudClient]
    fetch: Fetch
    cached_models: Callable[[], Optional[List[Dict]]]
    albums_with_tonies: Callable[..., Dict[str, str]]
    work_in_progress: Callable[[], Dict]
    queue_podcast: Callable[[str], Optional[str]]
    # Encodes that belong together, by group id: the parts of one album or
    # the albums of a batch.
    groups: Dict[str, List[str]] = field(default_factory=dict)
    # The groups that are several albums rather than one album in parts.
    batches: set = field(default_factory=set)
    # Batches whose stickers are handed out as they are placed on the box.
    placings: Dict[str, Placing] = field(default_factory=dict)


def _encode_job(connection: TeddyCloudClient, chosen: Sequence[Tuple[object, str]], target: str,
                replace: bool, carried: Dict,
                after: Optional[Callable[[Dict], None]] = None, reuse: bool = False) -> Callable:
    """The work of one encode, returning what the tag and model steps need.

    ``carried`` goes into the result unchanged: the names a person approved
    rather than the ones that were guessed, the cover, and what the printed
    text side and the album list read back. ``after`` runs once the taf
    exists and may add to the result; it must not raise for a failure the
    encode itself does not share. With ``reuse`` the taf already at
    ``target`` is taken as it is; it fails if that taf has gone or no
    longer has one chapter per track.
    """
    paths = [track.path for track, _name in chosen]
    names = [name for _track, name in chosen]
    # The playing time turns the encode from a dead bar into a position;
    # teddyCloud itself reports nothing while ffmpeg runs.
    playing_time = sum(track.duration or 0 for track, _name in chosen) or None

    def work(progress):
        if reuse:
            encoded = existing_taf(connection, target)
            if encoded is None or encoded.tracks != len(paths):
                raise EncodeError(f"{target} is no longer the taf this album was planned with")
        else:
            encoded = encode_files(connection, paths, target, progress=progress,
                                   replace=replace, total_seconds=playing_time)
        result = {"encode": encoded, "tracks": names, "seconds": playing_time, **carried}
        if after is not None:
            after(result)
        return result
    return work


def _publish_result(connection: TeddyCloudClient, settings: Settings, ruid: str, result: Dict,
                    series: str, episode: str, category: str = DEFAULT_CATEGORY) -> str:
    """Put a finished encode on a tag, and record what the app keeps about it."""
    model_id = publish(
        connection,
        ruid,
        result["encode"],
        series=series,
        episode=episode,
        tracks=result["tracks"],
        cover=_cropped_cover(result),
        category=category,
        year=result.get("year"),
    )
    remember(settings.data_dir, model_id, result.get("seconds"))
    remember_made(settings.data_dir, result.get("album", ""), model_id)
    return model_id


def _free_tags(connection: TeddyCloudClient) -> List[str]:
    """Tags the box has read that play nothing yet, in the order they are handed out."""
    return [tag.ruid for tag in list_tags(connection) if not tag.assigned]


def _cropped_cover(result: Dict) -> Optional[bytes]:
    """The album cover, cut the way the album page showed it.

    Cropping here rather than at print time means what teddyCloud displays and
    what comes out of the printer are the same picture.
    """
    cover = result.get("cover")
    if not cover:
        return None
    crop_settings = result.get("crop") or {}
    return crop(
        Path(cover),
        zoom=crop_settings.get("zoom", 1.0),
        offset_x=crop_settings.get("x", 0.0),
        offset_y=crop_settings.get("y", 0.0),
    )


def _finished_result(jobs: JobRunner, job_id: str) -> Dict:
    """The result of a finished encode, or a refusal explaining which it is not."""
    job = jobs.get(job_id)
    if job is None:
        raise Refused(404, "error.no_job")
    if job.state is not JobState.DONE:
        raise Refused(409, "error.no_taf_yet")
    return job.result


# Keyed by the directories scanned, not only by `describe`: two apps in one
# process - which is every test run - must not hand each other their archives.
_catalogue_cache: Dict[Tuple, Tuple[float, List[Tuple[str, AlbumSummary, str]]]] = {}


_catalogue_lock = Lock()


def _forget_catalogue() -> None:
    with _catalogue_lock:
        _catalogue_cache.clear()


def _catalogue(
    settings: Settings, describe: bool = True,
) -> List[Tuple[str, AlbumSummary, str]]:
    """Cached front for _scan_albums; see CATALOGUE_TTL."""
    key = (str(settings.archive_dir), str(settings.upload_dir), describe)
    now = time.monotonic()
    with _catalogue_lock:
        cached = _catalogue_cache.get(key)
        if cached and now - cached[0] < CATALOGUE_TTL:
            return cached[1]

    scanned = _scan_albums(settings, describe)
    with _catalogue_lock:
        _catalogue_cache[key] = (now, scanned)
    return scanned


def _scan_albums(
    settings: Settings, describe: bool = True,
) -> List[Tuple[str, AlbumSummary, str]]:
    """Every album on offer, as (id, summary, where it came from).

    Uploads come first: what was just added is what someone is looking for.
    """
    entries = []
    for prefix, root, origin in (
        (UPLOAD, settings.upload_dir, "upload"),
        (ARCHIVE, settings.archive_dir, "archive"),
    ):
        if not root.is_dir():
            continue
        for summary in list_albums(root, describe=describe):
            entries.append((f"{prefix}{summary.id}", summary, origin))
    return entries


def _resolve(settings: Settings, identifier: str) -> Path:
    """Find the folder an album id names, or refuse.

    Deliberately without the descriptions: the picker draws one preview per
    album, and each of those requests lands here. Reading every album's tags
    again for each of them turned a page load into seconds of network file
    opens.
    """
    for candidate_id, summary, _origin in _catalogue(settings, describe=False):
        if candidate_id == identifier:
            return summary.path
    raise Refused(404, "error.album_gone")


class Refused(Problem):
    """A request turned down, with the HTTP status to answer it with."""

    def __init__(self, status: int, key: str, **params):
        super().__init__(key, **params)
        self.status = status


def translator(request: Request) -> Translator:
    """The translator for whoever sent ``request``."""
    return Translator(negotiate(request.cookies.get(LANG_COOKIE),
                                request.headers.get("accept-language")))


def _language(request: Request) -> Dict:
    """What every page needs to speak the reader's language."""
    translate = translator(request)
    return {"t": translate, "lang": translate.lang, "languages": languages()}


def _is_audio_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _is_image_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def _next_to_place(placing: Optional[Placing]) -> str:
    """The episode the next sticker put on the box will get, for the page."""
    if placing is None:
        return ""
    waiting = placing.waiting(lambda job_id: False)
    return waiting.episode if waiting else ""
