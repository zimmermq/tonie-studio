"""FastAPI app: puts the pages together and owns what outlives a request.

The routes live in ``pages``, one module per area. This module builds what
they share - the teddyCloud client, the job runner, the box watcher, the
cached models and the podcast updater - hands it to every page as a
``Context``, and runs the housekeeping. Nothing here talks to
teddyCloud directly; it goes through the client and the encode module, so the
rules about staging and cleanup hold no matter which page started the work.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .boxstate import BoxWatcher
from .cache import BackgroundCache
from .encode import list_tafs, remove_orphaned_staging
from .importing import Importer
from .naming import Namer
from .jobs import Groups, JobRunner
from .library import same
from .listening import ListeningLog
from .models import ModelCache, cover_name, cover_url
from .pages import albums, batch, extras, importing, printing, site, tonies, upkeep
from .pages import jobs as job_pages
from .pages.common import (STATIC, TEMPLATES, Catalogue, Context, describe_progress,
                           page_context)
from .pages.printing import clear_old_pdfs
from .pages.upkeep import clear_parked_restores
from .podcast import Fetch, PodcastUpdater, download
from .settings import Settings
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .uploads import remove_old_uploads

log = logging.getLogger(__name__)

# How often, in seconds, old uploads, unconfirmed restores and stray PDFs are
# cleared while the app runs. They age in days, so once a day loses nothing.
HOUSEKEEPING_EVERY = 86400.0

# How old the album list's copy of the library may get, in seconds, before a
# page view starts a refresh. Walking the library is one request per folder.
LIBRARY_TTL = 60.0

def create_app(
    settings: Settings,
    client_factory: Optional[Callable[[], TeddyCloudClient]] = None,
    runner: Optional[JobRunner] = None,
    watcher: Optional[BoxWatcher] = None,
    fetch: Fetch = download,
    importer: Optional[Importer] = None,
) -> FastAPI:
    """Build the app. The dependencies are injected so tests can replace them."""
    templates = Jinja2Templates(directory=str(TEMPLATES), context_processors=[page_context])
    templates.env.filters["cover_name"] = cover_name
    templates.env.filters["cover_url"] = cover_url
    templates.env.globals["describe_progress"] = describe_progress
    jobs = runner or JobRunner()
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
    namer = Namer(client, importer.dumps)
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
        threading.Thread(target=_housekeeping, args=(settings, client, stopping),
                         name="housekeeping", daemon=True).start()
        box.start()
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
            stopping.set()

    app = FastAPI(title="tonie-studio", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    app.state.settings = settings
    app.state.catalogue = catalogue
    app.state.jobs = jobs

    ctx = Context(
        settings=settings, templates=templates, catalogue=catalogue, jobs=jobs, box=box,
        listening=listening, client=client, fetch=fetch, models=models, podcasts=podcasts,
        groups=groups, library=library, importer=importer, namer=namer,
    )
    for part in (site, albums, job_pages, batch, tonies, extras, upkeep, printing, importing):
        part.register(app, ctx)
    return app


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
            clear_parked_restores(settings)
            clear_old_pdfs(settings.data_dir)
        except Exception:
            log.exception("housekeeping round failed")
        if stop.wait(HOUSEKEEPING_EVERY):
            return
