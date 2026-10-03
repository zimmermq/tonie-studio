"""Follows what is lying on the Toniebox, over teddyCloud's event stream.

teddyCloud pushes box events on ``/api/sse``. Placing a tonie arrives as a
named ``TagValid`` event; **lifting one does not**. `tbs_tag_removed` is the
only state change in teddyCloud that never reaches SSE by name, which reads
like an oversight rather than a decision. The removal is still in the stream,
inside the raw RTNL log as function group 15, function 16067 on an ESP32 box
and 8630 on a CC3200 one, so one extra case turns "last placed" into "on the
box right now".

Two things about those raw entries were learned from a real box rather than
from the source, and both broke the first version:

- The tag id in ``field6`` has its two 4-byte halves swapped: teddyCloud reads
  the first four bytes as the low word (``read_big_endian64``). Read straight,
  the id names no tag on the box and every removal was dropped.
- The box sends its log in bundles, sometimes minutes after the fact; three
  rounds of on and off arrived in the same second. When something happened is
  therefore taken from the box's own ``uptime`` in each entry, tied to the wall
  clock by whichever entry arrived with the least delay. Changes are held back
  until their bundle is complete, because only then is that tie known.

Two things this deliberately does not do:

- Treat ``playback: stopped`` as a removal. Playback also stops when the
  content simply ends, and a tonie left on the box would vanish from the page.
- Believe an old event forever. If the box loses power mid-play the removal
  never arrives, so a reading older than STALE_AFTER is reported as stale and
  the page says it does not know.

teddyCloud serves at most eight SSE clients at once and answers the ninth with
503, so this holds exactly one connection and shares it across every request.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# The RTNL log group that carries tag events, and the removal functions in it.
RTNL_TAG_GROUP = 15
RTNL_TAG_REMOVED = frozenset({16067, 8630})

# After this long without any event, the box has not been heard from and the
# last reading stops being evidence of anything.
STALE_AFTER = timedelta(minutes=30)

# How long a tag teddyCloud does not know counts as "just held against the
# box". Without a limit it is remembered until the process restarts, and a
# sticker touched hours ago still looks like something happening now.
UNKNOWN_FOR = timedelta(minutes=5)

# The stream sends a keep-alive every 15 seconds; well over that means the
# connection is gone even if the socket has not noticed.
READ_TIMEOUT = 90

# Frames of one bundle arrive within milliseconds of each other; a pause this
# long means the bundle is over and its changes can be stamped and passed on.
BUNDLE_GAP = timedelta(seconds=1)

# An uptime this far below the previous one means the box restarted, and the
# old tie between its clock and the wall no longer holds.
REBOOT_DROP_MS = 10_000

RECONNECT_DELAY = 5.0


@dataclass
class BoxState:
    """What is known about the box, as a page needs to render it."""

    uid: Optional[str] = None
    ruid: Optional[str] = None
    on_box: bool = False
    playing: bool = False
    # The tag teddyCloud does not know, from a TagInvalid event: a fresh
    # sticker held against the box is exactly this.
    unknown_ruid: Optional[str] = None
    unknown_since: Optional[datetime] = None
    since: Optional[datetime] = None
    last_event: Optional[datetime] = None
    connected: bool = False

    @property
    def stale(self) -> bool:
        """True when the last event is too old to still mean anything."""
        if self.last_event is None:
            return False
        return datetime.now(timezone.utc) - self.last_event > STALE_AFTER

    @property
    def fresh_unknown(self) -> Optional[str]:
        """A tag teddyCloud does not know, if it was seen just now."""
        if self.unknown_ruid is None or self.unknown_since is None:
            return None
        if datetime.now(timezone.utc) - self.unknown_since > UNKNOWN_FOR:
            return None
        return self.unknown_ruid

    @property
    def known(self) -> bool:
        """True when there is something worth showing."""
        return self.ruid is not None and not self.stale


def uid_from_field6(field6: str) -> Optional[str]:
    """The tag id in a raw RTNL entry, the way teddyCloud itself reads it.

    The first four bytes are the low word and the next four the high one, so
    ``0EEDC9A9E0040350`` is the tag ``E00403500EEDC9A9``. Anything after the
    eighth byte belongs to something else.
    """
    text = (field6 or "").strip()
    if len(text) < 16 or any(character not in "0123456789abcdefABCDEF" for character in text[:16]):
        return None
    return (text[8:16] + text[:8]).upper()


def ruid_from_uid(uid: str) -> Optional[str]:
    """Convert a tag's UID into the ruid teddyCloud files it under.

    The event stream reports the plain UID (``E00403500EEDC9A9``) while the
    content tree and the tag index name the same tag by its reversed, lower
    case form (``a9c9ed0e500304e0``). Comparing the two without converting is
    silently looking at a different tonie.
    """
    uid = (uid or "").strip()
    if len(uid) != 16 or any(character not in "0123456789abcdefABCDEF" for character in uid):
        return None
    return "".join(uid[index:index + 2] for index in range(14, -2, -2)).lower()


class BoxWatcher:
    """Holds one SSE connection and the last thing it said.

    ``on_change`` is told about every real change - a tonie going on or coming
    off, playback starting or stopping - as ``(kind, ruid, at)``. Repeats of
    the same state are not changes and are not passed on. It is called on the
    stream thread, and whatever it raises is logged and swallowed: the record
    of what was heard is worth less than following the box at all.
    """

    def __init__(
        self,
        base_url: str,
        overlay: Optional[str] = None,
        on_change: Optional[Callable[[str, Optional[str], datetime], None]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.base_url = base_url.rstrip("/")
        self.overlay = overlay
        self.on_change = on_change
        self._clock = clock
        # The box's clock: its latest uptime in ms, the wall time it is
        # believed to have started at, and when the last frame came in.
        self._uptime: Optional[int] = None
        self._boot: Optional[datetime] = None
        self._last_arrival: Optional[datetime] = None
        self._state = BoxState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Accumulates the lines of the frame currently being read.
        self._event = ""
        self._data = ""
        # Changes of the bundle being read, as (kind, ruid, uptime, arrival).
        # They are stamped and passed on once the bundle is complete.
        self._pending: List[Tuple[str, Optional[str], Optional[int], datetime]] = []

    @property
    def state(self) -> BoxState:
        """A snapshot, safe to read while the stream thread writes."""
        with self._lock:
            return replace(self._state)

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="box-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.flush()

    def flush(self) -> None:
        """Pass on what is held back, stamped with the best clock known now.

        Called by the stream between bundles; a page about to show the record
        calls it too, so what it shows includes the last bundle.
        """
        with self._lock:
            changes = self._stamped()
        self._report(changes)

    # --- parsing ----------------------------------------------------------

    def feed(self, line: str) -> None:
        """Take one line of the event stream; a blank line ends a frame."""
        line = line.rstrip("\r")
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data = line[len("data:"):].strip()
        elif not line:
            if self._event:
                self.handle(self._event, _payload(self._data))
            self._event = ""
            self._data = ""

    def handle(self, event: str, data: str) -> None:
        """Apply one event to the state."""
        now = self._clock()
        if event == "keep-alive":
            # The quiet between bundles: whatever was read is complete.
            self.flush()
            return

        with self._lock:
            ready = []
            if self._last_arrival is not None and now - self._last_arrival > BUNDLE_GAP:
                ready = self._stamped()
            self._last_arrival = now
            self._state.last_event = now

            if event == "TagValid":
                self._place(data)
            elif event == "TagInvalid":
                # A blank NFC sticker held against the box arrives exactly
                # here: teddyCloud has never seen it, so it is "invalid".
                unknown = ruid_from_uid(data)
                if unknown:
                    self._state.unknown_ruid = unknown
                    self._state.unknown_since = datetime.now(timezone.utc)
            elif event == "playback":
                playing = data == "started"
                if playing != self._state.playing and self._state.on_box:
                    self._note("started" if playing else "stopped", self._state.ruid)
                self._state.playing = playing
            elif event == "rtnl-raw-log2":
                self._raw(data, now)

        self._report(ready)

    def _note(self, kind: str, ruid: Optional[str]) -> None:
        """Hold a change back until its bundle is complete. Called under the lock."""
        self._pending.append((kind, ruid, self._uptime, self._last_arrival or self._clock()))

    def _stamped(self) -> List[Tuple[str, Optional[str], datetime]]:
        """The held-back changes with their times, emptying the queue. Under the lock."""
        stamped = []
        for kind, ruid, uptime, arrival in self._pending:
            if uptime is not None and self._boot is not None:
                at = self._boot + timedelta(milliseconds=uptime)
            else:
                at = arrival
            stamped.append((kind, ruid, at))
        self._pending = []
        return stamped

    def _tick(self, uptime: int, arrival: datetime) -> None:
        """Tie the box's uptime to the wall clock, from one raw entry.

        An entry can arrive late but never early, so each one gives a latest
        possible start time, and the earliest of those is the best estimate.
        """
        candidate = arrival - timedelta(milliseconds=uptime)
        restarted = self._uptime is not None and uptime < self._uptime - REBOOT_DROP_MS
        if self._boot is None or restarted or candidate < self._boot:
            self._boot = candidate
        self._uptime = uptime

    def _report(self, changes: List[Tuple[str, Optional[str], datetime]]) -> None:
        if self.on_change is None:
            return
        for kind, ruid, at in changes:
            try:
                self.on_change(kind, ruid, at)
            except Exception as error:
                log.warning("could not record %s for %s: %s", kind, ruid, error)

    def _place(self, uid: str) -> None:
        ruid = ruid_from_uid(uid)
        if ruid is None:
            return
        if ruid != self._state.ruid or not self._state.on_box:
            self._state.since = datetime.now(timezone.utc)
            self._note("placed", ruid)
        self._state.uid = uid.lower()
        self._state.ruid = ruid
        self._state.on_box = True

    def _raw(self, data: str, arrival: datetime) -> None:
        """The box's own clock, and the only place a removal shows up."""
        try:
            entry = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(entry, dict):
            return
        uptime = entry.get("uptime")
        if isinstance(uptime, int) and uptime >= 0:
            self._tick(uptime, arrival)
        if entry.get("function_group") != RTNL_TAG_GROUP:
            return
        if entry.get("function") not in RTNL_TAG_REMOVED:
            return

        removed = ruid_from_uid(uid_from_field6(str(entry.get("field6", ""))) or "")
        if removed is None or removed == self._state.ruid:
            if self._state.on_box:
                self._note("removed", self._state.ruid)
            self._state.on_box = False
            self._state.playing = False

    # --- the stream -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._listen()
            except Exception as error:
                # Any failure here is a connection that went away, which is
                # normal. Losing the thread over it would not be.
                log.info("event stream from %s ended: %s", self.base_url, error)
            with self._lock:
                self._state.connected = False
            self._stop.wait(RECONNECT_DELAY)

    def _listen(self) -> None:
        params = {"overlay": self.overlay} if self.overlay else None
        with requests.get(
            f"{self.base_url}/api/sse",
            stream=True,
            timeout=(10, READ_TIMEOUT),
            params=params,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            response.encoding = "utf-8"
            with self._lock:
                self._state.connected = True
            log.info("following the box on %s/api/sse", self.base_url)

            for line in response.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    return
                self.feed(line if line is not None else "")


def _payload(data: str) -> str:
    """Unwrap ``{"type":..., "data":...}``, or hand back what was there.

    teddyCloud does not escape the content it puts in that envelope, so a
    title containing a quote arrives as broken JSON. That is one unusable
    frame, not a reason to drop the connection.
    """
    try:
        parsed = json.loads(data)
    except (ValueError, TypeError):
        return data
    if isinstance(parsed, dict) and "data" in parsed:
        inner = parsed["data"]
        return inner if isinstance(inner, str) else json.dumps(inner)
    return data
