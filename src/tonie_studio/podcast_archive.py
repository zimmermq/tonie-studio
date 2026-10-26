"""An archive of a podcast: every episode as its own taf in the library.

A subscription keeps one sticker playing the newest episodes; an archive keeps
them all, each on its own, in a library folder named after the podcast, to be
put on whichever sticker a person chooses. Nothing is ever deleted: an episode
the source no longer lists stays in the library.

The source is a feed, or a show of the ARD Audiothek (now ARD Sounds), whose
feeds hold only the latest episodes while its API lists every one. Episodes
are stored oldest first, at most BATCH in one job, so the one job worker is not
held for hours by a podcast of several hundred episodes; the next batch is
queued behind whatever else is waiting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from .encode import cut_to_bytes, encode_files, library_path, taf_exists
from .i18n import Problem
from .jobs import JobRunner
from .library import same
from .models import picture_suffix
from .podcast import (LARGEST_FEED, LARGEST_IMAGE, REFRESH_AFTER, TITLE_IN_NAME,
                      TITLE_IN_NAME_BYTES, WAKE_SECONDS, WORK_FOLDER, Episode, Feed, FeedError,
                      Fetch, download, episode_suffix, is_subscription_id, parse_feed)
from .store import LOCK, from_record, read_json, write_json
from .teddycloud import TeddyCloudClient

log = logging.getLogger(__name__)

FILE_NAME = "podcast-archives.json"

# Where the episodes' pictures are kept, beside the app's other data: they
# may go from the source like the episodes, and are needed for the model an
# episode gets on a sticker. One file per picture, however many episodes show it.
PICTURES = "archive-pictures"

# Episodes stored by one job.
BATCH = 10

AUDIOTHEK_HOSTS = ("ardaudiothek.de", "ardsounds.de")
AUDIOTHEK_API = "https://api.ardaudiothek.de"
# Episodes asked for per request; the API answers several hundred at once,
# but a smaller page keeps each answer well below LARGEST_FEED.
AUDIOTHEK_PAGE = 100
# The width asked for in the show's picture address, which leaves it open.
AUDIOTHEK_PICTURE_WIDTH = "1000"

SHOW = re.compile(r"urn:ard:show:[0-9a-f]+")
EPISODE = re.compile(r"urn:ard:episode:[0-9a-f]+")

# How much of an episode's id goes into a name when its title and date are both taken.
ID_IN_NAME = 20


@dataclass
class Stored:
    """One episode in the library."""

    path: str
    title: str = ""
    # When it was published, as ISO 8601; "" when the source did not say.
    published: str = ""
    seconds: Optional[int] = None
    # The file of its picture in PICTURES; "" without one.
    picture: str = ""


@dataclass
class Archive:
    id: str
    source: str
    # The library folder, fixed when the archive is made: a podcast that
    # renames itself must not start a second folder.
    folder: str = ""
    title: str = ""
    archived: Dict[str, Stored] = field(default_factory=dict)
    # The episode being stored and its target, written before its encode.
    storing: Dict[str, str] = field(default_factory=dict)
    total: int = 0
    checked: str = ""
    updated: str = ""
    error: str = ""
    error_key: str = ""
    error_params: Dict = field(default_factory=dict)


def read_source(url: str, fetch: Fetch = download) -> Feed:
    """The podcast at ``url``: a feed, or a show or episode link of the ARD Audiothek."""
    host = (urlparse(url).hostname or "").lower()
    if any(host == name or host.endswith("." + name) for name in AUDIOTHEK_HOSTS):
        return _audiothek(url, fetch)
    return parse_feed(fetch(url, LARGEST_FEED))


def _audiothek(url: str, fetch: Fetch) -> Feed:
    show = SHOW.search(url)
    if show:
        show_id = show.group(0)
    else:
        episode = EPISODE.search(url)
        if not episode:
            raise FeedError("archive.audiothek_link")
        item = _json(fetch(f"{AUDIOTHEK_API}/items/{episode.group(0)}", LARGEST_FEED))
        show_id = (((item.get("data") or {}).get("item") or {}).get("programSet") or {}).get("coreId")
        if not show_id:
            raise FeedError("archive.audiothek_link")

    title, image, nodes, offset = "", None, [], 0
    while True:
        answer = _json(fetch(f"{AUDIOTHEK_API}/programsets/{show_id}"
                             f"?offset={offset}&limit={AUDIOTHEK_PAGE}", LARGEST_FEED))
        show_set = (answer.get("data") or {}).get("programSet")
        if not show_set:
            raise FeedError("archive.audiothek_link")
        title = show_set.get("title") or title
        picture = (show_set.get("image") or {}).get("url1X1") or (show_set.get("image") or {}).get("url")
        image = image or (picture.replace("{width}", AUDIOTHEK_PICTURE_WIDTH) if picture else None)
        page = (show_set.get("items") or {}).get("nodes") or []
        nodes += page
        offset += len(page)
        if not page or offset >= (show_set.get("numberOfElements") or 0):
            break

    episodes = [episode for episode in map(_audiothek_episode, nodes) if episode]
    if not title:
        raise FeedError("feed.no_title")
    return Feed(title=title, image=image, episodes=episodes)


def _audiothek_episode(node: dict) -> Optional[Episode]:
    audios = node.get("audios") or []
    url = next((audio.get("downloadUrl") or audio.get("url") for audio in audios
                if audio.get("downloadUrl") or audio.get("url")), None)
    if not url or not node.get("id"):
        return None
    try:
        published = datetime.fromisoformat(node.get("publicationStartDateAndTime") or "")
    except ValueError:
        published = None
    picture = (node.get("image") or {}).get("url1X1") or (node.get("image") or {}).get("url")
    return Episode(guid=str(node["id"]), title=(node.get("title") or "").strip() or str(node["id"]),
                   url=url, published=published, seconds=node.get("duration") or None,
                   kind="audio/mpeg",
                   image=picture.replace("{width}", AUDIOTHEK_PICTURE_WIDTH) if picture else None)


def _json(data: bytes) -> dict:
    try:
        answer = json.loads(data)
    except ValueError as error:
        raise FeedError("feed.not_a_feed", detail=str(error)) from error
    return answer if isinstance(answer, dict) else {}


def refresh_archive(client: TeddyCloudClient, archive: Archive, data_dir: Path,
                    now: Optional[datetime] = None, fetch: Fetch = download,
                    most: Optional[int] = None) -> int:
    """Store episodes not stored yet, oldest first and at most ``most``; returns how many are left.

    Each episode is saved on the archive as soon as its taf is there, so a
    failure later in the batch costs only what was not stored yet.
    """
    now = now or datetime.now(timezone.utc)
    most = BATCH if most is None else most
    feed = read_source(archive.source, fetch)
    archive.title = feed.title
    archive.folder = archive.folder or feed.title
    archive.total = len(feed.episodes)
    archive.checked = now.isoformat()

    oldest = datetime.min.replace(tzinfo=timezone.utc)
    # The episode a crash interrupted first: storing the next one replaces its note.
    todo = sorted((episode for episode in feed.episodes if episode.guid not in archive.archived),
                  key=lambda episode: (episode.guid not in archive.storing,
                                       episode.published or oldest))
    for episode in todo[:most]:
        path = _store(client, archive, episode, data_dir, fetch)
        archive.archived[episode.guid] = Stored(
            path=path, title=episode.title,
            published=episode.published.isoformat() if episode.published else "",
            seconds=episode.seconds, picture=_keep_picture(data_dir, episode.image, fetch))
        archive.storing = {}
        archive.updated = now.isoformat()
        update_archive(data_dir, archive)
        log.info("archive %s stored %s", archive.id, path)

    archive.error, archive.error_key, archive.error_params = "", "", {}
    return max(0, len(todo) - most)


def _store(client: TeddyCloudClient, archive: Archive, episode: Episode, data_dir: Path,
           fetch: Fetch) -> str:
    """Download and encode one episode; returns the library path of its taf."""
    noted = archive.storing.get(episode.guid)
    if noted and taf_exists(client, noted):
        # Written before the app went away, but not saved on the archive.
        return noted

    target = _free_target(client, archive.folder, episode)
    archive.storing = {episode.guid: target}
    update_archive(data_dir, archive)

    if not is_subscription_id(archive.id):
        raise ValueError(f"not an archive id: {archive.id}")
    folder = data_dir / WORK_FOLDER / f"archive-{archive.id}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path = folder / f"episode{episode_suffix(episode)}"
        if fetch is download:
            download(episode.url, into=path)
        else:
            path.write_bytes(fetch(episode.url))
        encode_files(client, [path], target, total_seconds=episode.seconds)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return target


def _free_target(client: TeddyCloudClient, folder: str, episode: Episode) -> str:
    """The episode's title as its name, or with its date or id where that is taken."""
    title = cut_to_bytes(episode.title[:TITLE_IN_NAME], TITLE_IN_NAME_BYTES).strip() or episode.guid
    names = [title]
    if episode.published:
        names.append(f"{title} ({episode.published:%Y-%m-%d})")
    names.append(f"{title} ({episode.guid[-ID_IN_NAME:]})")
    for name in names:
        target = library_path(folder, name)
        if not taf_exists(client, target):
            return target
    raise FeedError("archive.name_taken", name=title)


def _keep_picture(data_dir: Path, url: Optional[str], fetch: Fetch) -> str:
    """Keep the picture at ``url`` in PICTURES; returns its file name, "" without one.

    A picture that cannot be had costs the episode its picture, not its place
    in the archive.
    """
    if not url:
        return ""
    name = f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.{picture_suffix(url)}"
    path = data_dir / PICTURES / name
    if path.exists():
        return name
    try:
        data = fetch(url, LARGEST_IMAGE)
    except (requests.RequestException, FeedError) as error:
        log.warning("no picture from %s: %s", url, error)
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return name


def picture_of(data_dir: Path, stored: Stored) -> Optional[bytes]:
    """The picture kept for an episode, or None."""
    if not stored.picture or "/" in stored.picture or stored.picture.startswith("."):
        return None
    path = data_dir / PICTURES / stored.picture
    return path.read_bytes() if path.is_file() else None


def episode_at(data_dir: Path, path: str) -> Optional[Stored]:
    """The archived episode whose taf is at the library path ``path``, or None."""
    wanted = same(path)
    for archive in load_archives(data_dir):
        for stored in archive.archived.values():
            if same(stored.path) == wanted:
                return stored
    return None


def load_archives(data_dir: Path) -> List[Archive]:
    with LOCK:
        return _read(data_dir)


def add_archive(data_dir: Path, source: str, title: str) -> Archive:
    """Store a new archive of ``source`` into the library folder ``title`` and return it."""
    archive = Archive(id=uuid.uuid4().hex[:10], source=source, folder=title, title=title)
    with LOCK:
        _write(data_dir, _read(data_dir) + [archive])
    return archive


def remove_archive(data_dir: Path, archive_id: str) -> None:
    """Stop archiving. What is in the library stays there."""
    with LOCK:
        _write(data_dir, [entry for entry in _read(data_dir) if entry.id != archive_id])


# What a refresh changes on an archive; the source and the folder are a person's.
REFRESHED = ("title", "archived", "storing", "total", "checked", "updated",
             "error", "error_key", "error_params")


def update_archive(data_dir: Path, archive: Archive) -> None:
    """Store what a refresh changed, but only if the archive still exists."""
    with LOCK:
        current = _read(data_dir)
        for entry in current:
            if entry.id == archive.id:
                for name in REFRESHED:
                    setattr(entry, name, getattr(archive, name))
                _write(data_dir, current)
                return


def _read(data_dir: Path) -> List[Archive]:
    """Under LOCK, which the caller holds for the whole read-change-write."""
    archives = []
    for entry in read_json(data_dir / FILE_NAME, []):
        if not (isinstance(entry, dict) and is_subscription_id(entry.get("id"))
                and entry.get("source")):
            continue
        archived = {guid: from_record(Stored, stored)
                    for guid, stored in (entry.get("archived") or {}).items()
                    if isinstance(stored, dict) and stored.get("path")}
        archives.append(from_record(Archive, {**entry, "archived": archived}))
    return archives


def _write(data_dir: Path, archives: List[Archive]) -> None:
    write_json(data_dir / FILE_NAME, [asdict(archive) for archive in archives])


class ArchiveUpdater:
    """Stores new episodes as jobs: on request, and on its own now and then."""

    def __init__(self, client: Callable[[], TeddyCloudClient], data_dir: Path,
                 jobs: JobRunner, fetch: Fetch):
        self.client = client
        self.data_dir = data_dir
        self.jobs = jobs
        self.fetch = fetch
        self._in_flight: set = set()
        self._lock = threading.Lock()

    def queue(self, archive_id: str) -> Optional[str]:
        """Start storing as a job, unless one for this archive is already under way."""
        with self._lock:
            if archive_id in self._in_flight:
                return None
            self._in_flight.add(archive_id)
        label = next((entry.title or entry.source for entry in load_archives(self.data_dir)
                      if entry.id == archive_id), archive_id)
        return self.jobs.submit("archive", label, lambda progress: self._refresh(archive_id))

    def run(self, stop: threading.Event) -> None:
        """Wake up now and then and look for new episodes, until ``stop``."""
        while not stop.wait(WAKE_SECONDS):
            try:
                self._queue_due(datetime.now(timezone.utc))
            except Exception:
                log.exception("archive round failed")

    def _queue_due(self, now: datetime) -> None:
        for archive in load_archives(self.data_dir):
            try:
                checked = datetime.fromisoformat(archive.checked)
            except ValueError:
                checked = None
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if checked is None or now - checked >= REFRESH_AFTER:
                self.queue(archive.id)

    def _refresh(self, archive_id: str) -> int:
        """Store the next batch of one archive; returns how many episodes are left."""
        left = 0
        try:
            archive = next((entry for entry in load_archives(self.data_dir)
                            if entry.id == archive_id), None)
            if archive is None:
                return 0
            try:
                left = refresh_archive(self.client(), archive, self.data_dir, fetch=self.fetch)
            except Exception as error:
                if isinstance(error, Problem):
                    archive.error = ""
                    archive.error_key, archive.error_params = error.key, error.params
                else:
                    archive.error = str(error) or error.__class__.__name__
                    archive.error_key, archive.error_params = "", {}
                archive.checked = datetime.now(timezone.utc).isoformat()
                update_archive(self.data_dir, archive)
                raise
            update_archive(self.data_dir, archive)
        finally:
            with self._lock:
                self._in_flight.discard(archive_id)
        if left:
            self.queue(archive_id)
        return left
