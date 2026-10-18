"""Small JSON records on the data volume.

Every record the app keeps beside teddyCloud is a note rather than a source of
truth: losing one costs a detail on a page, never a working tonie. So reading
never raises - a missing or damaged file reads as the default - and a failed
write is logged rather than allowed to break the request that made it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Mapping, Type, TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)

# One lock for all records: they are tiny, written rarely, and a single lock
# rules out two requests interleaving a read-modify-write on the same file.
LOCK = Lock()


def read_json(path: Path, default: Any) -> Any:
    """The file's content, or ``default`` when it is missing or unreadable.

    Unless ``default`` is None, content of another type than it (a list where
    a dict belongs) also reads as ``default``, so callers can rely on the type.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        log.warning("cannot read %s, carrying on without it: %s", path, error)
        return default
    if default is not None and not isinstance(value, type(default)):
        log.warning("%s holds a %s, not a %s; carrying on without it",
                    path, type(value).__name__, type(default).__name__)
        return default
    return value


@contextmanager
def changing_json(path: Path, default: Any) -> Iterator[Any]:
    """Hand out the file's content for changing in place, under LOCK.

    It is written back when the block ends and the content differs from what
    was read; a block that raises writes nothing. ``default`` must be a fresh
    object, as it is what a missing file hands out.
    """
    with LOCK:
        value = read_json(path, default)
        before = json.dumps(value, sort_keys=True)
        yield value
        if json.dumps(value, sort_keys=True) != before:
            write_json(path, value)


def from_record(cls: Type[T], record: Mapping) -> T:
    """``cls`` made from a stored record, leaving out keys it does not know.

    Records outlive the code: a key a newer version wrote, or an older one
    left, must not stop the rest from loading. A missing required field
    raises TypeError, as calling ``cls`` would.
    """
    known = {field.name for field in fields(cls)}
    return cls(**{key: value for key, value in record.items() if key in known})


def write_json(path: Path, value: Any) -> None:
    """Replace the file in one step, so a crash leaves the old one whole.

    Readable by this app's user only: a running import keeps each dump's cloud
    token in its record, and a GitHub token while it reads the repository.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(path.suffix + ".tmp")
        text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False)
        handle = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        # A scratch file a crash left keeps the mode it was made with.
        os.chmod(scratch, 0o600)
        scratch.replace(path)
    except OSError as error:
        log.warning("cannot store %s: %s", path, error)


def sweep(folder: Path, pattern: str, older_than: float) -> None:
    """Remove what in ``folder`` matches ``pattern`` and is older than ``older_than`` seconds.

    Files and whole folders alike. For what a process that died half way left
    behind; one being worked on right now is younger. What cannot be removed
    is logged and left.
    """
    if not folder.is_dir():
        return
    cutoff = time.time() - older_than
    for leftover in folder.glob(pattern):
        try:
            if leftover.stat().st_mtime >= cutoff:
                continue
            if leftover.is_dir():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        except OSError as error:
            log.warning("cannot remove %s: %s", leftover, error)
