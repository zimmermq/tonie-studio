"""Where the app finds its audio, its teddyCloud and its scratch space.

Everything here is environment configuration rather than a user preference:
none of it can be changed without also changing a volume mount, so there is no
settings screen and nothing is persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable setup."""


@dataclass
class Settings:
    """The paths and endpoints one deployment is wired to."""

    teddycloud_url: str
    # Read-only mount of the audio archive the picker browses.
    archive_dir: Path
    # Writable volume; uploaded files are spooled into "uploads" below it.
    data_dir: Path
    overlay: Optional[str] = None

    # How long a spooled upload is kept. Long enough to encode it again after
    # a mistake, short enough that a few FLAC albums do not sit there forever.
    upload_keep_days: int = 7

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("TS_TEDDYCLOUD_URL")
        if not url:
            raise ConfigError("TS_TEDDYCLOUD_URL is not set")

        return cls(
            teddycloud_url=url,
            archive_dir=Path(os.environ.get("TS_ARCHIVE_DIR", "/archive")),
            data_dir=Path(os.environ.get("TS_DATA_DIR", "/data")),
            overlay=os.environ.get("TS_TEDDYCLOUD_OVERLAY") or None,
        )
