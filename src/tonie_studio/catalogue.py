"""The albums on offer: what was uploaded here, and the archive on the share.

An album's id is its root's letter and the id ``list_albums`` gives it, so
an upload and an archive album of the same name stay apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .album import AlbumSummary, list_albums
from .cache import TtlCache
from .i18n import Problem
from .settings import Settings

# Which root an album id belongs to.
ARCHIVE = "a"
UPLOAD = "u"

# How long a scan of the archive stays good enough to reuse. One page load is
# one listing plus a preview request per album, and on a network share each of
# those walks costs more than rendering the page does. Short enough that an
# album copied onto the share shows up on the next reload; an upload made here
# clears it outright.
TTL = 10.0


class AlbumGone(Problem, LookupError):
    """No album on offer has this id: deleted, moved, or never there."""


class Catalogue:
    """Every album on offer, from the uploads and the archive.

    One per app. A scan is reused for TTL seconds; ``forget`` drops it at
    once, for an upload that has to show up on the very next page.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # Through a lambda, so the scan is looked up on each load.
        self._scans: TtlCache[bool, List[Tuple[str, AlbumSummary, str]]] = TtlCache(
            lambda describe: self._scan(describe), TTL)

    def albums(self, describe: bool = True) -> List[Tuple[str, AlbumSummary, str]]:
        """Every album as (id, summary, where it came from), uploads first.

        ``describe=False`` leaves out what only reading the tags tells: title,
        artist, year and cover. Looking an album up by its id needs none of it.
        """
        return self._scans.get(describe)

    def resolve(self, identifier: str) -> Path:
        """The folder an album id names; AlbumGone if none has it.

        Deliberately without the descriptions: the picker draws one preview
        per album, and each of those requests lands here. Reading every
        album's tags again for each of them turned a page load into seconds
        of network file opens.
        """
        for candidate_id, summary, _origin in self.albums(describe=False):
            if candidate_id == identifier:
                return summary.path
        raise AlbumGone("error.album_gone")

    def forget(self) -> None:
        self._scans.forget()

    def _scan(self, describe: bool) -> List[Tuple[str, AlbumSummary, str]]:
        # What was just added is what someone is looking for: uploads lead.
        entries = []
        for prefix, root, origin in (
            (UPLOAD, self.settings.upload_dir, "upload"),
            (ARCHIVE, self.settings.archive_dir, "archive"),
        ):
            if not root.is_dir():
                continue
            for summary in list_albums(root, describe=describe):
                entries.append((f"{prefix}{summary.id}", summary, origin))
        return entries
