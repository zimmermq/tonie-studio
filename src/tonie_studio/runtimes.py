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

from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from .store import LOCK, read_json, write_json

FILE_NAME = "playing-times.json"


def remember(data_dir: Path, model_id: str, seconds: Optional[float]) -> None:
    """Record how long ``model_id`` plays, if it is known."""
    if not model_id or not seconds or seconds <= 0:
        return
    with LOCK:
        times = _read(data_dir)
        times[model_id] = int(round(seconds))
        write_json(data_dir / FILE_NAME, times)


def forget(data_dir: Path, model_id: str) -> None:
    """Drop one model's time, for when the model itself is deleted."""
    with LOCK:
        times = _read(data_dir)
        if times.pop(model_id, None) is not None:
            write_json(data_dir / FILE_NAME, times)


def add_missing(data_dir: Path, saved: Mapping, write: bool = True) -> Tuple[bool, bool]:
    """Add the times in ``saved`` that this volume does not know yet.

    A time recorded here wins over the saved one: it is the newer of the two.
    Returns whether anything was or would be added, and whether there were
    times here before. With ``write`` false nothing is written, for a preview.
    """
    with LOCK:
        current = _read(data_dir)
        merged = {**_valid(saved), **current}
        changed = merged != current
        if changed and write:
            write_json(data_dir / FILE_NAME, merged)
        return changed, bool(current)


def playing_times(data_dir: Path) -> Dict[str, int]:
    """Every known playing time in seconds, by model id."""
    with LOCK:
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
    """Under LOCK, which the caller holds."""
    return _valid(read_json(data_dir / FILE_NAME, {}))


def _valid(loaded) -> Dict[str, int]:
    if not isinstance(loaded, Mapping):
        return {}
    return {str(key): int(value) for key, value in loaded.items()
            if isinstance(value, (int, float))}
