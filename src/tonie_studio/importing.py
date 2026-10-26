"""Importing tonies from Flipper Zero dumps, a few an hour, for as long as it takes.

A dump holds a tonie's UID and its cloud token (see :mod:`nfc`). Given both,
teddyCloud can fetch the tonie's audio from the Tonies cloud itself: it reads
them from the tonie's content JSON, downloads the taf and, with
``cloud.cacheToLibrary``, keeps it in its library. Importing a dump is
therefore writing that JSON and asking teddyCloud for the download.

A collection of dumps is imported slowly on purpose, by default ten an hour
with the pause between them spread by a quarter either way, so the cloud sees
the trickle of a person putting tonies on a box rather than a burst from one
account. Hundreds of dumps take days. The run lives in a file beside the app's
other data, written after every tonie, and a restarted app carries on where it
stopped, keeping to the schedule it had.

An imported tonie ends up in the library only: once teddyCloud has its
audio, the audio is moved into the library if teddyCloud left it beside the
content JSON, and the content JSON is deleted (see into_library). The tonies
dumped need not be stickers on a box of this teddyCloud, and the library is
where audio is put on the stickers that are.

Nothing is fetched twice. Before a run starts and again right before each
tonie, the list of tonies imported before (see imported_list) and teddyCloud
are asked whether the audio is there, so a tonie put on the box meanwhile, or
imported by a previous run, is skipped and costs no wait.

The run file is what keeps the pace: when the next tonie is due, and that a
download was asked for, are written before the download starts. A run whose
file cannot be written therefore stops asking anybody anything (see
Importer.held) until it can, rather than repeat work it cannot note as done.

A run from a GitHub repository starts by reading the repository, which for
hundreds of dumps takes minutes and therefore happens on the worker, not in
the request that starts it. The access token, if one was given, is kept only
until then: once the dumps are read, the run holds everything it needs.

Only one run exists at a time. A finished, stopped or abandoned one stays on
show until the next is started.

A run can switch teddyCloud's access to the Tonies cloud on for itself and
off again when it ends (see Cloud), so that access is open only while it is
needed.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import github
from .github import Repo, RepoError
from .i18n import Problem
from .models import WRITING
from .naming import LIBRARY_FOLDER, LIBRARY_PREFIX
from .nfc import DumpError, parse_dump
from .store import LOCK, changing_json, from_record, read_json, write_json
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

FILE_NAME = "import.json"

# Dumps later runs leave out; see Skip.
SKIP_FILE = "import-skip.json"

# How cached_at names a taf that lies beside its content JSON.
BESIDE = "content/"

# The tonies imported into the library only, by ruid; see imported_list.
IMPORTED_FILE = "imported.json"

# The tree of teddyCloud's that holds the library.
LIBRARY = "library"

# Item states. NEW items have not been checked against teddyCloud yet;
# SKIPPED ones are on the skip list and are not asked for.
NEW, PENDING, CACHED, IMPORTED, FAILED, DUPLICATE, UNREADABLE, SKIPPED = (
    "new", "pending", "cached", "imported", "failed", "duplicate", "unreadable", "skipped")

# What the cloud answers for a dump whose token it does not take. Asking
# again with the same token gets the same answer, so such a dump is put on
# the skip list; other failures, such as a tonie the cloud lacks today, may
# go away and are tried again by the next run.
UNAUTHORIZED = 401

# Run phases. FETCHING, CHECKING and RUNNING are the active ones; the rest are ends.
FETCHING, CHECKING, RUNNING, FINISHED, STOPPED, GIVEN_UP = (
    "fetching", "checking", "running", "finished", "stopped", "given_up")
ACTIVE = (FETCHING, CHECKING, RUNNING)

# How long, in seconds, the worker sleeps when there is nothing to do. Every
# control wakes it at once, so this only bounds a wait nobody interrupts.
IDLE_SECONDS = 3600

# How many items are checked between two writes of the run file. The file
# holds every item, so writing it after each of several hundred checks would
# rewrite megabytes for nothing; a restart re-checks at most this many.
CHECKS_PER_WRITE = 25

# How many times, a second apart, teddyCloud is asked after a finished
# download whether it kept the taf. It moves the taf into the library right
# after the last byte, so a few are plenty; the rest covers a busy disk.
SETTLE_SECONDS = 30

# How many of those times a taf found beside its JSON is given to move into
# the library. teddyCloud renames the finished download next to the JSON
# first and only then moves it, so a look in between finds it there; with
# cloud.cacheToLibrary off it stays, and after this long that is taken as
# its place.
BESIDE_GRACE_SECONDS = 5

# A pause after an unexpected error in the worker, so a fault that repeats
# does not spin.
RETRY_SECONDS = 60

# How long the worker waits before trying again to write a run file it could
# not write, such as on a full data volume. Until it can, it asks neither
# teddyCloud nor the cloud anything; every control wakes it sooner.
HOLD_SECONDS = 1800

# How long after a download was asked for the same tonie may be asked for
# again when the app went away before it ended. teddyCloud finishes such a
# download on its own; asking again before it is done would fetch the tonie
# from the cloud a second time at once.
RESUME_SECONDS = 600

# The failure of a download whose connection to teddyCloud broke. teddyCloud
# carries on with the download by itself, so this says nothing about the
# cloud: it does not count towards giving up and is no reason to skip a dump.
BROKE_OFF = "import.download_broke"

# What a run leaves teddyCloud's cloud settings at when it ends; see Cloud.
RESTORE, SWITCH_OFF = "restore", "off"

# The settings teddyCloud needs on to fetch a tonie's audio: access to the
# cloud at all, and keeping what it fetched.
CLOUD_SETTINGS = ("cloud.enabled", "cloud.cacheContent")


@dataclass
class Skip:
    """A dump later runs leave out, kept until a person takes it off the list."""

    ruid: str
    path: str
    # A fingerprint of the dump's token, never the token itself, which is
    # what the cloud takes for the tonie. "" leaves out every dump of the
    # sticker; with one, a new dump of the same tonie is tried again.
    token: str = ""
    reason: str = ""
    added_at: float = 0.0


def token_print(auth: str) -> str:
    """A fingerprint that tells two tokens apart and gives neither away."""
    return hashlib.sha256(auth.encode("ascii")).hexdigest()[:16] if auth else ""


def skip_list(data_dir: Path) -> List[Skip]:
    """The skip list, oldest first."""
    entries = read_json(data_dir / SKIP_FILE, [])
    skips = []
    for entry in entries:
        try:
            skips.append(from_record(Skip, entry))
        except (TypeError, AttributeError):
            log.warning("ignoring an unreadable entry of %s", SKIP_FILE)
    return skips


def add_skip(data_dir: Path, skip: Skip) -> None:
    """Put ``skip`` on the list, in place of an entry for the same dump. Not under LOCK."""
    with changing_json(data_dir / SKIP_FILE, []) as entries:
        entries[:] = [entry for entry in entries
                      if (entry.get("ruid"), entry.get("token")) != (skip.ruid, skip.token)]
        entries.append(asdict(skip))


def remove_skip(data_dir: Path, ruid: str) -> int:
    """Take every entry of the sticker ``ruid`` off the list; returns how many. Not under LOCK."""
    with changing_json(data_dir / SKIP_FILE, []) as entries:
        before = len(entries)
        entries[:] = [entry for entry in entries if entry.get("ruid") != ruid]
        return before - len(entries)


def _on_list(skips: List[Skip], item: "Item") -> bool:
    mark = token_print(item.auth)
    return any(skip.ruid == item.ruid and (not skip.token or skip.token == mark)
               for skip in skips)


class ImportProblem(Problem):
    """A run that cannot start, or a tonie that could not be imported."""


@dataclass
class Pace:
    """How fast a run goes: tonies per hour, the spread of each pause as a
    fraction either way, and how many failures in a row end it."""

    per_hour: float = 10.0
    jitter: float = 0.25
    max_failures: int = 3

    def check(self) -> None:
        if not (0 < self.per_hour <= 120 and 0 <= self.jitter < 1 and self.max_failures >= 1):
            raise ImportProblem("import.bad_pace")

    def pause(self, rng: random.Random) -> float:
        """Seconds from the start of one import to the start of the next."""
        return 3600 / self.per_hour * rng.uniform(1 - self.jitter, 1 + self.jitter)


@dataclass
class Cloud:
    """Whether a run switches teddyCloud's cloud access on for itself, and
    what it leaves behind when it ends: the settings as it found them
    (RESTORE), or access switched off whatever it was before (SWITCH_OFF).

    The settings are teddyCloud's own, so while a run holds them on every box
    teddyCloud serves reaches the cloud too, not only the import.
    """

    switch: bool = False
    after: str = RESTORE
    # The settings as the run found them, written before it changes any.
    # Not empty exactly while the run owes teddyCloud putting them back,
    # which a restart of the app does not forget.
    before: Dict[str, str] = field(default_factory=dict)

    def check(self) -> None:
        if self.after not in (RESTORE, SWITCH_OFF):
            raise ImportProblem("import.bad_cloud")

    def left_at(self) -> Dict[str, str]:
        """The settings as the run leaves them when it ends."""
        values = dict(self.before)
        if self.after == SWITCH_OFF:
            values["cloud.enabled"] = "false"
        return values


@dataclass
class Item:
    """One dump of a run. ``auth`` is dropped once the item is settled."""

    path: str
    ruid: str = ""
    auth: str = ""
    state: str = NEW
    result: str = ""  # where teddyCloud keeps the audio, once it does
    error: str = ""
    error_key: str = ""
    error_params: Dict[str, Any] = field(default_factory=dict)
    # When the run imported it, failed at it, or found it cached at its turn,
    # as seconds since 1970; 0 for one teddyCloud had before the run began.
    settled_at: float = 0.0
    # When its download was asked for, written before the download starts; 0
    # before. A pending item with one was cut off by the app going away, and
    # audio teddyCloud has for it at its next turn is that download's.
    asked_at: float = 0.0


@dataclass
class Run:
    id: str
    started_at: float
    pace: Pace
    items: List[Item]
    # What the dumps came from, for the page: a "label", and while the run is
    # active anything needed to read them again, such as a "token".
    source: Dict[str, Any]
    phase: str = CHECKING
    # A run stored before runs could switch the cloud finds none here and
    # leaves the settings alone.
    cloud: Cloud = field(default_factory=Cloud)
    paused: bool = False
    next_at: float = 0.0
    streak: int = 0
    # Why the run ended early, when it did for a reason of its own.
    error: str = ""
    error_key: str = ""
    error_params: Dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.phase in ACTIVE

    def count(self, state: str) -> int:
        return sum(1 for item in self.items if item.state == state)

    def settle(self, phase: str) -> None:
        """End the run in ``phase`` and forget the tokens only an active run needs."""
        self.phase = phase
        self.paused = False
        self.source = {"label": self.source.get("label", "")}
        for item in self.items:
            item.auth = ""


# --- one tonie --------------------------------------------------------------

def _content_path(ruid: str) -> Tuple[str, str]:
    return ruid[:8].upper(), ruid[8:].upper()


def cached_at(client: TeddyCloudClient, ruid: str) -> str:
    """Where teddyCloud keeps the tonie's audio, or "" if it has none.

    That is the library path its content JSON points at or, with
    ``cloud.cacheToLibrary`` off, a non-empty taf beside the JSON. teddyCloud
    answers an empty entry for a tonie whose folder does not exist, which
    saves listing the content tree's hundreds of folders.
    """
    return lookup(client, ruid)[1]


def lookup(client: TeddyCloudClient, ruid: str) -> Tuple[Dict[str, Any], str]:
    """The tonie's content entry, and where its audio is (see :func:`cached_at`)."""
    entry = client.content_entry(ruid)
    if not entry:
        return entry, ""
    if entry.get("source"):
        return entry, entry["source"]
    folder, file = _content_path(ruid)
    try:
        listing = client.list_dir(f"/{folder}")
    except TeddyCloudError:
        return entry, ""
    if any(item.get("name") == file and not item.get("isDir") and item.get("size")
           for item in listing):
        return entry, f"{BESIDE}{folder}/{file}"
    return entry, ""


def cloud_off(client: TeddyCloudClient, ruid: str, entry: Dict[str, Any]) -> None:
    """Keep teddyCloud from asking the Tonies cloud about a tonie whose audio it now has.

    The cloud is needed once, to fetch the audio. After that teddyCloud plays
    what it keeps, and every further question to the cloud is contact the box
    does not need. Only switched off once the audio is there, never before:
    a download that failed has to be able to try again. A refusal here is
    logged and left, since the tonie plays either way.
    """
    if entry.get("nocloud"):
        return
    try:
        with WRITING:
            client.set_tag(ruid, nocloud=True)
    except TeddyCloudError as error:
        log.warning("could not switch the cloud off for %s: %s", ruid, error)


def import_one(client: TeddyCloudClient, ruid: str, auth: str,
               sleep: Callable[[float], None] = time.sleep,
               entry: Optional[Dict[str, Any]] = None) -> str:
    """Write the tonie's content JSON, have teddyCloud fetch its audio, and
    return where it keeps it.

    The JSON is written whole, because teddyCloud's own setter does not take
    the cloud fields. What an existing entry already says, such as a model
    picked for the tonie, is kept. ``entry`` is that entry when the caller has
    just read it; every read of a tonie teddyCloud does not know yet is a
    warning in its log.
    """
    folder, file = _content_path(ruid)
    entry = dict(client.content_entry(ruid) if entry is None else entry)
    entry.update({"cloud_ruid": ruid, "cloud_auth": auth, "nocloud": False,
                  "cloud_override": False})
    entry.setdefault("source", "")
    entry.setdefault("_version", 5)

    # Asking for a folder that is not there makes teddyCloud drop the
    # connection, so the root is listed first.
    if not any(item.get("name") == folder and item.get("isDir") for item in client.list_dir("/")):
        client.create_dir(f"/{folder}")
    client.upload(io.BytesIO(json.dumps(entry, indent=4).encode("utf-8")), f"{file}.json",
                  path=f"/{folder}")
    # teddyCloud takes the token for the download from this entry, and when
    # it reads none it sends whatever token its connection last carried -
    # another tonie's. The cloud refuses that, and a stream of such requests
    # looks like misuse. So the entry is read back first, and without the
    # tonie's own token nothing goes to the cloud.
    written = client.content_entry(ruid).get("cloud_auth") or ""
    if written.lower() != auth.lower():
        raise ImportProblem("import.entry_not_written")

    try:
        size = client.cache_content(folder, file)
    except TeddyCloudError as error:
        if error.status is not None:
            raise
        # The connection to teddyCloud broke, not the one to the cloud:
        # teddyCloud reads the download to the end by itself and keeps it.
        where = _kept(client, ruid, sleep, tolerant=True)
        if where:
            log.info("download of %s broke off, but teddyCloud finished it", ruid)
            cloud_off(client, ruid, {})
            return where
        raise ImportProblem(BROKE_OFF) from error
    if size == 0:
        raise ImportProblem("import.no_taf")

    where = _kept(client, ruid, sleep)
    if where:
        cloud_off(client, ruid, {})
        return where
    raise ImportProblem("import.not_cached")


def _kept(client: TeddyCloudClient, ruid: str, sleep: Callable[[float], None],
          tolerant: bool = False) -> str:
    """Where teddyCloud keeps the tonie's audio once it has settled, or "" after SETTLE_SECONDS.

    ``tolerant`` takes a question teddyCloud does not answer as "not yet"
    rather than raising, for when its connection just broke.
    """
    for waited in range(SETTLE_SECONDS):
        try:
            where = cached_at(client, ruid)
        except TeddyCloudError:
            if not tolerant:
                raise
            where = ""
        if where and (not where.startswith(BESIDE) or waited >= BESIDE_GRACE_SECONDS):
            return where
        sleep(1)
    return ""


# --- into the library only ----------------------------------------------------
#
# The content entry has to exist while teddyCloud downloads, since it takes
# the cloud token from it, and goes once the audio is in the library. The
# token is not lost with it: the dump still holds it.

def into_library(client: TeddyCloudClient, ruid: str, where: str) -> str:
    """Where the tonie's audio lies in the library, moving it there if teddyCloud left it beside.

    teddyCloud keeps one taf per audio id in the library and leaves a download
    beside its content JSON when that place holds other audio. The Tonies
    cloud does hand out one audio id for different audio, such as figures of
    one series, so such a taf goes into the same folder under its audio id
    and the start of its hash. teddyCloud names a taf by the audio id and
    hash in its header, not by its file name, so it is named all the same.
    The taf beside is deleted only once the library holds it whole; raises
    ImportProblem, leaving everything as it was, when it cannot be moved.
    """
    if not where.startswith(BESIDE):
        return where
    folder, file = _content_path(ruid)
    header = _header(client.list_dir(f"/{folder}"), file)
    if header is None:
        raise ImportProblem("import.unreadable_taf")
    name = f"{header[0]}-{header[1][:8]}.taf"
    target = f"{LIBRARY_FOLDER}/{name}"
    there = _header(_fetched(client), name)
    if there is not None and there != header:
        raise ImportProblem("import.library_taken", name=name)
    if there is None:
        client.upload(io.BytesIO(client.fetch(f"/content/{folder}/{file}")), name,
                      path=LIBRARY_FOLDER, special=LIBRARY)
        if _header(_fetched(client), name) != header:
            client.delete(target, special=LIBRARY)
            raise ImportProblem("import.not_moved", name=name)
    source = f"{LIBRARY_PREFIX}{name}"
    with WRITING:
        client.set_tag(ruid, source=source)
    client.delete(f"/{folder}/{file}")
    log.info("moved the audio of %s into the library as %s", ruid, source)
    return source


def forget(client: TeddyCloudClient, ruid: str) -> None:
    """Delete the tonie's content entry, and its folder unless another tonie shares it.

    The folder goes too because teddyCloud answers a question about a tonie
    whose folder exists as if it knew the tonie.
    """
    folder, file = _content_path(ruid)
    with WRITING:
        client.delete(f"/{folder}/{file}.json")
        if not [item for item in client.list_dir(f"/{folder}") if item.get("name") != ".."]:
            client.delete(f"/{folder}")


def imported_list(data_dir: Path) -> Dict[str, str]:
    """``{ruid: library source}`` of every tonie imported into the library only.

    With their content entries gone, this is what tells a later run that
    teddyCloud has their audio. Losing it costs the downloads again, nothing more.
    """
    with LOCK:
        return read_json(data_dir / IMPORTED_FILE, {})


def _note_imported(data_dir: Path, ruid: str, source: str) -> None:
    with changing_json(data_dir / IMPORTED_FILE, {}) as imported:
        imported[ruid] = source


def _in_library(client: TeddyCloudClient, source: str,
                listings: Dict[str, List[Dict[str, Any]]]) -> bool:
    """Whether the library still holds ``source``, so a tonie noted as imported is skipped.

    Audio deleted from the library since is fetched again. ``listings`` keeps
    the folders listed so far, as hundreds of tonies share one. A folder that
    cannot be listed raises rather than count as empty, which would fetch
    every tonie in it again.
    """
    folder, _, name = source[len("lib:/"):].rpartition("/")
    if folder not in listings:
        listings[folder] = client.list_dir(folder or "/", special=LIBRARY)
    return any(item.get("name") == name and not item.get("isDir") for item in listings[folder])


def _fetched(client: TeddyCloudClient) -> List[Dict[str, Any]]:
    """The listing of LIBRARY_FOLDER, which is created when missing.

    Asking for a folder that is not there makes teddyCloud drop the
    connection, so each parent is listed first.
    """
    parent = "/"
    for name in LIBRARY_FOLDER.strip("/").split("/"):
        path = f"{parent.rstrip('/')}/{name}"
        if not any(item.get("name") == name and item.get("isDir")
                   for item in client.list_dir(parent, special=LIBRARY)):
            client.create_dir(path, special=LIBRARY)
        parent = path
    return client.list_dir(LIBRARY_FOLDER, special=LIBRARY)


def _header(listing: List[Dict[str, Any]], name: str) -> Optional[Tuple[int, str]]:
    """The audio id and hash (lower case) of the taf ``name`` in ``listing``; None
    when it is not there or teddyCloud read no header from it."""
    for item in listing:
        if item.get("name") == name and not item.get("isDir"):
            header = item.get("tafHeader") or {}
            if header.get("audioId") is None or not header.get("sha1Hash"):
                return None
            return int(header["audioId"]), str(header["sha1Hash"]).lower()
    return None


# --- a run ------------------------------------------------------------------

def new_items(dumps: Iterable[Tuple[str, str]]) -> List[Item]:
    """Read every ``(path, text)`` dump; one already seen counts as a duplicate."""
    items, seen = [], set()
    for path, text in dumps:
        try:
            tonie = parse_dump(text)
        except DumpError as error:
            items.append(Item(path=path, state=UNREADABLE, error=str(error),
                              error_key=error.key, error_params=error.params))
            continue
        if tonie.ruid in seen:
            items.append(Item(path=path, ruid=tonie.ruid, state=DUPLICATE))
            continue
        seen.add(tonie.ruid)
        items.append(Item(path=path, ruid=tonie.ruid, auth=tonie.auth))
    return items


def _load(data_dir: Path) -> Optional[Run]:
    """Under LOCK. A damaged or foreign file means no run rather than a crash."""
    stored = read_json(data_dir / FILE_NAME, None)
    if not isinstance(stored, dict):
        return None
    try:
        items = [from_record(Item, entry) for entry in stored.get("items", [])]
        pace = from_record(Pace, stored.get("pace", {}))
        cloud = from_record(Cloud, stored.get("cloud", {}))
        return from_record(Run, {**stored, "items": items, "pace": pace, "cloud": cloud})
    except (TypeError, AttributeError) as error:
        log.warning("ignoring unreadable %s: %s", FILE_NAME, error)
        return None


def _save(data_dir: Path, run: Run) -> bool:
    """Write the run; returns whether it is stored."""
    return write_json(data_dir / FILE_NAME, asdict(run))


class Importer:
    """Keeps the one run going on a thread of its own.

    It does not use the app's job runner: that has a single worker shared
    with every encode, and a run of days would hold it the whole time.
    """

    def __init__(self, client: Callable[[], TeddyCloudClient], data_dir: Path,
                 clock: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 fetch_repo: Callable[..., List[Tuple[str, str]]] = github.dumps,
                 after_import: Optional[Callable[[str, str], None]] = None):
        self.client = client
        self.fetch_repo = fetch_repo
        # Called with the ruid and dump path of every tonie once it is imported,
        # such as to name it. Its failure is logged and leaves the import alone.
        self.after_import = after_import
        self.data_dir = data_dir
        self.clock = clock
        self.rng = rng or random.Random()
        self.sleep = sleep
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Why the run asks nothing of anybody now, for the page: its file
        # could not be written. Kept in memory, as it is what cannot be stored.
        self.held: Optional[ImportProblem] = None
        # (run id, when its next tonie is due), as the worker last set it.
        # The run file says the same, unless it could not be written or lost
        # what was written; this makes sure the cloud is never asked sooner.
        self._paced: Tuple[str, float] = ("", 0.0)

    # --- what the pages call ------------------------------------------------

    def dumps(self) -> Dict[str, str]:
        """``{ruid: dump path}`` of every tonie the latest run read."""
        run = self.current()
        return {item.ruid: item.path for item in run.items if item.ruid} if run else {}

    def current(self) -> Optional[Run]:
        with LOCK:
            return _load(self.data_dir)

    def begin(self, dumps: Iterable[Tuple[str, str]], pace: Pace, label: str,
              token: str = "", cloud: Optional[Cloud] = None) -> Run:
        """Start a run over ``dumps``; refused while another is active."""
        cloud = cloud or Cloud()
        self._preflight(pace, cloud)
        return self._create(pace, new_items(dumps), CHECKING, {"label": label, "token": token},
                            cloud)

    def begin_github(self, repo: Repo, pace: Pace, token: str = "",
                     cloud: Optional[Cloud] = None) -> Run:
        """Start a run over the dumps in ``repo``, which the worker reads first."""
        cloud = cloud or Cloud()
        self._preflight(pace, cloud)
        source = {"label": repo.label, "owner": repo.owner, "name": repo.name,
                  "branch": repo.branch, "subfolder": repo.subfolder, "token": token}
        return self._create(pace, [], FETCHING, source, cloud)

    def _preflight(self, pace: Pace, cloud: Cloud) -> None:
        pace.check()
        cloud.check()
        client = self.client()
        cloud_on = all(client.setting(name) == "true" for name in CLOUD_SETTINGS)
        if not cloud_on and not cloud.switch:
            raise ImportProblem("import.cloud_off")

    def _create(self, pace: Pace, items: List[Item], phase: str, source: Dict[str, Any],
                cloud: Cloud) -> Run:
        with LOCK:
            existing = _load(self.data_dir)
            if existing and existing.active:
                raise ImportProblem("import.busy")
            # A run that ended without putting the settings back, such as
            # while teddyCloud was away, hands that over: the new run puts
            # back what the old one found, not what it finds itself.
            cloud = Cloud(switch=cloud.switch, after=cloud.after,
                          before=dict(existing.cloud.before) if existing else {})
            run = Run(id=uuid.uuid4().hex[:12], started_at=self.clock(), pace=pace,
                      items=items, source=source, phase=phase, cloud=cloud)
            if not _save(self.data_dir, run):
                raise self._cannot_save()
        self.held = None
        log.info("import %s started: %d dump(s) from %s", run.id, len(items), source["label"])
        self._wake.set()
        return run

    def to_library(self, progress: Optional[Callable[[int, int], None]] = None
                   ) -> Tuple[int, int]:
        """Leave every tonie the latest run imported or found in the library only.

        For tonies settled before imports did so by themselves. Returns how
        many were moved and how many could not be, which keep their entry;
        ``progress`` is called with the tonies done and their number. Refused
        while the run is active, as it moves tonies itself.
        """
        run = self.current()
        if run is None:
            return 0, 0
        if run.active:
            raise ImportProblem("import.busy")
        todo = self._earlier(run)
        client = self.client()
        moved = failed = 0
        for done, item in enumerate(todo, 1):
            _entry, where = lookup(client, item.ruid)
            if where:
                self._keep(client, item, where)
                if item.ruid in imported_list(self.data_dir):
                    moved += 1
                else:
                    failed += 1
            if progress:
                progress(done, len(todo))
        log.info("moved %d imported tonie(s) into the library only, %d could not be", moved,
                 failed)
        return moved, failed

    def earlier(self) -> int:
        """How many tonies of the latest run to_library would move; 0 while it is active."""
        run = self.current()
        return len(self._earlier(run)) if run and not run.active else 0

    def _earlier(self, run: Run) -> List[Item]:
        imported = imported_list(self.data_dir)
        return [item for item in run.items
                if item.ruid and item.state in (CACHED, IMPORTED) and item.ruid not in imported]

    def skip_failed(self, path: str) -> bool:
        """Put the failed dump ``path`` of the latest run on the skip list.

        Its token is known while the run is active; after, the entry leaves
        out every dump of the sticker. Returns whether there was such a dump.
        """
        run = self.current()
        item = next((item for item in run.items if item.path == path and item.state == FAILED),
                    None) if run else None
        if item is None:
            return False
        add_skip(self.data_dir, Skip(ruid=item.ruid, path=item.path, token=token_print(item.auth),
                                     reason=item.error, added_at=self.clock()))
        return True

    def adopt_refusals(self) -> None:
        """Put the latest run's unauthorized failures on the skip list.

        For runs that failed at a dump before the list existed, and so did
        not put it there; an entry already there is written once more.
        """
        run = self.current()
        for item in run.items if run else []:
            if item.state == FAILED and f"answered {UNAUTHORIZED} " in item.error:
                add_skip(self.data_dir, Skip(ruid=item.ruid, path=item.path,
                                             token=token_print(item.auth), reason=item.error,
                                             added_at=item.settled_at))

    def pause(self) -> None:
        self._change(lambda run: setattr(run, "paused", True) if run.active else None)

    def resume(self) -> None:
        def resume(run: Run) -> None:
            if run.phase == GIVEN_UP:
                run.phase, run.streak = RUNNING, 0
            run.paused = False
        self._change(resume)

    def stop(self) -> None:
        self._change(lambda run: run.settle(STOPPED) if run.active else None)

    def _change(self, change: Callable[[Run], None]) -> None:
        with LOCK:
            run = _load(self.data_dir)
            if run is None:
                return
            change(run)
            if not _save(self.data_dir, run):
                self._hold()
        self._wake.set()

    # --- the worker ---------------------------------------------------------

    def start(self) -> None:
        try:
            self.adopt_refusals()
        except Exception:
            # The list is a convenience; the importer runs without it.
            log.exception("could not take earlier refusals onto the skip list")
        self._thread = threading.Thread(target=self._loop, name="importer", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.step()
            except Exception:
                log.exception("import step failed")
                wait = RETRY_SECONDS
            if wait > 0:
                self._wake.wait(wait)
                self._wake.clear()

    def step(self) -> float:
        """Do the next piece of work that is due; return seconds until the next."""
        run = self.current()
        if run is None:
            return IDLE_SECONDS
        if not run.active:
            return self._put_cloud_back(run)
        if run.paused:
            return IDLE_SECONDS
        if self.held is not None:
            # Rewriting the run as it stands tells whether the file can be
            # written again, and asks nobody anything.
            if not self._record(run.id, lambda current: None):
                return HOLD_SECONDS
            log.info("import %s can write its record again and carries on", run.id)
            self.held = None
        if run.cloud.switch and not run.cloud.before and not self._switch_cloud_on(run):
            return HOLD_SECONDS if self.held is not None else 0
        wait = self._work(run)
        return HOLD_SECONDS if self.held is not None else wait

    def _switch_cloud_on(self, run: Run) -> bool:
        """Switch teddyCloud's cloud access on for ``run``; returns whether it is.

        What it was is written first, so that a run cut off halfway still
        knows what to put back. A refusal ends the run, before the cloud is
        asked anything; a run that ended puts back what it changed.
        """
        client = self.client()
        before = {name: client.setting(name) for name in CLOUD_SETTINGS}
        if not self._record(run.id, lambda current: setattr(current.cloud, "before", before)):
            return False
        try:
            _set_cloud(client, {name: "true" for name in CLOUD_SETTINGS})
        except TeddyCloudError as error:
            log.warning("import %s cannot switch teddyCloud's cloud access on: %s", run.id, error)
            cause = ImportProblem("import.cloud_not_switched", reason=str(error))

            def failed(current: Run) -> None:
                current.error, current.error_key, current.error_params = (
                    str(cause), cause.key, cause.params)
                current.settle(STOPPED)
            self._record(run.id, failed)
            return False
        log.info("import %s switched teddyCloud's cloud access on", run.id)
        return True

    def _put_cloud_back(self, run: Run) -> float:
        """Leave teddyCloud's cloud settings as the ended ``run`` was asked to.

        Tried again until teddyCloud takes them: access that stays open is
        what switching it was meant to avoid.
        """
        if not run.cloud.before:
            return IDLE_SECONDS
        try:
            _set_cloud(self.client(), run.cloud.left_at())
        except TeddyCloudError as error:
            log.warning("import %s cannot put teddyCloud's cloud settings back yet: %s",
                        run.id, error)
            return RETRY_SECONDS
        self._record(run.id, lambda current: setattr(current.cloud, "before", {}))
        log.info("import %s put teddyCloud's cloud settings back", run.id)
        return IDLE_SECONDS

    def _work(self, run: Run) -> float:
        if run.phase == FETCHING:
            self._fetch(run)
            return 0
        if run.phase == CHECKING:
            self._check(run)
            return 0
        return self._import_next(run)

    def _fetch(self, run: Run) -> None:
        """Read the run's repository and keep its dumps; the token is not needed after."""
        source = run.source
        repo = Repo(source["owner"], source["name"], source.get("branch", ""),
                    source.get("subfolder", ""))
        try:
            found = self.fetch_repo(repo, token=source.get("token", ""))
        except RepoError as error:
            log.warning("import %s cannot read %s: %s", run.id, repo.label, error)
            cause = error

            def failed(current: Run) -> None:
                current.error, current.error_key, current.error_params = (
                    str(cause), cause.key, cause.params)
                current.settle(STOPPED)
            self._record(run.id, failed)
            return

        items = new_items(found)
        log.info("import %s read %d dump(s) from %s", run.id, len(items), repo.label)

        def fetched(current: Run) -> None:
            if current.phase != FETCHING:
                return
            current.items, current.phase = items, CHECKING
            current.source = {"label": current.source.get("label", "")}
        self._record(run.id, fetched)

    def _check(self, run: Run) -> None:
        """Ask teddyCloud about the next batch of unchecked items."""
        client = self.client()
        skips = skip_list(self.data_dir)
        imported = imported_list(self.data_dir)
        listings: Dict[str, List[Dict[str, Any]]] = {}
        checked: Dict[int, Tuple[str, str]] = {}
        for index, item in enumerate(run.items):
            if item.state != NEW:
                continue
            if item.ruid in imported and _in_library(client, imported[item.ruid], listings):
                where = imported[item.ruid]
            else:
                entry, where = lookup(client, item.ruid)
                if where:
                    cloud_off(client, item.ruid, entry)
                    where = self._keep(client, item, where)
            checked[index] = ((CACHED, where) if where
                              else (SKIPPED, "") if _on_list(skips, item) else (PENDING, ""))
            if len(checked) == CHECKS_PER_WRITE:
                break

        def record(current: Run) -> None:
            for index, (state, where) in checked.items():
                if state in (CACHED, SKIPPED):
                    _settle_item(current.items[index], state, where, 0.0)
                else:
                    current.items[index].state = state
            if not any(item.state == NEW for item in current.items):
                current.phase = RUNNING
        self._record(run.id, record)

    def _import_next(self, run: Run) -> float:
        index = next((i for i, item in enumerate(run.items) if item.state == PENDING), None)
        if index is None:
            self._record(run.id, lambda current: current.settle(FINISHED))
            log.info("import %s finished", run.id)
            # At once, not after an idle wait: the ended run puts the cloud back.
            return 0

        item = run.items[index]
        client = self.client()
        # Settled meanwhile by the box or another run: no download, no wait.
        entry, where = lookup(client, item.ruid)
        if where:
            cloud_off(client, item.ruid, entry)
            if item.asked_at:
                # The download the app went away from: teddyCloud finished it.
                self._imported(client, run.id, index, item, where)
                return 0
            where = self._keep(client, item, where)
            settled = self.clock()
            self._record(run.id,
                         lambda current: _settle_item(current.items[index], CACHED, where, settled))
            return 0

        # Put on the skip list while the run went on: left out, no wait.
        if _on_list(skip_list(self.data_dir), item):
            settled = self.clock()
            self._record(run.id,
                         lambda current: _settle_item(current.items[index], SKIPPED, "", settled))
            return 0

        now = self.clock()
        due = max(run.next_at, self._paced[1] if self._paced[0] == run.id else 0.0,
                  item.asked_at + RESUME_SECONDS if item.asked_at else 0.0)
        if now < due:
            return due - now

        # The pace and the start are written before the download: a restart
        # in the middle of it keeps the pace and leaves teddyCloud time to
        # finish, and a run that cannot write them asks the cloud nothing.
        started = now
        next_at = started + run.pace.pause(self.rng)
        self._paced = (run.id, next_at)

        def asking(current: Run) -> None:
            current.next_at = next_at
            current.items[index].asked_at = started
        if not self._record(run.id, asking):
            return HOLD_SECONDS

        try:
            where = import_one(client, item.ruid, item.auth, sleep=self.sleep, entry=entry)
        except (TeddyCloudError, ImportProblem) as error:
            log.warning("import of %s failed: %s", item.path, error)
            # Python unbinds ``error`` when the except block ends.
            cause = error

            def failed(current: Run) -> None:
                target = current.items[index]
                target.state, target.error = FAILED, str(cause)
                target.settled_at = self.clock()
                if isinstance(cause, Problem):
                    target.error_key, target.error_params = cause.key, cause.params
                if getattr(cause, "key", "") == BROKE_OFF:
                    return
                current.streak += 1
                if current.streak >= current.pace.max_failures and current.active:
                    current.phase = GIVEN_UP
            self._record(run.id, failed)
            if isinstance(cause, TeddyCloudError) and cause.status == UNAUTHORIZED:
                add_skip(self.data_dir, Skip(ruid=item.ruid, path=item.path,
                                             token=token_print(item.auth), reason=str(cause),
                                             added_at=self.clock()))
            return 0

        self._imported(client, run.id, index, item, where)
        return 0

    def _imported(self, client: TeddyCloudClient, run_id: str, index: int, item: Item,
                  where: str) -> None:
        where = self._keep(client, item, where, name=True)
        log.info("imported %s as %s", item.path, where)

        def imported(current: Run) -> None:
            _settle_item(current.items[index], IMPORTED, where, self.clock())
            current.streak = 0
        self._record(run_id, imported)

    def _keep(self, client: TeddyCloudClient, item: Item, where: str, name: bool = False) -> str:
        """Leave the tonie's audio in the library only; returns where it is.

        ``name`` hands it to after_import first, which names it by its
        content entry. Audio that cannot be moved keeps its entry, so it
        still plays and a later run or to_library tries again.
        """
        try:
            where = into_library(client, item.ruid, where)
        except (TeddyCloudError, ImportProblem) as error:
            log.warning("the audio of %s stays where it is, at %s: %s", item.path, where, error)
            if name:
                self._name(item)
            return where
        if name:
            self._name(item)
        try:
            forget(client, item.ruid)
        except TeddyCloudError as error:
            log.warning("could not delete the entry of %s: %s", item.path, error)
            return where
        _note_imported(self.data_dir, item.ruid, where)
        return where

    def _name(self, item: Item) -> None:
        if self.after_import:
            try:
                self.after_import(item.ruid, item.path)
            except Exception:
                log.exception("after the import of %s", item.path)

    def _record(self, run_id: str, change: Callable[[Run], None]) -> bool:
        """Apply a result to the stored run, unless it was replaced meanwhile.

        A pause or stop made while a tonie was downloading is kept: only the
        result is written into the run as it now stands. Returns False when
        the run could not be written, and holds the run (see held).
        """
        with LOCK:
            run = _load(self.data_dir)
            if run is None or run.id != run_id:
                return True
            change(run)
            if _save(self.data_dir, run):
                return True
        self._hold()
        return False

    def _cannot_save(self) -> ImportProblem:
        return ImportProblem("import.cannot_save", folder=str(self.data_dir))

    def _hold(self) -> None:
        """Ask nobody anything until the run file can be written again; see step."""
        if self.held is None:
            log.error("import cannot write %s; asking nobody anything until it can",
                      self.data_dir / FILE_NAME)
        self.held = self._cannot_save()


def _set_cloud(client: TeddyCloudClient, values: Dict[str, str]) -> None:
    """Set teddyCloud's ``values`` and save them, so a restart of teddyCloud keeps them.

    Only what differs is written. teddyCloud answers a setting it did not
    take with an error text at 200, so each one is read back; one it kept
    unchanged raises TeddyCloudError.
    """
    changing = {name: value for name, value in values.items() if client.setting(name) != value}
    if not changing:
        return
    for name, value in changing.items():
        client.set_setting(name, value)
    client.save_settings()
    kept = [name for name, value in values.items() if client.setting(name) != value]
    if kept:
        raise TeddyCloudError(f"teddyCloud kept {', '.join(kept)} unchanged", refused=True)


def _settle_item(item: Item, state: str, where: str, when: float) -> None:
    item.state, item.result, item.auth, item.settled_at = state, where, "", when
    item.error, item.error_key, item.error_params = "", "", {}
