"""What the pages share: the context they are built with, the album catalogue,
the addresses of the static files, and turning requests down in the reader's
language.

Album ids carry a one letter prefix for the root they came from. Without it an
uploaded folder and an archive folder of the same name would hash to the same
id, and the wrong one would be encoded.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional, Set, Tuple

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import encode
from ..album import AUDIO_EXTENSIONS, AlbumSummary, list_albums
from ..boxstate import BoxWatcher
from ..cache import BackgroundCache
from ..i18n import COOKIE as LANG_COOKIE
from ..i18n import Problem, Translator, languages, negotiate
from ..importing import Importer
from ..naming import Namer
from ..jobs import Groups, JobRunner
from ..listening import ListeningLog
from ..models import ModelCache
from ..podcast import Fetch, PodcastUpdater
from ..settings import Settings
from ..teddycloud import TeddyCloudClient

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent.parent / "templates"
STATIC = Path(__file__).parent.parent / "static"

# Which root an album id belongs to.
ARCHIVE = "a"

UPLOAD = "u"

# How long a scan of the archive stays good enough to reuse. One page load is
# one listing plus a preview request per album, and on a network share each of
# those walks costs more than rendering the page does. Short enough that an
# album copied onto the share shows up on the next reload; an upload made here
# clears it outright.
CATALOGUE_TTL = 10.0

# Below this many seconds of audio an encode is done before minutes would mean
# anything, so its progress is not counted in them.
LONG_ENCODE_SECONDS = 90


@dataclass
class Context:
    """What the pages share, built once by ``create_app``.

    The callables are closures over the app's own state; a page calls them
    rather than holding a copy, so a test that swaps a dependency in
    ``create_app`` reaches every page.
    """

    settings: Settings
    templates: Jinja2Templates
    catalogue: "Catalogue"
    jobs: JobRunner
    box: BoxWatcher
    listening: ListeningLog
    client: Callable[[], TeddyCloudClient]
    fetch: Fetch
    models: ModelCache
    podcasts: PodcastUpdater
    groups: Groups
    # Normalised paths of every taf in the library, or None before the first copy.
    library: BackgroundCache[Set[str]]
    importer: Importer
    namer: Namer


class Catalogue:
    """Every album on offer, from the uploads and the archive.

    One per app. A scan is reused for CATALOGUE_TTL: one page load is a
    listing plus a preview request per album, and over a network share each
    walk costs more than rendering the page does. ``forget`` drops it at once,
    for an upload that has to show up on the very next page.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._scans: Dict[bool, Tuple[float, List[Tuple[str, AlbumSummary, str]]]] = {}
        self._lock = Lock()

    def albums(self, describe: bool = True) -> List[Tuple[str, AlbumSummary, str]]:
        """Every album as (id, summary, where it came from), uploads first.

        ``describe=False`` leaves out what only reading the tags tells: title,
        artist, year and cover. Looking an album up by its id needs none of it.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._scans.get(describe)
            if cached and now - cached[0] < CATALOGUE_TTL:
                return cached[1]
        scanned = self._scan(describe)
        with self._lock:
            self._scans[describe] = (now, scanned)
        return scanned

    def resolve(self, identifier: str) -> Path:
        """Find the folder an album id names, or refuse.

        Deliberately without the descriptions: the picker draws one preview
        per album, and each of those requests lands here. Reading every
        album's tags again for each of them turned a page load into seconds
        of network file opens.
        """
        for candidate_id, summary, _origin in self.albums(describe=False):
            if candidate_id == identifier:
                return summary.path
        raise Refused(404, "error.album_gone")

    def forget(self) -> None:
        with self._lock:
            self._scans.clear()

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


class Refused(Problem):
    """A request turned down, with the HTTP status to answer it with."""

    def __init__(self, status: int, key: str, **params):
        super().__init__(key, **params)
        self.status = status


def refused(problem: Problem, status: int = 400) -> Refused:
    """A problem the services raised, as the answer a request gets."""
    return Refused(status, problem.key, **problem.params)


def translator(request: Request) -> Translator:
    """The translator for whoever sent ``request``."""
    return Translator(negotiate(request.cookies.get(LANG_COOKIE),
                                request.headers.get("accept-language")))


# Where the source lives, for the release and commit every page header links to.
SOURCE = "https://github.com/zimmermq/tonie-studio"


def page_context(request: Request) -> Dict:
    """What every page's template needs: the reader's language, the shared files and the build."""
    translate = translator(request)
    return {"t": translate, "lang": translate.lang, "languages": languages(), "asset": asset,
            "build": build()}


def build() -> Dict[str, str]:
    """The release and commit this process runs, as the image was built with them.

    CI passes both when it builds a release; a run from the sources has
    neither, says "dev" and links nowhere, since nothing there was released.
    """
    version = os.environ.get("TS_VERSION") or "dev"
    commit = os.environ.get("TS_COMMIT") or ""
    return {
        "version": version,
        "version_url": f"{SOURCE}/releases/tag/{version}" if version != "dev" else "",
        "commit": commit[:7],
        "commit_url": f"{SOURCE}/commit/{commit}" if commit else "",
    }


@lru_cache(maxsize=None)
def asset(name: str) -> str:
    """The address of a file in ``static``, named after its content.

    The browser may keep such a file as long as it likes: a release that
    changes it changes the address, so an old copy is never used by mistake.
    Read once per process; the files ship with the app.
    """
    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


def is_audio_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def describe_progress(job, translate: Translator) -> str:
    """Where a running job is, in words: tracks for uploads, minutes for an encode.

    The encode counts seconds of audio, and "encode 312/4917" would be true
    and useless. ``static/job.js`` says the same while the page watches.
    """
    if not job.step:
        return translate("job.preparing")
    if job.step == encode.UPLOAD:
        return translate("job.uploaded", done=job.done_count, count=job.total)
    if job.step == encode.ENCODE:
        if job.total > LONG_ENCODE_SECONDS:
            return translate("job.encoded_minutes", done=round(job.done_count / 60),
                             count=round(job.total / 60))
        return translate("job.encoding")
    return f"{job.step} {job.done_count}/{job.total}"
