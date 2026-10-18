"""The NFC stickers teddyCloud knows, as this app reads and names them.

A sticker only appears here once a Toniebox has held it against its reader. The
tag index also has placeholder entries no sticker stands for; they never leave
this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .i18n import Problem
from .teddycloud import TeddyCloudClient


# teddyCloud's placeholder for a tag whose slot in the index is not a real tag.
SYSTEM_RUID = "0000000000000000"


class TonieError(Problem, RuntimeError):
    """Raised when a tag or a model cannot be written as asked; a person reads it."""


@dataclass
class TonieTag:
    """One NFC tag as the assignment page needs to show it."""

    ruid: str
    uid: str
    source: str
    model: str
    series: str
    episode: str
    picture: str
    claimed: bool
    nocloud: bool = False
    live: bool = False
    # Whether the audio it would play is there: in the library, or cached
    # beside its content JSON, where teddyCloud keeps a real tonie's audio
    # without giving it a source.
    exists: bool = False
    # How many chapters its audio has; 0 when teddyCloud does not know.
    chapters: int = 0

    @property
    def assigned(self) -> bool:
        """Whether the tag plays something or is named for something.

        A source alone is not enough to tell: a real tonie teddyCloud cached
        in its content tree has none, yet plays its own audio. A tag with a
        model is somebody's tonie too. Only a tag with none of these is free
        to be given something else without asking.
        """
        return bool(self.source or self.exists or self.model)

    @property
    def label(self) -> str:
        """What to call this tag in a list."""
        return label(self.series, self.episode) or self.source or self.uid or self.ruid


def label(series: Optional[str], episode: Optional[str]) -> str:
    """How a tonie is named in a list: series and episode, or whichever is there."""
    return " - ".join(part for part in (series or "", episode or "") if part)


def is_real_tag(entry: Dict[str, Any]) -> bool:
    """False for teddyCloud's placeholder entries in the tag index, which no sticker has."""
    return entry.get("ruid") != SYSTEM_RUID and entry.get("type") != "system"


def list_tags(client: TeddyCloudClient) -> List[TonieTag]:
    """Every tag the box has read, the empty ones first.

    A tag only appears here once a Toniebox has held it against the reader, so
    a fresh sticker is invisible until then. The empty ones lead because that
    is what a new traveltonie is looking for.
    """
    tags = [_tag(entry) for entry in client.tags() if is_real_tag(entry)]
    return sorted(tags, key=lambda tag: (tag.assigned, tag.label.lower()))


def find_tag(client: TeddyCloudClient, ruid: str) -> Optional[TonieTag]:
    """One tag by its ruid, or None when teddyCloud does not know it."""
    return next((tag for tag in list_tags(client) if tag.ruid == ruid), None)


def require_tag(client: TeddyCloudClient, ruid: str) -> TonieTag:
    """The tag ``ruid``, or a TonieError saying how a sticker becomes known."""
    tag = find_tag(client, ruid)
    if tag is None:
        raise TonieError("tonie.unknown_tag", ruid=ruid)
    return tag


def _tag(entry: Dict[str, Any]) -> TonieTag:
    info = entry.get("tonieInfo") or {}
    return TonieTag(
        ruid=entry.get("ruid", ""),
        uid=entry.get("uid", ""),
        source=entry.get("source") or "",
        model=info.get("model") or "",
        series=info.get("series") or "",
        episode=info.get("episode") or "",
        picture=info.get("picture") or "",
        claimed=bool(entry.get("claimed")),
        nocloud=bool(entry.get("nocloud")),
        live=bool(entry.get("live")),
        exists=bool(entry.get("exists")),
        chapters=len(entry.get("trackSeconds") or []),
    )
