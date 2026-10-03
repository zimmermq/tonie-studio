"""Small JSON records on the data volume.

Every record the app keeps beside teddyCloud is a note rather than a source of
truth: losing one costs a detail on a page, never a working tonie. So reading
never raises - a missing or damaged file reads as the default - and a failed
write is logged rather than allowed to break the request that made it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

# One lock for all records: they are tiny, written rarely, and a single lock
# rules out two requests interleaving a read-modify-write on the same file.
LOCK = Lock()


def read_json(path: Path, default: Any) -> Any:
    """The file's content, or ``default`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        log.warning("cannot read %s, carrying on without it: %s", path, error)
        return default


def write_json(path: Path, value: Any) -> None:
    """Replace the file in one step, so a crash leaves the old one whole."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(path.suffix + ".tmp")
        scratch.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False),
                           encoding="utf-8")
        scratch.replace(path)
    except OSError as error:
        log.warning("cannot store %s: %s", path, error)
