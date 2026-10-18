"""What the pages share: the context they are built with, the album catalogue,
the addresses of the static files, and turning requests down in the reader's
language.

Album ids carry a one letter prefix for the root they came from. Without it an
uploaded folder and an archive folder of the same name would hash to the same
id, and the wrong one would be encoded.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional, Set, Tuple, Type

from fastapi import Request, UploadFile
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates

from .. import encode
from ..boxstate import BoxWatcher
from ..cache import BackgroundCache
from ..catalogue import AlbumGone, Catalogue
from ..i18n import COOKIE as LANG_COOKIE
from ..i18n import Problem, Translator, languages, negotiate
from ..importing import Importer
from ..naming import Namer
from ..shop import Shop
from ..jobs import Group, Groups, Job, JobRunner
from ..listening import ListeningLog
from ..models import ModelCache, is_cover_name
from ..podcast import Fetch, PodcastUpdater
from ..settings import Settings
from ..tags import TonieTag
from ..teddycloud import TeddyCloudClient

TEMPLATES = Path(__file__).parent.parent / "templates"
STATIC = Path(__file__).parent.parent / "static"

# The most files and fields one upload may carry. A folder upload sends every
# file in the folder, notes and pictures included: a series of sixty episodes,
# or a folder of a thousand dumps, passes the form parser's default of 1000.
MOST_UPLOAD_PARTS = 20000

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
    catalogue: Catalogue
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
    # What the tonies.com shop says about tonies.
    shop: Shop
    # Sheets being made, apart from ``jobs`` so a print never waits for an encode.
    prints: JobRunner


class Refused(Problem):
    """A request turned down, with the HTTP status to answer it with."""

    def __init__(self, status: int, key: str, **params):
        super().__init__(key, **params)
        self.status = status


@contextmanager
def refusing(*kinds: Type[BaseException], status: int = 400) -> Iterator[None]:
    """Answer what the services raise as one of ``kinds`` with a refusal the page shows.

    For the errors a person can act on - a sticker that is taken, a file that
    cannot be read - which the page then names: "That did not work: ...".
    Anything else is a fault and stays one. ``status`` is 502 where what failed
    is a server this app asked on the person's behalf. TeddyCloudError is not
    one of these: the app-wide handler tells teddyCloud refusing from
    teddyCloud being away, and answers both the same on every page.
    """
    try:
        yield
    except kinds as error:
        raise Refused(status, "error.failed", detail=error) from error


async def upload_form(request: Request) -> FormData:
    """The request's form, read with room for a whole folder of files.

    A dependency, ``Depends(upload_form)``, for routes that declare no form
    parameters of their own: FastAPI would otherwise read the form first,
    with the parser's defaults. Being async, only the reading happens on the
    event loop; the route itself still runs in the thread pool.
    """
    try:
        return await request.form(max_files=MOST_UPLOAD_PARTS, max_fields=MOST_UPLOAD_PARTS)
    except StarletteHTTPException as error:
        raise Refused(400, "error.upload_too_many", most=MOST_UPLOAD_PARTS) from error


def album_or_404(catalogue: Catalogue, identifier: str) -> Path:
    """The folder of the album ``identifier``, or a refusal saying it is gone."""
    try:
        return catalogue.resolve(identifier)
    except AlbumGone as gone:
        raise Refused(404, gone.key) from gone


def tag_or_404(tags: Iterable[TonieTag], ruid: str) -> TonieTag:
    """The tag ``ruid`` among ``tags``, or a refusal saying teddyCloud does not know it."""
    tag = next((candidate for candidate in tags if candidate.ruid == ruid), None)
    if tag is None:
        raise Refused(404, "error.no_tag")
    return tag


def job_or_404(runner: JobRunner, job_id: str, kind: Optional[str] = None) -> Job:
    """The job ``job_id`` (of ``kind``, if given), or a refusal saying there is none."""
    job = runner.get(job_id)
    if job is None or (kind is not None and job.kind != kind):
        raise Refused(404, "error.no_job")
    return job


def group_or_404(groups: Groups, group_id: str) -> Group:
    """The group of jobs ``group_id``, or a refusal saying it is gone."""
    group = groups.get(group_id)
    if group is None:
        raise Refused(404, "error.parts_gone")
    return group


def failure_text(job: Job, translate: Translator) -> Optional[str]:
    """Why ``job`` failed, in the reader's language; None if it did not fail.

    A failure the app described is said as it was described; anything else
    is raw text from a part that does not speak the reader's language, so it
    is framed as "That did not work: ...".
    """
    if job.problem:
        return job.problem.say(translate)
    return translate("error.failed", detail=job.error) if job.error else None


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


def uploaded_cover(cover: Optional[UploadFile]) -> Tuple[Optional[bytes], str]:
    """The picture a form sent along, and the extension to store it under.

    Anything that is not an image by its name is ignored rather than refused:
    the cover is optional, and the rest of the form is still worth saving.
    """
    if cover is None or not is_cover_name(cover.filename):
        return None, "jpg"
    return cover.file.read(), Path(cover.filename).suffix.lstrip(".") or "jpg"
