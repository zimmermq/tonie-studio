"""What was played on the box, kept beyond a restart.

The box watcher sees every tonie go on and come off, and every start and stop
of playback, but only holds the latest of each. Home Assistant sees the same
events over MQTT and forgets them after its recorder's ten days, and it knows a
tag only by its UID, never by the name a person gave the tonie. This keeps the
events themselves, so a question like "what has not been on the box since the
summer" has an answer.

The log is one JSON object per line, appended and never rewritten: a crash can
cost at most the line being written, and reading skips a damaged one. A busy
week is a few hundred events, so a year of listening is around a megabyte and
nothing here prunes it.

Durations are reconstructed from the events and therefore only as good as the
events. A box that loses power never reports the tonie coming off, so every
interval is capped at LONGEST_SESSION rather than running until whatever event
happens to come next, possibly days later.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional

from .tags import TonieTag

log = logging.getLogger(__name__)

FILE_NAME = "listening.jsonl"

# How much a run may exceed its content's length before the rest is
# disbelieved: enough for the box to start up and for chapter gaps.
CONTENT_SLACK = timedelta(minutes=2)

# The longest one sitting is credited. Longer than any album in the archive
# that a child plays through in one go, short enough that a box which went dark
# with a tonie on it does not report a day of listening.
LONGEST_SESSION = timedelta(hours=4)

PLACED = "placed"
REMOVED = "removed"
STARTED = "started"
STOPPED = "stopped"
KINDS = frozenset({PLACED, REMOVED, STARTED, STOPPED})



@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str
    ruid: Optional[str]


@dataclass
class TonieListening:
    """One tonie's listening within the period asked about."""

    ruid: str
    placed: int = 0
    on_box: timedelta = timedelta(0)
    played: timedelta = timedelta(0)
    # Over the whole log, not just the period: "not heard this month" still
    # wants to say when it last was.
    last_placed: Optional[datetime] = None


class ListeningLog:
    """Appends box events to the data volume and reads them back."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / FILE_NAME
        self._lock = Lock()

    def record(self, kind: str, ruid: Optional[str], at: datetime) -> None:
        """Add one event. Never raises: losing a line beats losing the stream."""
        if kind not in KINDS:
            return
        line = json.dumps({"at": at.isoformat(), "kind": kind, "ruid": ruid})
        try:
            self._append(line + "\n")
        except OSError as error:
            log.warning("cannot record %s for %s in %s: %s", kind, ruid, self.path, error)

    def _append(self, lines: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
                # A crash mid-write leaves a line without its newline.
                # Appending straight after it would glue these entries onto
                # the broken one and lose them with it.
                handle.seek(0, 2)
                if handle.tell():
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write(lines.encode("utf-8"))

    def events(self) -> List[Event]:
        """Everything recorded, oldest first; lines that cannot be read are skipped."""
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as error:
            log.warning("cannot read %s: %s", self.path, error)
            return []

        events = []
        for line in lines:
            event = _parse(line)
            if event is not None:
                events.append(event)
        events.sort(key=lambda event: event.at)
        return events


def summarise(
    events: Iterable[Event], now: datetime, since: Optional[datetime] = None,
    content: Optional[Dict[str, timedelta]] = None,
) -> Dict[str, TonieListening]:
    """Per tonie: how often it went on, how long it sat there, how long it played.

    Only what falls inside ``since``..``now`` counts towards the numbers; an
    interval that began before ``since`` contributes its inside part, and a
    placement before it is not counted. A tonie appears once it has been placed
    at all, so one last heard before the period shows up with zeros and its
    last date.

    ``content`` is how long each tonie plays, by ruid. A run of playback longer
    than that is not credited at all: a box that falls asleep keeps its last
    log bundle until it wakes, so the end arrives hours late, and Home
    Assistant's record then shows an 84 minute album playing for forty hours.
    How much of such a run was heard is unknown, so it counts as a placement
    and adds no time. Time lying on the box is only capped at LONGEST_SESSION.
    """
    lengths = content or {}
    result: Dict[str, TonieListening] = {}
    current: Optional[str] = None
    placed_at: Optional[datetime] = None
    playing_since: Optional[datetime] = None

    def credit(ruid: str, field: str, start: datetime, end: datetime) -> None:
        if field == "played":
            longest = min(LONGEST_SESSION, lengths.get(ruid, LONGEST_SESSION) + CONTENT_SLACK)
            if end - start > longest:
                # Longer than anything on the tonie: the end that closed this
                # run is not when it ended, and how much was heard is unknown.
                # Nothing is credited rather than a guess.
                return
        end = min(end, start + LONGEST_SESSION)
        if since is not None:
            start = max(start, since)
        if end > start:
            entry = result[ruid]
            setattr(entry, field, getattr(entry, field) + (end - start))

    def close(end: datetime) -> None:
        nonlocal current, placed_at, playing_since
        if current is None or placed_at is None:
            return
        if playing_since is not None:
            credit(current, "played", playing_since, end)
        credit(current, "on_box", placed_at, end)
        current, placed_at, playing_since = None, None, None

    for event in sorted(events, key=lambda event: event.at):
        if event.kind == PLACED and event.ruid:
            close(event.at)
            current, placed_at = event.ruid, event.at
            entry = result.setdefault(event.ruid, TonieListening(event.ruid))
            entry.last_placed = event.at
            if since is None or event.at >= since:
                entry.placed += 1
        elif event.kind == REMOVED:
            if current is not None and (event.ruid is None or event.ruid == current):
                close(event.at)
        elif event.kind == STARTED:
            if current is not None and playing_since is None:
                playing_since = event.at
        elif event.kind == STOPPED:
            if current is not None and playing_since is not None:
                credit(current, "played", playing_since, event.at)
                playing_since = None

    close(now)
    return result


def _parse(line: str) -> Optional[Event]:
    try:
        raw = json.loads(line)
        at = datetime.fromisoformat(raw["at"])
        kind = raw["kind"]
    except (ValueError, TypeError, KeyError):
        return None
    if kind not in KINDS or at.tzinfo is None:
        return None
    ruid = raw.get("ruid")
    return Event(at=at, kind=kind, ruid=ruid if isinstance(ruid, str) else None)


# Heard in the period asked for, or quiet in it; the page filters by it.
HEARD, QUIET = "heard", "quiet"


@dataclass
class Heard:
    """A tonie as the listening list shows it: its sticker and what the box told of it."""

    tag: TonieTag
    entry: Optional[TonieListening]
    kind: str

    @property
    def placed(self) -> int:
        return self.entry.placed if self.entry else 0

    @property
    def played(self) -> int:
        """Seconds it played in the period."""
        return int(self.entry.played.total_seconds()) if self.entry else 0

    @property
    def last(self) -> Optional[datetime]:
        return self.entry.last_placed if self.entry else None


def heard(events: List[Event], tags: Iterable[TonieTag], times: Dict[str, int],
          now: datetime, since: Optional[datetime]) -> List[Heard]:
    """Every sticker that plays something, with what was heard of it, by name.

    A sticker that was emptied has nothing to recommend or to miss and is
    left out. ``times`` are the playing times by model, which cap what one
    run of a tonie can be credited (see summarise).
    """
    tags = list(tags)
    content = {tag.ruid: timedelta(seconds=times[tag.model]) for tag in tags if tag.model in times}
    summary = summarise(events, now=now, since=since, content=content)
    rows = []
    for tag in tags:
        if not tag.assigned:
            continue
        entry = summary.get(tag.ruid)
        played = entry is not None and bool(entry.placed or entry.on_box)
        rows.append(Heard(tag, entry, HEARD if played else QUIET))
    rows.sort(key=lambda row: row.tag.label.casefold())
    return rows

