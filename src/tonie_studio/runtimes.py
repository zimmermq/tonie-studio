"""How long each tonie plays.

The model format teddyCloud shares with the official catalogue has twelve
fields and not one of them is a running time, so a playing time has nowhere to
live over there. It is kept here instead, beside the app's own data, written
once when a tonie is recorded and read when a sticker is printed.

This is a note, not a record. A model whose time is missing simply prints
without one, which is why nothing here raises: a sheet that refuses to print
because a number is absent would be worse than a sheet without the number. A
leftover entry for a model that no longer exists is never looked up and costs
a few bytes, so nothing goes hunting for them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

log = logging.getLogger(__name__)

FILE_NAME = "playing-times.json"

_lock = Lock()


def remember(data_dir: Path, model_id: str, seconds: Optional[float]) -> None:
    """Record how long ``model_id`` plays, if it is known."""
    if not model_id or not seconds or seconds <= 0:
        return
    with _lock:
        times = _read(data_dir)
        times[model_id] = int(round(seconds))
        _write(data_dir, times)


def forget(data_dir: Path, model_id: str) -> None:
    """Drop one model's time, for when the model itself is deleted."""
    with _lock:
        times = _read(data_dir)
        if times.pop(model_id, None) is not None:
            _write(data_dir, times)


def playing_times(data_dir: Path) -> Dict[str, int]:
    """Every known playing time in seconds, by model id."""
    with _lock:
        return _read(data_dir)


def minutes(seconds: Optional[int]) -> Optional[int]:
    """Whole minutes, or None. Zero is treated as unknown.

    Rounded half up rather than with Python's round, which sends 66.5 to the
    even 66: a playing time that drops half a minute reads as a mistake.
    """
    if not seconds or seconds <= 0:
        return None
    return max(1, int(seconds / 60 + 0.5))


def _read(data_dir: Path) -> Dict[str, int]:
    path = data_dir / FILE_NAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        log.warning("cannot read %s, carrying on without playing times: %s", path, error)
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): int(value) for key, value in loaded.items()
            if isinstance(value, (int, float))}


def _write(data_dir: Path, times: Dict[str, int]) -> None:
    path = data_dir / FILE_NAME
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place, so a crash halfway
        # through leaves the previous file rather than half of a new one.
        scratch = path.with_suffix(".tmp")
        scratch.write_text(json.dumps(times, indent=1, sort_keys=True), encoding="utf-8")
        scratch.replace(path)
    except OSError as error:
        log.warning("cannot store playing times in %s: %s", path, error)
