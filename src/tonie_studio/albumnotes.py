"""What the album list knows about an album beyond its files.

Two things: when an album first showed up, so the list can mark what is new,
and which tonies were made from it, so nobody encodes the same album twice by
accident. Both are notes on the data volume; losing them costs a marker on the
page and nothing else.

Which tonie came from which album is recorded when a tonie is made. Tonies made
before that record existed are matched by name instead, and only when exactly
one album fits: a wrong "already done" would hide an album someone wants, which
is worse than a missing one.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .store import LOCK, read_json, write_json

SEEN_FILE = "albums-seen.json"
MADE_FILE = "albums-made.json"

# How long an album counts as new after it first appeared. Two weeks covers a
# copy onto the share that nobody looks at until the weekend after next.
NEW_FOR = timedelta(days=14)

# Stands in for "was already there" on the very first look, far enough back
# never to count as new.
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def first_sightings(data_dir: Path, ids: Iterable[str], now: datetime) -> Dict[str, datetime]:
    """When each album was first seen, recording the ones seen for the first time.

    The very first look records everything as long known: to an app that has
    never looked, the whole archive is new, and marking all of it says nothing.
    """
    ids = list(ids)
    path = data_dir / SEEN_FILE
    with LOCK:
        stored = read_json(path, None)
        first_look = not isinstance(stored, dict)
        seen = dict(stored) if not first_look else {}
        added = False
        for identifier in ids:
            if identifier not in seen:
                seen[identifier] = (LONG_AGO if first_look else now).isoformat()
                added = True
        if added:
            write_json(path, seen)

    result = {}
    for identifier in ids:
        try:
            result[identifier] = datetime.fromisoformat(seen[identifier])
        except (TypeError, ValueError):
            result[identifier] = LONG_AGO
    return result


def is_new(first_seen: datetime, now: datetime) -> bool:
    return now - first_seen <= NEW_FOR


def remember_made(data_dir: Path, album_id: str, model_id: str) -> None:
    if not album_id or not model_id:
        return
    path = data_dir / MADE_FILE
    with LOCK:
        records = read_json(path, {})
        models = records.setdefault(album_id, [])
        if model_id not in models:
            models.append(model_id)
            write_json(path, records)


def forget_model(data_dir: Path, model_id: str) -> None:
    """Drop a deleted tonie, so its album stops looking done."""
    path = data_dir / MADE_FILE
    with LOCK:
        records = read_json(path, {})
        changed = False
        for album_id in list(records):
            if model_id in records[album_id]:
                records[album_id] = [model for model in records[album_id] if model != model_id]
                changed = True
            if not records[album_id]:
                del records[album_id]
        if changed:
            write_json(path, records)


def made(data_dir: Path) -> Dict[str, List[str]]:
    """Album id to the tonies recorded as made from it."""
    with LOCK:
        records = read_json(data_dir / MADE_FILE, {})
    return records if isinstance(records, dict) else {}


def guess_made(albums: Sequence[tuple], models: Sequence[dict]) -> Dict[str, str]:
    """Album id to model id, for models whose album can only be one.

    A model matches an album when its episode is part of the album's title and
    its series is either the album's artist or also part of the title. A model
    matching two albums is left out rather than pinned to either.
    """
    guesses: Dict[str, str] = {}
    for model in models:
        episode = _fold(model.get("episodes"))
        series = _fold(model.get("series"))
        if not episode:
            continue
        candidates = []
        for identifier, summary, *_ in albums:
            title = _fold(summary.title)
            if episode in title and (series == _fold(summary.artist) or (series and series in title)):
                candidates.append(identifier)
        if len(candidates) == 1:
            guesses.setdefault(candidates[0], str(model.get("model", "")))
    return guesses


def _fold(text) -> str:
    return unicodedata.normalize("NFC", str(text or "")).casefold().strip()
