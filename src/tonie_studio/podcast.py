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
import time
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from .album import AUDIO_EXTENSIONS
from .encode import (EncodeError, EncodeResult, cut_to_bytes, delete_taf, encode_files,
                     library_path, taf_exists)
from .i18n import Problem
from .jobs import JobRunner
from .library import is_library, path_of, played, same
from .models import WRITING, find_model, picture_suffix
from .runtimes import remember
from .store import LOCK, from_record, read_json, write_json
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
# The most episodes one sticker carries; more would not fit its memory and
# would take an evening to encode.
MOST_KEEP = 20

# Children's podcast episodes run from ten minutes to an hour; this is several
# times the largest of them and still something a pod can hold in memory.
LARGEST_DOWNLOAD = 300 * 1024 * 1024

# A feed is text and a cover is one picture; either at this size is not one.
LARGEST_FEED = 10 * 1024 * 1024
LARGEST_IMAGE = 10 * 1024 * 1024

MOST_REDIRECTS = 5

# Where a refresh keeps the episodes it downloads until they are encoded;
# emptied as each refresh ends, swept by housekeeping after a crash.
WORK_FOLDER = "podcasts"

# How long one download may take, start to end, in seconds. The timeout of a
# request only bounds the wait for each piece, so a host sending a byte a
# minute would hold the one job worker, and every encode behind it, for days.
# Half an hour fetches the largest episode allowed at some 170 KB/s.
LONGEST_DOWNLOAD = 30 * 60

# How much of the podcast's title goes into a taf's name, in characters and in
# bytes. Names are cut at MAX_NAME and MAX_NAME_BYTES, and what tells one
# refresh's taf from the next - the time stamp after the title - must never be
# the part that is cut off.
TITLE_IN_NAME = 80
TITLE_IN_NAME_BYTES = 160

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


def download(url: str, limit: int = LARGEST_DOWNLOAD, into: Optional[Path] = None) -> bytes:
    """Fetch ``url`` within the limits a feed from elsewhere deserves.

    Redirects are followed by hand, so every address on the way is checked,
    not only the first one. With ``into`` the body is written to that file as
    it arrives and nothing is returned: an episode held whole and then joined
    would need twice its size in memory. A download that fails leaves no file.
    """
    for _ in range(MOST_REDIRECTS + 1):
        check_public(url)
        with requests.get(url, timeout=(10, 60), stream=True, allow_redirects=False,
                          headers={"User-Agent": "tonie-studio"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if into is None:
                return b"".join(_chunks(response, url, limit))
            try:
                with into.open("wb") as target:
                    for chunk in _chunks(response, url, limit):
                        target.write(chunk)
            except BaseException:
                into.unlink(missing_ok=True)
                raise
            return b""
    raise FeedError("feed.redirects", count=MOST_REDIRECTS, url=url)


def _chunks(response, url: str, limit: int) -> Iterator[bytes]:
    """The body in pieces, given up on past ``limit`` bytes or LONGEST_DOWNLOAD seconds."""
    size = 0
    give_up = time.monotonic() + LONGEST_DOWNLOAD
    for chunk in response.iter_content(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise FeedError("feed.too_large", url=url, megabytes=limit // 1048576)
        if time.monotonic() > give_up:
            raise FeedError("feed.too_slow", url=url, minutes=LONGEST_DOWNLOAD // 60)
        yield chunk


def check_public(url: str, resolve: Optional[Callable] = None) -> None:
    """Refuse any address that is not on the public internet.

    The name is resolved here and again by the request itself, so a name that
    answers differently the second time is not caught. That narrows what a
    hostile feed can reach to someone running such a name server for it.
    ``resolve`` defaults to socket.getaddrinfo, looked up on each call.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FeedError("feed.not_http", url=url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        # A port that is not a number, or out of range: a typo in the address.
        raise FeedError("feed.bad_address", url=url) from error
    try:
        answers = (resolve or socket.getaddrinfo)(parsed.hostname, port,
                                                  type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as error:
        raise FeedError("feed.not_found", host=parsed.hostname, detail=str(error)) from error
    for answer in answers:
        address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
        if not address.is_global or not _embedded_ipv4_global(address):
            raise FeedError("feed.not_public", host=parsed.hostname, address=str(address))


# NAT64 addresses carry an IPv4 address in their last 32 bits and count as
# global themselves, whatever that address is.
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4_global(address) -> bool:
    """False for a NAT64 address whose IPv4 address is not public; True otherwise."""
    if address.version == 6 and address in NAT64:
        return ipaddress.ip_address(int(address) & 0xFFFFFFFF).is_global
    return True


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
    if guids == subscription.guids and subscription.source and _audio_there(client, subscription):
        # Nothing new, but the feed answered: whatever went wrong last time
        # is over, and the page must stop saying it.
        subscription.error, subscription.error_key, subscription.error_params = "", "", {}
        return False

    seconds = sum(episode.seconds or 0 for episode in chosen) or None
    encoded = _encode_episodes(client, data_dir / WORK_FOLDER / subscription.id, feed, chosen,
                               now, fetch, seconds)
    model_id = _put_on_sticker(client, subscription, feed, chosen, encoded, fetch, data_dir)
    remember(data_dir, model_id, seconds)

    old = subscription.source
    subscription.model = model_id
    subscription.source = encoded.source
    subscription.guids = guids
    subscription.updated = now.isoformat()
    subscription.error, subscription.error_key, subscription.error_params = "", "", {}
    if old and old != encoded.source and is_library(old):
        # Checked and deleted under the lock tag writes share: a copy of the
        # old episodes onto another sticker must not lose them in between.
        with WRITING:
            if not _still_played(client, old):
                _delete_quietly(client, path_of(old), subscription.id)
    log.info("podcast %s now plays %d episodes from %s", subscription.id, len(chosen), feed.title)
    return True


def _encode_episodes(client: TeddyCloudClient, folder: Path, feed: Feed, chosen: List[Episode],
                     now: datetime, fetch: Fetch, seconds: Optional[float]) -> EncodeResult:
    """Download the chosen episodes into ``folder`` and encode them into a new taf.

    A new name every time, so the old taf keeps playing until the new one is
    complete and the tag has been switched over. The folder goes either way.
    """
    if not is_subscription_id(folder.name):
        raise ValueError(f"not a work folder: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        paths = []
        for number, episode in enumerate(chosen, 1):
            path = folder / f"{number:03d}{_episode_suffix(episode)}"
            if fetch is download:
                # Straight to the file; a fetch handed in, as tests do, gives bytes.
                download(episode.url, into=path)
            else:
                path.write_bytes(fetch(episode.url))
            paths.append(path)
        target = library_path(SERIES, f"{_title_in_name(feed.title)} {now:%Y-%m-%d %H%M%S}")
        return encode_files(client, paths, target, total_seconds=seconds)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _put_on_sticker(client: TeddyCloudClient, subscription: Subscription, feed: Feed,
                    chosen: List[Episode], encoded: EncodeResult, fetch: Fetch,
                    data_dir: Path) -> str:
    """Point the sticker and its model at the new taf; returns the model id.

    Should that fail, the new taf is deleted: the sticker still plays the old
    episodes, and the new file would only be one nothing points at.
    """
    try:
        cover = _cover(client, subscription, feed, fetch)
        # Checked again right before writing, under the lock writes share: the
        # download and the encode took minutes, and the sticker may have been
        # given something else since, or be given it between check and write.
        with WRITING:
            _check_sticker(client, subscription)
            return publish(
                client, subscription.ruid, encoded, series=feed.title, episode=EPISODE_LINE,
                tracks=[episode.title for episode in chosen], cover=cover,
                cover_name=picture_suffix(feed.image or ""),
                model_id=subscription.model or None, data_dir=data_dir,
            )
    except Exception:
        _delete_quietly(client, encoded.target, subscription.id)
        raise


def _check_sticker(client: TeddyCloudClient, subscription: Subscription) -> None:
    """Refuse to touch a sticker that now plays something else."""
    tag = find_tag(client, subscription.ruid)
    if tag is None:
        raise FeedError("feed.sticker_unknown")
    expected = subscription.source or subscription.played_before
    if expected is None:
        return
    playing = tag.source or ""
    if same(playing) == same(expected):
        return
    if _made_by_this_subscription(subscription, tag):
        # Published, then the process stopped before the subscription was
        # saved: the sticker plays this podcast's newer taf, no stranger's.
        log.info("podcast %s takes back %s", subscription.id, playing)
        subscription.source = playing
        return
    raise FeedError("feed.sticker_reused", playing=playing or "-")


def _title_in_name(title: str) -> str:
    """The feed title as the start of a taf's name: short enough for any file system."""
    return cut_to_bytes(title[:TITLE_IN_NAME], TITLE_IN_NAME_BYTES).strip()


def _made_by_this_subscription(subscription: Subscription, tag) -> bool:
    """Whether the tag plays a taf this subscription would have made, as its model.

    Its tafs are named after the feed, with the time of the refresh; another
    feed of the same title on the same sticker is the one thing it confuses.
    """
    if not is_library(tag.source or "") or not subscription.title:
        return False
    if subscription.model and (tag.model or "").lower() != subscription.model.lower():
        return False
    stem = library_path(SERIES, _title_in_name(subscription.title))[:-len(".taf")]
    return same(path_of(tag.source)).startswith(same(stem) + " ")


def _audio_there(client: TeddyCloudClient, subscription: Subscription) -> bool:
    """Whether the taf the sticker plays is still in the library.

    Gone after a restore or a delete in the library, the sticker would stay
    silent until the feed has a new episode; its episodes are made again.
    """
    source = subscription.source
    return not is_library(source) or taf_exists(client, path_of(source))


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


def load_subscriptions(data_dir: Path) -> List[Subscription]:
    with LOCK:
        return _read(data_dir)


def add_subscription(data_dir: Path, subscription: Subscription) -> None:
    with LOCK:
        current = [entry for entry in _read(data_dir) if entry.id != subscription.id]
        _write(data_dir, current + [subscription])


def subscribe(data_dir: Path, feed: str, ruid: str, keep: int, title: str = "",
              played_before: str = "") -> Subscription:
    """Store a new subscription of ``feed`` onto the sticker ``ruid`` and return it.

    ``played_before`` is what the sticker plays now, so the first refresh can
    tell it was not given something else meanwhile. ``keep`` is held to
    between one episode and MOST_KEEP.
    """
    subscription = Subscription(id=uuid.uuid4().hex[:10], feed=feed, ruid=ruid,
                                keep=max(1, min(keep, MOST_KEEP)), title=title,
                                played_before=played_before)
    add_subscription(data_dir, subscription)
    return subscription


def remove_subscription(data_dir: Path, subscription_id: str) -> None:
    with LOCK:
        _write(data_dir, [entry for entry in _read(data_dir) if entry.id != subscription_id])


def move_subscriptions(data_dir: Path, from_ruid: str, to_ruid: str) -> None:
    """Have every subscription of one sticker follow its tonie onto another."""
    with LOCK:
        current = _read(data_dir)
        for entry in current:
            if entry.ruid == from_ruid:
                entry.ruid = to_ruid
        _write(data_dir, current)


def unsubscribe(data_dir: Path, ruid: str) -> None:
    """Drop every subscription that plays onto this sticker."""
    with LOCK:
        _write(data_dir, [entry for entry in _read(data_dir) if entry.ruid != ruid])


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
    return [from_record(Subscription, entry) for entry in read_json(data_dir / FILE_NAME, [])
            if isinstance(entry, dict) and is_subscription_id(entry.get("id"))
            and entry.get("feed")]


# What an id may be: it names a work folder that a refresh removes when it is
# done, so ".." or a path would remove something else entirely. New ids are
# ten hex digits; older records and tests use plain words.
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_subscription_id(value) -> bool:
    """Whether ``value`` can be a subscription's id: a plain name, never a path."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


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
