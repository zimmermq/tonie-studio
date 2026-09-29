"""Housekeeping for the files a browser upload leaves behind.

Every upload is spooled into its own folder so the album reader can treat it
like any other album. Nothing removed them, and a FLAC album is several hundred
megabytes, so the volume filled up one careless evening at a time.

Folders are removed by age rather than after a successful encode: an upload is
still worth having when the encode failed, when the tracks were ordered wrongly,
or when the same album is about to be cut a second way.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)


def remove_old_uploads(upload_dir: Path, keep_days: int) -> Tuple[int, int]:
    """Delete spooled uploads older than ``keep_days``.

    Returns how many folders went and how many bytes that freed. A folder that
    cannot be read or removed is logged and left alone; housekeeping must never
    be the reason the app fails to start.
    """
    if keep_days <= 0 or not upload_dir.is_dir():
        return 0, 0

    cutoff = time.time() - keep_days * 86400
    removed = freed = 0

    for folder in sorted(upload_dir.iterdir()):
        try:
            if not folder.is_dir() or folder.stat().st_mtime >= cutoff:
                continue
            size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
            shutil.rmtree(folder)
        except OSError as error:
            log.warning("cannot clear the upload %s: %s", folder, error)
            continue
        removed += 1
        freed += size

    if removed:
        log.info("cleared %d upload(s), %.1f MB", removed, freed / 1048576)
    return removed, freed
