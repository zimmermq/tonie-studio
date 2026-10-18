"""Entry point: build the app from the environment.

Used as ``uvicorn --factory tonie_studio.main:app``. The factory form exists
because the settings are read at call time, so a missing variable fails with
this app's own message rather than at import.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI

from .settings import Settings
from .web import create_app


# Asked of the app all the time: by the cluster's probes, and by every open
# page about the box every three seconds. Logged, they would bury the rest.
QUIET_PATHS = ("/health", "/api/box")


class QuietPolls(logging.Filter):
    """Leaves the access log's lines for answered polls out; failed ones stay."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) < 5:
            return True
        path, status = str(args[2]).split("?", 1)[0], args[4]
        return not (path in QUIET_PATHS and isinstance(status, int) and status < 400)


def app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("TS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(QuietPolls())
    # The image points TMPDIR into the data volume, which is mounted over
    # whatever the image had there; without the folder Python would quietly
    # fall back to /tmp.
    _ensure_tmpdir()
    return create_app(Settings.from_env())


def _ensure_tmpdir() -> None:
    """Create TMPDIR, or do without it.

    A volume this app cannot write - mounted read-only, owned by root - must
    not keep the app from starting: the pages that only read still work.
    Uploads are then buffered in the system's own temporary folder.
    """
    folder = os.environ.get("TMPDIR")
    if not folder:
        return
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logging.getLogger(__name__).warning(
            "cannot create TMPDIR %s, using the system's temporary folder: %s", folder, error)
        del os.environ["TMPDIR"]
        tempfile.tempdir = None
