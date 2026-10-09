"""A tonie that brings the newest episodes of a podcast.

A subscription ties a feed to a sticker. On every refresh the newest episodes
are downloaded, encoded into one taf - newest first, one chapter each - and the
sticker is pointed at it. Unlike a radio tonie this plays offline once the box
has it, and has no six-hour stream limit.

The order of a refresh is what keeps the sticker playing throughout: the new
taf is built under a new name, the tag is switched to it, and only then is the
old one deleted. Encoding over the old taf would leave the tonie silent for the
whole encode, and with nothing at all if the encode failed.

Feeds come from elsewhere, so what is fetched is bounded: only http and https,
only addresses on the public internet - an episode URL is written by whoever
wrote the feed, and could otherwise point the pod at teddyCloud or anything
else inside the cluster - a timeout on every request, a cap on every download,
and no XML entity declarations, which have no business in a feed and are how an
XML document grows into gigabytes while being read.

A subscription never takes a sticker back. If the sticker plays something other
than what the last refresh put there, someone has used it for something else,
and the refresh stops and says so instead of overwriting it.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import socket
import threading
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from .album import AUDIO_EXTENSIONS
from .encode import EncodeError, cut_to_bytes, delete_taf, encode_files, library_path
from .i18n import Problem
from .jobs import JobRunner
from .library import is_library, path_of, played, same
from .models import COVER_TYPES, find_model
from .runtimes import remember
from .store import LOCK, read_json, write_json
from .tags import find_tag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import publish

log = logging.getLogger(__name__)

FILE_NAME = "podcasts.json"

# The library folder every podcast taf lives in, beside the albums' series.
SERIES = "Podcasts"

# What the tonie's episode line says; the tracks list the actual episodes.
EPISODE_LINE = "Neueste Folgen"

DEFAULT_KEEP = 5

# Children's podcast episodes run from ten minutes to an hour; this is several
# times the largest of them and still something a pod can hold in memory.
LARGEST_DOWNLOAD = 300 * 1024 * 1024

# A feed is text and a cover is one picture; either at this size is not one.
LARGEST_FEED = 10 * 1024 * 1024
LARGEST_IMAGE = 10 * 1024 * 1024

MOST_REDIRECTS = 5

# How much of the podcast's title goes into a taf's name, in characters and in
# bytes. Names are cut at MAX_NAME and MAX_NAME_BYTES, and what tells one
# refresh's taf from the next - the time stamp after the title - must never be
# the part that is cut off.
TITLE_IN_NAME = 80
TITLE_IN_NAME_BYTES = 160

PICTURE_SUFFIXES = frozenset(suffix.lstrip(".") for suffix in COVER_TYPES)

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

AUDIO_TYPES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/opus": ".opus",
}

# fetch(url, limit) -> the body, refusing anything larger than ``limit``.
Fetch = Callable[..., bytes]


class FeedError(Problem, ValueError):
    """A feed that cannot be read or has nothing to play."""


@dataclass
class Episode:
    guid: str
    title: str
    url: str
    published: Optional[datetime] = None
    seconds: Optional[int] = None
    kind: str = ""


@dataclass
class Feed:
    title: str
    image: Optional[str]
    episodes: List[Episode]


@dataclass
class Subscription:
    """One podcast on one sticker, and what it played after the last refresh."""

    id: str
    feed: str
    ruid: str
    keep: int = DEFAULT_KEEP
    title: str = ""
    model: str = ""
    source: str = ""
    # What the sticker played when it was chosen, so the first refresh can
    # tell a sticker given something else meanwhile from the one chosen.
    # None in a subscription stored without it: that refresh cannot tell.
    played_before: Optional[str] = None
    guids: List[str] = field(default_factory=list)
    checked: str = ""
    updated: str = ""
    # What went wrong last time: a key and its values for the reader's
    # language, or raw text from a part that does not speak it (teddyCloud).
    error: str = ""
    error_key: str = ""
    error_params: Dict = field(default_factory=dict)


def parse_feed(data: bytes) -> Feed:
    """The feed's title, cover and episodes that carry audio, in feed order."""
    if b"<!ENTITY" in data:
        raise FeedError("feed.entities")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise FeedError("feed.not_a_feed", detail=str(error)) from error
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError("feed.not_rss")

    image = None
    itunes_image = channel.find(f"{ITUNES}image")
    if itunes_image is not None and itunes_image.get("href"):
        image = itunes_image.get("href")
    elif channel.findtext("image/url"):
        image = channel.findtext("image/url").strip()

    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        url = enclosure.get("url").strip()
        kind = (enclosure.get("type") or "").lower()
        if not (kind.startswith("audio/") or _suffix(url) in AUDIO_EXTENSIONS):
            continue
        episodes.append(Episode(
            guid=(item.findtext("guid") or url).strip(),
            title=(item.findtext("title") or "").strip() or url.rsplit("/", 1)[-1],
            url=url,
            published=_date(item.findtext("pubDate")),
            seconds=_seconds(item.findtext(f"{ITUNES}duration")),
            kind=kind,
        ))

    title = (channel.findtext("title") or "").strip()
    if not title:
        raise FeedError("feed.no_title")
    return Feed(title=title, image=image, episodes=episodes)


def newest(feed: Feed, count: int) -> List[Episode]:
    """The ``count`` most recent episodes, newest first; undated ones last."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(feed.episodes, key=lambda episode: episode.published or oldest, reverse=True)
    return ordered[:max(1, count)]


def download(url: str, limit: int = LARGEST_DOWNLOAD) -> bytes:
    """Fetch ``url`` within the limits a feed from elsewhere deserves.

    Redirects are followed by hand, so every address on the way is checked,
    not only the first one.
    """
    for _ in range(MOST_REDIRECTS + 1):
        check_public(url)
        with requests.get(url, timeout=(10, 60), stream=True, allow_redirects=False,
                          headers={"User-Agent": "tonie-studio"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise FeedError("feed.too_large", url=url, megabytes=limit // 1048576)
                chunks.append(chunk)
            return b"".join(chunks)
    raise FeedError("feed.redirects", count=MOST_REDIRECTS, url=url)


def check_public(url: str, resolve: Callable = socket.getaddrinfo) -> None:
    """Refuse any address that is not on the public internet.

    The name is resolved here and again by the request itself, so a name that
    answers differently the second time is not caught. That narrows what a
    hostile feed can reach to someone running such a name server for it.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FeedError("feed.not_http", url=url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = resolve(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as error:
        raise FeedError("feed.not_found", host=parsed.hostname, detail=str(error)) from error
    for answer in answers:
        address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
        if not address.is_global:
            raise FeedError("feed.not_public", host=parsed.hostname, address=str(address))


def refresh(
    client: TeddyCloudClient,
    subscription: Subscription,
    data_dir: Path,
    now: Optional[datetime] = None,
    fetch: Fetch = download,
) -> bool:
    """Bring the sticker up to date. Returns whether anything changed.

    Raises when the feed or the encode fails; the sticker then still plays
    what it played before.
    """
    now = now or datetime.now(timezone.utc)
    feed = parse_feed(fetch(subscription.feed, LARGEST_FEED))
    chosen = newest(feed, subscription.keep)
    if not chosen:
        raise FeedError("feed.no_audio")

    subscription.checked = now.isoformat()
    subscription.title = feed.title
    _check_sticker(client, subscription)
    guids = [episode.guid for episode in chosen]
    if guids == subscription.guids and subscription.source:
        # Nothing new, but the feed answered: whatever went wrong last time
        # is over, and the page must stop saying it.
        subscription.error, subscription.error_key, subscription.error_params = "", "", {}
        return False

    folder = data_dir / "podcasts" / subscription.id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        paths = []
        for number, episode in enumerate(chosen, 1):
            path = folder / f"{number:03d}{_episode_suffix(episode)}"
            path.write_bytes(fetch(episode.url))
            paths.append(path)
        seconds = sum(episode.seconds or 0 for episode in chosen) or None
        # A new name every time, so the old taf keeps playing until the new
        # one is complete and the tag has been switched over.
        title = cut_to_bytes(feed.title[:TITLE_IN_NAME], TITLE_IN_NAME_BYTES).strip()
        target = library_path(SERIES, f"{title} {now:%Y-%m-%d %H%M%S}")
        encoded = encode_files(client, paths, target, total_seconds=seconds)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    try:
        # Again, right before writing: the download and the encode took
        # minutes, and the sticker may have been given something else since.
        _check_sticker(client, subscription)
        cover = _cover(client, subscription, feed, fetch)
        model_id = publish(
            client, subscription.ruid, encoded, series=feed.title, episode=EPISODE_LINE,
            tracks=[episode.title for episode in chosen], cover=cover,
            cover_name=_picture_suffix(feed.image or ""),
            model_id=subscription.model or None,
        )
    except Exception:
        # The sticker still plays the old episodes; the new taf would only be
        # a file nothing points at.
        _delete_quietly(client, encoded.target, subscription.id)
        raise
    remember(data_dir, model_id, seconds)

    old = subscription.source
    subscription.model = model_id
    subscription.source = encoded.source
    subscription.guids = guids
    subscription.updated = now.isoformat()
    subscription.error, subscription.error_key, subscription.error_params = "", "", {}
    if old and old != encoded.source and is_library(old) and not _still_played(client, old):
        _delete_quietly(client, path_of(old), subscription.id)
    log.info("podcast %s now plays %d episodes from %s", subscription.id, len(chosen), feed.title)
    return True


def _check_sticker(client: TeddyCloudClient, subscription: Subscription) -> None:
    """Refuse to touch a sticker that now plays something else."""
    tag = find_tag(client, subscription.ruid)
    if tag is None:
        raise FeedError("feed.sticker_unknown")
    expected = subscription.source or subscription.played_before
    if expected is None:
        return
    playing = tag.source or ""
    if same(playing) != same(expected):
        raise FeedError("feed.sticker_reused", playing=playing or "-")


def _cover(client: TeddyCloudClient, subscription: Subscription, feed: Feed,
           fetch: Fetch) -> Optional[bytes]:
    """The feed's picture for a new model; an existing one keeps its own."""
    if subscription.model and find_model(client, subscription.model):
        return None
    if not feed.image:
        return None
    try:
        return fetch(feed.image, LARGEST_IMAGE)
    except (requests.RequestException, FeedError) as error:
        log.warning("podcast %s: no cover from %s: %s", subscription.id, feed.image, error)
        return None


def _still_played(client: TeddyCloudClient, source: str) -> bool:
    """Whether any sticker plays ``source``, such as a copy made for grandma's."""
    return same(source) in played(tag.source for tag in list_tags(client))


def _delete_quietly(client: TeddyCloudClient, path: str, subscription_id: str) -> None:
    try:
        delete_taf(client, path)
    except (TeddyCloudError, EncodeError) as error:
        # A leftover file is for the checkup page, not a reason to fail.
        log.warning("podcast %s: %s stays: %s", subscription_id, path, error)


def _picture_suffix(url: str) -> str:
    suffix = _suffix(url).lstrip(".")
    return suffix if suffix in PICTURE_SUFFIXES else "jpg"


def load_subscriptions(data_dir: Path) -> List[Subscription]:
    with LOCK:
        return _read(data_dir)


def save_subscriptions(data_dir: Path, subscriptions: List[Subscription]) -> None:
    with LOCK:
        _write(data_dir, subscriptions)


def add_subscription(data_dir: Path, subscription: Subscription) -> None:
    with LOCK:
        current = [entry for entry in _read(data_dir) if entry.id != subscription.id]
        _write(data_dir, current + [subscription])


def remove_subscription(data_dir: Path, subscription_id: str) -> None:
    with LOCK:
        _write(data_dir, [entry for entry in _read(data_dir) if entry.id != subscription_id])


def update_subscription(data_dir: Path, subscription: Subscription) -> None:
    """Store a changed subscription, but only if it still exists.

    A refresh works on its own copy for minutes. If the subscription was
    removed meanwhile, writing that copy back would quietly undo the removal.
    """
    with LOCK:
        current = _read(data_dir)
        if any(entry.id == subscription.id for entry in current):
            _write(data_dir, [subscription if entry.id == subscription.id else entry
                              for entry in current])


def _read(data_dir: Path) -> List[Subscription]:
    """Under LOCK, which the caller holds for the whole read-change-write."""
    stored = read_json(data_dir / FILE_NAME, [])
    known = {item.name for item in fields(Subscription)}
    subscriptions = []
    for entry in stored if isinstance(stored, list) else []:
        if isinstance(entry, dict) and entry.get("id") and entry.get("feed"):
            subscriptions.append(Subscription(**{key: value for key, value in entry.items()
                                                 if key in known}))
    return subscriptions


def _write(data_dir: Path, subscriptions: List[Subscription]) -> None:
    write_json(data_dir / FILE_NAME, [asdict(subscription) for subscription in subscriptions])


def _episode_suffix(episode: Episode) -> str:
    suffix = _suffix(episode.url)
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    return AUDIO_TYPES.get(episode.kind.split(";")[0].strip(), ".mp3")


def _suffix(url: str) -> str:
    return PurePosixPath(urlparse(url).path).suffix.lower()


def _date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds(text: Optional[str]) -> Optional[int]:
    """itunes:duration as seconds; it comes as "1:02:03", "24:10" or "1450"."""
    if not text or not re.fullmatch(r"\d+(:\d+){0,2}", text.strip()):
        return None
    total = 0
    for part in text.strip().split(":"):
        total = total * 60 + int(part)
    return total or None


# How often the updater wakes up, and how old a check has to be before it
# looks again. Children's podcasts publish weekly at most; twice a day finds a
# new episode before the next bedtime without asking a feed every hour.
WAKE_SECONDS = 3600
REFRESH_AFTER = timedelta(hours=12)


class PodcastUpdater:
    """Refreshes subscriptions as jobs: on request, and on its own now and then.

    One refresh per subscription at a time, so a slow feed is not queued
    twice. The podcast series folder is busy while any refresh runs, because
    a refresh builds its taf under a name only known once it runs.
    """

    def __init__(self, client: Callable[[], TeddyCloudClient], data_dir: Path,
                 jobs: JobRunner, fetch: Fetch):
        self.client = client
        self.data_dir = data_dir
        self.jobs = jobs
        self.fetch = fetch
        self._in_flight: set = set()
        self._lock = threading.Lock()

    @property
    def refreshing(self) -> bool:
        with self._lock:
            return bool(self._in_flight)

    def queue(self, subscription_id: str) -> Optional[str]:
        """Start a refresh as a job, unless one for it is already under way."""
        with self._lock:
            if subscription_id in self._in_flight:
                return None
            self._in_flight.add(subscription_id)
        label = next((entry.title or entry.feed for entry in load_subscriptions(self.data_dir)
                      if entry.id == subscription_id), subscription_id)
        return self.jobs.submit("podcast", label, lambda progress: self._refresh(subscription_id))

    def run(self, stop: threading.Event) -> None:
        """Wake up now and then and refresh what has not been checked lately, until ``stop``."""
        while not stop.wait(WAKE_SECONDS):
            # Whatever goes wrong in one round, the next comes all the same:
            # a dead loop would stop every refresh until the app restarts.
            try:
                self._queue_due(datetime.now(timezone.utc))
            except Exception:
                log.exception("podcast round failed")

    def _queue_due(self, now: datetime) -> None:
        """Queue every subscription not checked for REFRESH_AFTER."""
        for subscription in load_subscriptions(self.data_dir):
            try:
                checked = datetime.fromisoformat(subscription.checked)
            except ValueError:
                checked = None
            # This app writes times with their zone; one without, as a hand-edited
            # file may hold, is read as UTC rather than failing the comparison.
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if checked is None or now - checked >= REFRESH_AFTER:
                self.queue(subscription.id)

    def _refresh(self, subscription_id: str) -> bool:
        """Refresh one subscription; True when its sticker now plays something new."""
        try:
            subscription = next((entry for entry in load_subscriptions(self.data_dir)
                                 if entry.id == subscription_id), None)
            if subscription is None:
                return False
            try:
                changed = refresh(self.client(), subscription, self.data_dir, fetch=self.fetch)
            except Exception as error:
                # Kept on the subscription, so the podcast page can say why the
                # episodes are old, not only the job list. A problem this app
                # describes is kept as its key, to be said in the reader's
                # language; anything else as it came.
                if isinstance(error, Problem):
                    subscription.error = ""
                    subscription.error_key, subscription.error_params = error.key, error.params
                else:
                    subscription.error = str(error) or error.__class__.__name__
                    subscription.error_key, subscription.error_params = "", {}
                subscription.checked = datetime.now(timezone.utc).isoformat()
                update_subscription(self.data_dir, subscription)
                raise
            update_subscription(self.data_dir, subscription)
            return changed
        finally:
            with self._lock:
                self._in_flight.discard(subscription_id)
