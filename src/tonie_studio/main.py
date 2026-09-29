"""Entry point: build the app from the environment.

Used as ``uvicorn --factory tonie_studio.main:app``. The factory form exists
because the settings are read at call time, so a missing variable fails with
this app's own message rather than at import.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from .settings import Settings
from .web import create_app


def app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("TS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return create_app(Settings.from_env())
