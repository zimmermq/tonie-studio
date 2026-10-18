"""Moments as the app and teddyCloud keep them: seconds since 1970.

Read back as datetimes in UTC, so every moment compares with every other and
a page shows each in the container's time zone the same way.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union


def from_seconds(value: Union[int, float, str, None]) -> Optional[datetime]:
    """The moment ``value`` seconds after 1970, or None for none (0, empty, unreadable)."""
    try:
        seconds = float(value) if value not in (None, "") else 0.0
        return datetime.fromtimestamp(seconds, timezone.utc) if seconds else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
