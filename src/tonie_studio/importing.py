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

Nothing is fetched twice. Before a run starts and again right before each
tonie, teddyCloud is asked whether it already has the audio, so a tonie put on
the box meanwhile, or imported by a previous run, is skipped and costs no wait.

A run from a GitHub repository starts by reading the repository, which for
hundreds of dumps takes minutes and therefore happens on the worker, not in
the request that starts it. The access token, if one was given, is kept only
until then: once the dumps are read, the run holds everything it needs.

Only one run exists at a time. A finished, stopped or abandoned one stays on
show until the next is started.
"""

from __future__ import annotations

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
from .nfc import DumpError, parse_dump
from .store import LOCK, from_record, read_json, write_json
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

FILE_NAME = "import.json"

# How cached_at names a taf that lies beside its content JSON.
BESIDE = "content/"

# Item states. NEW items have not been checked against teddyCloud yet.
NEW, PENDING, CACHED, IMPORTED, FAILED, DUPLICATE, UNREADABLE = (
    "new", "pending", "cached", "imported", "failed", "duplicate", "unreadable")

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

    if client.cache_content(folder, file) == 0:
        raise ImportProblem("import.no_taf")

    for waited in range(SETTLE_SECONDS):
        where = cached_at(client, ruid)
        if where and (not where.startswith(BESIDE) or waited >= BESIDE_GRACE_SECONDS):
            cloud_off(client, ruid, {})
            return where
        sleep(1)
    raise ImportProblem("import.not_cached")


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
        return from_record(Run, {**stored, "items": items, "pace": pace})
    except (TypeError, AttributeError) as error:
        log.warning("ignoring unreadable %s: %s", FILE_NAME, error)
        return None


def _save(data_dir: Path, run: Run) -> None:
    write_json(data_dir / FILE_NAME, asdict(run))


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

    # --- what the pages call ------------------------------------------------

    def dumps(self) -> Dict[str, str]:
        """``{ruid: dump path}`` of every tonie the latest run read."""
        run = self.current()
        return {item.ruid: item.path for item in run.items if item.ruid} if run else {}

    def current(self) -> Optional[Run]:
        with LOCK:
            return _load(self.data_dir)

    def begin(self, dumps: Iterable[Tuple[str, str]], pace: Pace, label: str,
              token: str = "") -> Run:
        """Start a run over ``dumps``; refused while another is active."""
        self._preflight(pace)
        return self._create(pace, new_items(dumps), CHECKING, {"label": label, "token": token})

    def begin_github(self, repo: Repo, pace: Pace, token: str = "") -> Run:
        """Start a run over the dumps in ``repo``, which the worker reads first."""
        self._preflight(pace)
        source = {"label": repo.label, "owner": repo.owner, "name": repo.name,
                  "branch": repo.branch, "subfolder": repo.subfolder, "token": token}
        return self._create(pace, [], FETCHING, source)

    def _preflight(self, pace: Pace) -> None:
        pace.check()
        client = self.client()
        if client.setting("cloud.enabled") != "true" or \
                client.setting("cloud.cacheContent") != "true":
            raise ImportProblem("import.cloud_off")

    def _create(self, pace: Pace, items: List[Item], phase: str, source: Dict[str, Any]) -> Run:
        with LOCK:
            existing = _load(self.data_dir)
            if existing and existing.active:
                raise ImportProblem("import.busy")
            run = Run(id=uuid.uuid4().hex[:12], started_at=self.clock(), pace=pace,
                      items=items, source=source, phase=phase)
            _save(self.data_dir, run)
        log.info("import %s started: %d dump(s) from %s", run.id, len(items), source["label"])
        self._wake.set()
        return run

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
            _save(self.data_dir, run)
        self._wake.set()

    # --- the worker ---------------------------------------------------------

    def start(self) -> None:
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
        if run is None or not run.active or run.paused:
            return IDLE_SECONDS
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
        checked: Dict[int, Tuple[str, str]] = {}
        for index, item in enumerate(run.items):
            if item.state != NEW:
                continue
            entry, where = lookup(client, item.ruid)
            if where:
                cloud_off(client, item.ruid, entry)
            checked[index] = (CACHED, where) if where else (PENDING, "")
            if len(checked) == CHECKS_PER_WRITE:
                break

        def record(current: Run) -> None:
            for index, (state, where) in checked.items():
                if state == CACHED:
                    _settle_item(current.items[index], CACHED, where, 0.0)
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
            return IDLE_SECONDS

        item = run.items[index]
        client = self.client()
        # Settled meanwhile by the box or another run: no download, no wait.
        entry, where = lookup(client, item.ruid)
        if where:
            cloud_off(client, item.ruid, entry)
            settled = self.clock()
            self._record(run.id,
                         lambda current: _settle_item(current.items[index], CACHED, where, settled))
            return 0

        now = self.clock()
        if now < run.next_at:
            return run.next_at - now

        started = now
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
                current.streak += 1
                current.next_at = started + current.pace.pause(self.rng)
                if current.streak >= current.pace.max_failures and current.active:
                    current.phase = GIVEN_UP
            self._record(run.id, failed)
            return 0

        log.info("imported %s as %s", item.path, where)

        def imported(current: Run) -> None:
            _settle_item(current.items[index], IMPORTED, where, self.clock())
            current.streak = 0
            current.next_at = started + current.pace.pause(self.rng)
        self._record(run.id, imported)
        if self.after_import:
            try:
                self.after_import(item.ruid, item.path)
            except Exception:
                log.exception("after the import of %s", item.path)
        return 0

    def _record(self, run_id: str, change: Callable[[Run], None]) -> None:
        """Apply a result to the stored run, unless it was replaced meanwhile.

        A pause or stop made while a tonie was downloading is kept: only the
        result is written into the run as it now stands.
        """
        with LOCK:
            run = _load(self.data_dir)
            if run is None or run.id != run_id:
                return
            change(run)
            _save(self.data_dir, run)


def _settle_item(item: Item, state: str, where: str, when: float) -> None:
    item.state, item.result, item.auth, item.settled_at = state, where, "", when
    item.error, item.error_key, item.error_params = "", "", {}
