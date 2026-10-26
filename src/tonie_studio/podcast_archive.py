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
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from .encode import (EncodeError, Progress, cut_to_bytes, delete_taf, encode_files,
                     encode_library, library_path, taf_exists)
from .i18n import Problem
from .jobs import JobRunner
from .languages import DEFAULT, STORED
from .library import same
from .models import picture_suffix
from .podcast import (LARGEST_FEED, LARGEST_IMAGE, MOST_KEEP, REFRESH_AFTER, TITLE_IN_NAME,
                      TITLE_IN_NAME_BYTES, WAKE_SECONDS, WORK_FOLDER, Episode, Feed, FeedError,
                      Fetch, download, episode_suffix, is_subscription_id, parse_feed)
from .runtimes import remember
from .store import LOCK, from_record, read_json, write_json
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import publish

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

# The most episodes put together onto one sticker: as many as a podcast
# subscription keeps there.
MOST_TOGETHER = MOST_KEEP


class ArchiveError(Problem, ValueError):
    """A choice of archived episodes that cannot be put together."""


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
    # What it speaks, as models store it, such as "de-de"; "" when unknown.
    language: str = ""


@dataclass
class Missing:
    """An episode the source lists but whose audio it no longer has."""

    title: str = ""
    # When its audio was last asked for, as ISO 8601.
    checked: str = ""


@dataclass
class Archive:
    id: str
    source: str
    # The library folder, fixed when the archive is made: a podcast that
    # renames itself must not start a second folder.
    folder: str = ""
    title: str = ""
    # What the source speaks, as models store it; "" when it does not say.
    language: str = ""
    archived: Dict[str, Stored] = field(default_factory=dict)
    # The episode being stored and its target, written before its encode.
    storing: Dict[str, str] = field(default_factory=dict)
    missing: Dict[str, Missing] = field(default_factory=dict)
    total: int = 0
    checked: str = ""
    updated: str = ""
    error: str = ""
    error_key: str = ""
    error_params: Dict = field(default_factory=dict)
    # Seconds spent storing ``timed`` episodes, download and encode, for how
    # long the rest will take; episodes stored before this was kept are not in it.
    spent: float = 0.0
    timed: int = 0


def read_source(url: str, fetch: Fetch = download, glance: bool = False) -> Feed:
    """The podcast at ``url``: a feed, or a show or episode link of the ARD Audiothek.

    ``glance`` reads only the first page of an Audiothek show: enough to tell
    that the link works, while a show of a thousand episodes takes longer to
    read whole than a person on the page may be kept waiting.
    """
    host = (urlparse(url).hostname or "").lower()
    if any(host == name or host.endswith("." + name) for name in AUDIOTHEK_HOSTS):
        return _audiothek(url, fetch, glance)
    return parse_feed(fetch(url, LARGEST_FEED))


def _audiothek(url: str, fetch: Fetch, glance: bool = False) -> Feed:
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
        if glance or not page or offset >= (show_set.get("numberOfElements") or 0):
            break

    episodes = [episode for episode in map(_audiothek_episode, nodes) if episode]
    if not title:
        raise FeedError("feed.no_title")
    # The Audiothek is the ARD's, and says nothing of a show's language.
    return Feed(title=title, image=image, episodes=episodes, language="de")


def _audiothek_episode(node: dict) -> Optional[Episode]:
    urls = list(dict.fromkeys(audio.get("downloadUrl") or audio.get("url")
                              for audio in node.get("audios") or []
                              if audio.get("downloadUrl") or audio.get("url")))
    if not urls or not node.get("id"):
        return None
    try:
        published = datetime.fromisoformat(node.get("publicationStartDateAndTime") or "")
    except ValueError:
        published = None
    picture = (node.get("image") or {}).get("url1X1") or (node.get("image") or {}).get("url")
    return Episode(guid=str(node["id"]), title=(node.get("title") or "").strip() or str(node["id"]),
                   url=urls[0], others=urls[1:], published=published,
                   seconds=node.get("duration") or None,
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
                    most: Optional[int] = None, progress: Optional[Callable] = None) -> int:
    """Store episodes not stored yet, oldest first and at most ``most``; returns how many are left.

    Each episode is saved on the archive as soon as its taf is there, so a
    failure later in the batch costs only what was not stored yet.
    ``progress`` hears ARCHIVE_STEP with the episodes of the round stored so
    far, how many the round takes, and the title of the one it stores now.
    """
    now = now or datetime.now(timezone.utc)
    most = BATCH if most is None else most
    feed = read_source(archive.source, fetch)
    archive.title = feed.title
    archive.folder = archive.folder or feed.title
    archive.language = stored_language(feed.language) or archive.language
    archive.total = len(feed.episodes)
    archive.checked = now.isoformat()

    oldest = datetime.min.replace(tzinfo=timezone.utc)
    # The episode a crash interrupted first: storing the next one replaces its note.
    todo = sorted((episode for episode in feed.episodes if episode.guid not in archive.archived
                   and not _resting(archive.missing.get(episode.guid), now)),
                  key=lambda episode: (episode.guid not in archive.storing,
                                       episode.published or oldest))
    stored = 0
    round_size = min(most, len(todo))
    for episode in todo:
        if stored >= most:
            break
        if progress:
            progress(ARCHIVE_STEP, stored, round_size, current=episode.title)
        began = time.monotonic()
        try:
            path = _store(client, archive, episode, data_dir, fetch)
        except _Gone:
            # Passed over, so the episodes after it are still stored; asked
            # for again after REFRESH_AFTER, should the source mend it.
            archive.missing[episode.guid] = Missing(title=episode.title, checked=now.isoformat())
            archive.storing = {}
            update_archive(data_dir, archive)
            log.warning("archive %s: the source no longer has the audio of %s",
                        archive.id, episode.title)
            continue
        stored += 1
        archive.spent += time.monotonic() - began
        archive.timed += 1
        archive.missing.pop(episode.guid, None)
        archive.archived[episode.guid] = Stored(
            path=path, title=episode.title,
            published=episode.published.isoformat() if episode.published else "",
            seconds=episode.seconds, picture=_keep_picture(data_dir, episode.image, fetch),
            language=archive.language)
        archive.storing = {}
        archive.updated = now.isoformat()
        update_archive(data_dir, archive)
        log.info("archive %s stored %s", archive.id, path)

    if progress:
        progress(ARCHIVE_STEP, stored, round_size, current="")
    archive.error, archive.error_key, archive.error_params = "", "", {}
    return sum(1 for episode in todo
               if episode.guid not in archive.archived and episode.guid not in archive.missing)


# The step a refresh reports its episodes under; see refresh_archive.
ARCHIVE_STEP = "archive"

# What a server answers for audio it no longer has; anything else may mend
# itself and fails the run, to be tried again.
GONE = (404, 410)


class _Gone(Exception):
    """No address of an episode has its audio any more."""


def stored_language(code: Optional[str]) -> str:
    """A source's language as models store it, "de" as "de-de"; "" for none."""
    code = (code or "").strip().lower().replace("_", "-")
    if "-" in code:
        return code
    return STORED.get(code, "")


def _resting(missing: Optional[Missing], now: datetime) -> bool:
    """Whether a missing episode was asked for too recently to be asked again."""
    if missing is None:
        return False
    try:
        checked = datetime.fromisoformat(missing.checked)
    except ValueError:
        return False
    return now - checked < REFRESH_AFTER


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
        _fetch_audio(episode, path, fetch)
        encode_files(client, [path], target, total_seconds=episode.seconds)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return target


def _fetch_audio(episode: Episode, path: Path, fetch: Fetch) -> None:
    """Download the episode from the first of its addresses that still has it."""
    for url in [episode.url] + episode.others:
        try:
            if fetch is download:
                download(url, into=path)
            else:
                path.write_bytes(fetch(url))
            return
        except requests.HTTPError as error:
            if getattr(error.response, "status_code", None) not in GONE:
                raise
            log.info("%s is gone: %s", url, error)
    raise _Gone(episode.guid)


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


# What _keep_picture names a picture: never a path, so a name from a request
# cannot reach another file.
PICTURE_NAME = re.compile(r"[0-9a-f]{20}\.[a-z0-9]{2,5}")


def picture_path(data_dir: Path, name: str) -> Optional[Path]:
    """The file of a kept picture, or None for a name _keep_picture does not give or a gone file."""
    if not PICTURE_NAME.fullmatch(name or ""):
        return None
    path = data_dir / PICTURES / name
    return path if path.is_file() else None


def picture_of(data_dir: Path, stored: Stored) -> Optional[bytes]:
    """The picture kept for an episode, or None."""
    path = picture_path(data_dir, stored.picture)
    return path.read_bytes() if path else None


def stored_by_path(data_dir: Path) -> Dict[str, Stored]:
    """Every archived episode by the library path of its taf, as ``same`` spells it."""
    return {same(stored.path): stored for archive in load_archives(data_dir)
            for stored in archive.archived.values()}


def episode_at(data_dir: Path, path: str) -> Optional[Stored]:
    """The archived episode whose taf is at the library path ``path``, or None."""
    return stored_by_path(data_dir).get(same(path))


def put_together(client: TeddyCloudClient, archive: Archive, guids: List[str], name: str,
                 ruid: str, data_dir: Path, progress: Optional[Progress] = None) -> str:
    """Put the chosen episodes onto the sticker ``ruid`` as one taf named ``name``.

    The episodes come in the order they came out, one chapter each, from their
    tafs in the library: an episode the source has taken down since is put
    together all the same. The tonie shows the first episode's picture.
    Returns the model id. Should the sticker not take it, the new taf goes.
    """
    chosen = sorted((archive.archived[guid] for guid in dict.fromkeys(guids)
                     if guid in archive.archived), key=lambda stored: stored.published)
    if not chosen:
        raise ArchiveError("archive.choose_episodes")
    if len(chosen) > MOST_TOGETHER:
        raise ArchiveError("archive.too_many", most=MOST_TOGETHER)
    seconds = sum(stored.seconds or 0 for stored in chosen) or None
    encoded = encode_library(client, [stored.path for stored in chosen],
                             library_path(archive.folder or archive.title, name), progress,
                             seconds)
    cover = picture_of(data_dir, chosen[0])
    try:
        model_id = publish(client, ruid, encoded, series=archive.title or archive.folder,
                           episode=name, tracks=[stored.title for stored in chosen], cover=cover,
                           cover_name=chosen[0].picture.rsplit(".", 1)[-1] if cover else "jpg",
                           language=archive.language or DEFAULT, data_dir=data_dir)
    except Exception:
        try:
            delete_taf(client, encoded.target)
        except (TeddyCloudError, EncodeError) as error:
            log.warning("archive %s: %s stays: %s", archive.id, encoded.target, error)
        raise
    remember(data_dir, model_id, seconds)
    return model_id


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
REFRESHED = ("title", "language", "archived", "storing", "missing", "total", "checked", "updated",
             "error", "error_key", "error_params", "spent", "timed")


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
        # An episode stored before its archive knew a language speaks the archive's.
        archived = {guid: from_record(Stored, {"language": entry.get("language") or "", **stored})
                    for guid, stored in (entry.get("archived") or {}).items()
                    if isinstance(stored, dict) and stored.get("path")}
        for stored in archived.values():
            stored.language = stored.language or entry.get("language") or ""
        missing = {guid: from_record(Missing, gone)
                   for guid, gone in (entry.get("missing") or {}).items() if isinstance(gone, dict)}
        archives.append(from_record(Archive, {**entry, "archived": archived, "missing": missing}))
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
        # Which archive each job stores for, so its page can say how far that one is.
        self._archive_of: Dict[str, str] = {}

    def queue(self, archive_id: str) -> Optional[str]:
        """Start storing as a job, unless one for this archive is already under way."""
        with self._lock:
            if archive_id in self._in_flight:
                return None
            self._in_flight.add(archive_id)
        label = next((entry.title or entry.source for entry in load_archives(self.data_dir)
                      if entry.id == archive_id), archive_id)
        job_id = self.jobs.submit("archive", label,
                                  lambda progress: self._refresh(archive_id, progress))
        with self._lock:
            self._archive_of[job_id] = archive_id
        return job_id

    def running(self, archive_id: str) -> Optional[str]:
        """The job storing for ``archive_id`` now, queued or under way; None when there is none."""
        with self._lock:
            if archive_id not in self._in_flight:
                return None
            return next((job_id for job_id, of in reversed(list(self._archive_of.items()))
                         if of == archive_id), None)

    def archive_of(self, job_id: str) -> Optional[Archive]:
        """The archive the job ``job_id`` stores for, as it stands now; None for another job."""
        with self._lock:
            archive_id = self._archive_of.get(job_id)
        return next((entry for entry in load_archives(self.data_dir) if entry.id == archive_id),
                    None) if archive_id else None

    def run(self, stop: threading.Event) -> None:
        """Carry on what a restart cut short, then look for new episodes now and then, until ``stop``."""
        try:
            self.carry_on()
        except Exception:
            log.exception("carrying on the archives failed")
        while not stop.wait(WAKE_SECONDS):
            try:
                self._queue_due(datetime.now(timezone.utc))
            except Exception:
                log.exception("archive round failed")

    def carry_on(self) -> None:
        """Start a round for every archive with episodes still to store.

        Rounds follow each other only while the app runs; after a restart the
        next would come REFRESH_AFTER after the last check, half a day later.
        An episode a store was cut off in counts as still to store.
        """
        for archive in load_archives(self.data_dir):
            if archive.storing or archive.total > len(archive.archived) + len(archive.missing):
                self.queue(archive.id)

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

    def _refresh(self, archive_id: str, progress: Optional[Callable] = None) -> int:
        """Store the next batch of one archive; returns how many episodes are left."""
        left = 0
        try:
            archive = next((entry for entry in load_archives(self.data_dir)
                            if entry.id == archive_id), None)
            if archive is None:
                return 0
            # The failure of the last round is not this one's; it comes back if it is.
            if archive.error or archive.error_key:
                archive.error, archive.error_key, archive.error_params = "", "", {}
                update_archive(self.data_dir, archive)
            try:
                left = refresh_archive(self.client(), archive, self.data_dir, fetch=self.fetch,
                                       progress=progress)
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
