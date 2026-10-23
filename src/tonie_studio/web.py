"""FastAPI app: puts the pages together and owns what outlives a request.

The routes live in ``pages``, one module per area. This module builds what
they share - the teddyCloud client, the job runner, the box watcher, the
cached models and the podcast updater - hands it to every page as a
``Context``, and runs the housekeeping. Nothing here talks to
teddyCloud directly; it goes through the client and the encode module, so the
rules about staging and cleanup hold no matter which page started the work.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional, Sequence

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .backup import clear_parked
from .boxstate import BoxWatcher
from .cache import BackgroundCache
from .catalogue import Catalogue
from .compress import CompressMiddleware
from .encode import list_tafs, recover_asides, remove_orphaned_staging
from .importing import Importer
from .languages import NAMED, spoken
from .naming import Namer
from .shop import Shop
from .jobs import Groups, JobRunner
from .library import same
from .listening import ListeningLog
from .listing import orders
from .models import ModelCache, cover_name, cover_url
from .pages import albums, batch, extras, importing, printing, site, tonies, upkeep
from .pages import box as box_page
from .pages import covers as cover_pages
from .pages import library as library_page
from .pages import radio as radio_page
from .pages import jobs as job_pages
from .pages.common import (STATIC, TEMPLATES, Context, describe_progress,
                           page_context)
from .podcast import WORK_FOLDER as PODCAST_WORK_FOLDER
from .podcast import Fetch, PodcastUpdater, download
from .printing import clear_old_pdfs
from .settings import Settings
from .store import sweep
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .uploads import remove_old_uploads

log = logging.getLogger(__name__)

# How often, in seconds, old uploads, unconfirmed restores and stray PDFs are
# cleared while the app runs. They age in days, so once a day loses nothing.
HOUSEKEEPING_EVERY = 86400.0

# How old the album list's copy of the library may get, in seconds, before a
# page view starts a refresh. Walking the library is one request per folder.
LIBRARY_TTL = 60.0

# How long, in seconds, a stopping app waits for the jobs still running. The
# process ends right after, cutting off whatever is left; Kubernetes kills it
# 30 seconds after asking it to stop, and the server needs some of those to
# close its connections first.
SHUTDOWN_GRACE = 20.0

def create_app(
    settings: Settings,
    client_factory: Optional[Callable[[], TeddyCloudClient]] = None,
    runner: Optional[JobRunner] = None,
    watcher: Optional[BoxWatcher] = None,
    fetch: Fetch = download,
    importer: Optional[Importer] = None,
    shop: Optional[Shop] = None,
    background: bool = True,
    shutdown_grace: float = SHUTDOWN_GRACE,
) -> FastAPI:
    """Build the app. The dependencies are injected so tests can replace them.

    ``background`` false starts none of the loops that work on their own -
    housekeeping, podcasts, the importer, the naming - for a test that changes
    teddyCloud while the app runs and must not race them. ``shutdown_grace``
    is how long a stopping app waits for running jobs; see SHUTDOWN_GRACE.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES), context_processors=[page_context])
    templates.env.filters["cover_name"] = cover_name
    templates.env.filters["cover_url"] = cover_url
    templates.env.filters["orders"] = orders
    templates.env.globals["describe_progress"] = describe_progress
    templates.env.globals["spoken_choices"] = NAMED
    templates.env.filters["spoken"] = spoken
    jobs = runner or JobRunner()
    prints = JobRunner()
    catalogue = Catalogue(settings)
    groups = Groups(jobs)
    listening = ListeningLog(settings.data_dir)
    box = watcher if watcher is not None else BoxWatcher(settings.teddycloud_url, settings.overlay)
    # Whoever built the watcher decides how it connects; where its changes go
    # is this app's business, unless a caller already said otherwise.
    if box.on_change is None:
        box.on_change = listening.record

    def client() -> TeddyCloudClient:
        # A fresh client, and so a fresh requests.Session, per use: requests
        # does not promise a Session is safe across threads, and the request
        # handlers, the job runner and the podcast updater all run on their own.
        # The connection set-up this costs is small next to teddyCloud's answers.
        if client_factory:
            return client_factory()
        return TeddyCloudClient(settings.teddycloud_url, overlay=settings.overlay)

    models = ModelCache(client)
    # Every taf path in the library, for the album list to tell which albums
    # are encoded. Strict: a folder it cannot read leaves the copy unknown
    # rather than making every taf in it look missing.
    library = BackgroundCache(
        lambda: {same(taf.path) for taf in list_tafs(client(), strict=True)},
        name="library", ttl=LIBRARY_TTL)
    podcasts = PodcastUpdater(client, settings.data_dir, jobs, fetch)
    importer = importer or Importer(client, settings.data_dir)
    namer = Namer(client, importer.dumps, data_dir=settings.data_dir)
    shop = shop or Shop(settings.data_dir)
    # Whoever built the importer decides what follows an import, unless they
    # left it open: then each imported tonie is named straight away.
    if importer.after_import is None:
        importer.after_import = namer.name_one
    stopping = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Off the startup path on purpose. An unreachable teddyCloud answers
        # only after DNS timeouts and retries, and blocking here would hold the
        # readiness probe down for a service outage this app can survive.
        box.start()
        if background:
            threading.Thread(target=_housekeeping, args=(settings, client, stopping),
                             name="housekeeping", daemon=True).start()
            threading.Thread(target=podcasts.run, args=(stopping,),
                             name="podcasts", daemon=True).start()
            # Carries on a run an earlier process left unfinished.
            importer.start()
            threading.Thread(target=namer.run, args=(stopping,),
                             name="naming", daemon=True).start()
        # Starts the background load now, so the first page after a restart
        # already shows which albums have a tonie.
        models.get()
        library.get()
        try:
            yield
        finally:
            box.stop()
            importer.shutdown()
            # Jobs live in memory only: what was still waiting is lost, and
            # the log is the one place that can say so.
            dropped = jobs.shutdown() + prints.shutdown()
            if dropped:
                log.warning("shutting down: %d job(s) that had not started are dropped", dropped)
            stopping.set()
            # Off the event loop: waiting on a thread is blocking work.
            await asyncio.to_thread(_finish_jobs, (jobs, prints), shutdown_grace)

    app = FastAPI(title="tonie-studio", lifespan=lifespan)
    app.add_middleware(CompressMiddleware)
    app.mount("/static", VersionedStatic(directory=str(STATIC)), name="static")
    app.state.settings = settings
    app.state.catalogue = catalogue
    app.state.jobs = jobs
    app.state.prints = prints

    ctx = Context(
        settings=settings, templates=templates, catalogue=catalogue, jobs=jobs, box=box,
        listening=listening, client=client, fetch=fetch, models=models, podcasts=podcasts,
        groups=groups, library=library, importer=importer, namer=namer,
        shop=shop, prints=prints,
    )
    for part in (site, albums, job_pages, batch, tonies, library_page, radio_page, cover_pages,
                 box_page, extras, upkeep, printing, importing):
        part.register(app, ctx)
    return app


def _finish_jobs(runners: Sequence[JobRunner], grace: float) -> None:
    """Give the running jobs up to ``grace`` seconds in all, and log each one cut off.

    Whatever a cut-off job was writing is left half done - a staging folder,
    a taf without its sticker - and the log is the one place that says which.
    """
    deadline = time.monotonic() + grace
    for runner in runners:
        for job in runner.finish(max(0.0, deadline - time.monotonic())):
            log.warning("shutting down: job %s (%s, %s) was still running and is cut off",
                        job.id, job.kind, job.label)


def _housekeeping(settings: Settings, client: Callable[[], TeddyCloudClient],
                  stop: threading.Event) -> None:
    """Clear what an earlier run left lying about, then keep the volume tidy until ``stop``."""
    # Whatever was mid-encode when the process died is nobody's job now, and
    # it is holding hundreds of megabytes in teddyCloud's library. Only at
    # startup: once jobs run, a staging folder is usually one of theirs.
    try:
        removed = remove_orphaned_staging(client())
        if removed:
            log.info("cleared %d abandoned staging folder(s)", removed)
    except TeddyCloudError as error:
        # teddyCloud being down is no reason for this app not to start; the
        # pages that need it will say so themselves.
        log.warning("could not reach teddyCloud at startup: %s", error)
    while True:
        # A failed round is logged and the next one comes all the same; a dead
        # loop would leave the volume to fill until the app restarts.
        try:
            remove_old_uploads(settings.upload_dir, settings.upload_keep_days)
            clear_parked(settings.data_dir)
            clear_old_pdfs(settings.data_dir)
            # A refresh takes minutes; a day-old folder is one a crash left.
            sweep(settings.data_dir / PODCAST_WORK_FOLDER, "*", 86400)
            _check_space(settings.data_dir)
            # A replace stopped with the process leaves its stickers on the
            # copy beside the taf; nothing else points them back. Every round,
            # not only the first: teddyCloud may still have been writing the
            # taf when the app came back. The stickers play the copy meanwhile.
            recover_asides(client())
        except Exception:
            log.exception("housekeeping round failed")
        if stop.wait(HOUSEKEEPING_EVERY):
            return


class VersionedStatic(StaticFiles):
    """The static files, kept by the browser for good when asked for by version.

    pages.common.asset puts the file's content hash in ``v``; a release that
    changes the file changes the address, so a kept copy is never stale.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200 and b"v=" in scope.get("query_string", b""):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Below this much free space on the data volume housekeeping warns: uploads
# and podcast episodes need room, and a full volume only shows as records
# quietly not being saved.
LOW_SPACE = 1024 ** 3


def _check_space(data_dir: Path) -> None:
    try:
        free = shutil.disk_usage(data_dir).free
    except OSError as error:
        log.warning("cannot tell how full %s is: %s", data_dir, error)
        return
    if free < LOW_SPACE:
        log.warning("only %d MB free on %s", free // 1024 ** 2, data_dir)

