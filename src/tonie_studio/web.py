"""FastAPI app: puts the pages together and owns what outlives a request.

The routes live in ``pages``, one module per area. This module builds what
they share - the teddyCloud client, the job runner, the box watcher, the
cached models and the podcast updater - hands it to every page as a
``Context``, and runs the housekeeping at startup. Nothing here talks to
teddyCloud directly; it goes through the client and the encode module, so the
rules about staging and cleanup hold no matter which page started the work.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .albumnotes import guess_made, made
from .boxstate import BoxWatcher
from .encode import remove_orphaned_staging
from .i18n import COOKIE as LANG_COOKIE
from .i18n import Problem, catalogs
from .jobs import JobRunner
from .listening import ListeningLog
from .pages import albums, batch, extras, printing, tonies, upkeep
from .pages.common import TEMPLATES, Catalogue, Context, Refused, language_context, translator
from .pages.upkeep import clear_parked_restores
from .podcast import SERIES as PODCAST_SERIES
from .podcast import Fetch, download, load_subscriptions, refresh, update_subscription
from .settings import Settings
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import Model, list_models
from .uploads import remove_old_uploads

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

# How often the podcast updater wakes up, and how old a check has to be before
# it looks again. Children's podcasts publish weekly at most; twice a day finds
# a new episode before the next bedtime without asking a feed every hour.
PODCAST_WAKE_SECONDS = 3600
PODCAST_REFRESH_AFTER = timedelta(hours=12)

# How long the album list trusts its copy of teddyCloud's models.
MODELS_TTL = 60.0

def create_app(
    settings: Settings,
    client_factory: Optional[Callable[[], TeddyCloudClient]] = None,
    runner: Optional[JobRunner] = None,
    watcher: Optional[BoxWatcher] = None,
    fetch: Fetch = download,
) -> FastAPI:
    """Build the app. The dependencies are injected so tests can replace them."""
    templates = Jinja2Templates(directory=str(TEMPLATES), context_processors=[language_context])
    jobs = runner or JobRunner()
    catalogue = Catalogue(settings)
    listening = ListeningLog(settings.data_dir)
    box = watcher if watcher is not None else BoxWatcher(settings.teddycloud_url, settings.overlay)
    # Whoever built the watcher decides how it connects; where its changes go
    # is this app's business, unless a caller already said otherwise.
    if box.on_change is None:
        box.on_change = listening.record

    def client() -> TeddyCloudClient:
        if client_factory:
            return client_factory()
        return TeddyCloudClient(settings.teddycloud_url, overlay=settings.overlay)

    def housekeeping() -> None:
        """What an earlier run left lying about, in teddyCloud and on the volume."""
        remove_old_uploads(settings.upload_dir, settings.upload_keep_days)
        clear_parked_restores(settings)
        clear_abandoned_staging()

    def clear_abandoned_staging() -> None:
        """Drop what an interrupted run left in teddyCloud's library.

        Whatever was mid-encode when the process died is nobody's job now, and
        it is holding hundreds of megabytes.
        """
        try:
            removed = remove_orphaned_staging(client())
            if removed:
                log.info("cleared %d abandoned staging folder(s)", removed)
        except TeddyCloudError as error:
            # teddyCloud being down is no reason for this app not to start; the
            # pages that need it will say so themselves.
            log.warning("could not reach teddyCloud at startup: %s", error)

    # Podcast refreshes queued or running, so a slow one is not queued twice.
    podcasts_in_flight: set = set()
    podcasts_lock = Lock()
    stop_podcasts = threading.Event()

    def queue_podcast(subscription_id: str) -> Optional[str]:
        """Start a refresh as a job, unless one for it is already under way."""
        with podcasts_lock:
            if subscription_id in podcasts_in_flight:
                return None
            podcasts_in_flight.add(subscription_id)

        def work(progress):
            try:
                subscription = next((entry for entry in load_subscriptions(settings.data_dir)
                                     if entry.id == subscription_id), None)
                if subscription is None:
                    return {"changed": False}
                try:
                    changed = refresh(client(), subscription, settings.data_dir, fetch=fetch)
                except Exception as error:
                    # Kept on the subscription, so the podcast page can say
                    # why the episodes are old, not only the job list. A
                    # problem this app describes is kept as its key, to be
                    # said in the reader's language; anything else as it came.
                    if isinstance(error, Problem):
                        subscription.error = ""
                        subscription.error_key, subscription.error_params = error.key, error.params
                    else:
                        subscription.error = str(error) or error.__class__.__name__
                        subscription.error_key, subscription.error_params = "", {}
                    subscription.checked = datetime.now(timezone.utc).isoformat()
                    update_subscription(settings.data_dir, subscription)
                    raise
                update_subscription(settings.data_dir, subscription)
                return {"changed": changed}
            finally:
                with podcasts_lock:
                    podcasts_in_flight.discard(subscription_id)

        label = next((entry.title or entry.feed for entry in load_subscriptions(settings.data_dir)
                      if entry.id == subscription_id), subscription_id)
        return jobs.submit("podcast", label, work)

    def podcast_updater() -> None:
        """Wake up now and then and refresh what has not been checked lately."""
        while not stop_podcasts.wait(PODCAST_WAKE_SECONDS):
            now = datetime.now(timezone.utc)
            for subscription in load_subscriptions(settings.data_dir):
                try:
                    checked = datetime.fromisoformat(subscription.checked)
                except ValueError:
                    checked = None
                if checked is None or now - checked >= PODCAST_REFRESH_AFTER:
                    queue_podcast(subscription.id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Off the startup path on purpose. An unreachable teddyCloud answers
        # only after DNS timeouts and retries, and blocking here would hold the
        # readiness probe down for a service outage this app can survive.
        cleanup = threading.Thread(target=housekeeping,
                                   name="housekeeping", daemon=True)
        cleanup.start()
        box.start()
        threading.Thread(target=podcast_updater, name="podcasts", daemon=True).start()
        try:
            yield
        finally:
            box.stop()
            stop_podcasts.set()

    app = FastAPI(title="tonie-studio", lifespan=lifespan)
    app.state.settings = settings
    app.state.catalogue = catalogue
    app.state.jobs = jobs

    @app.exception_handler(TeddyCloudError)
    def teddycloud_unreachable(request: Request, error: TeddyCloudError):
        """Answer a page, not a stack trace, when teddyCloud is down.

        Half the app keeps working without it, so an outage should read as a
        service being away rather than as this one being broken.
        """
        log.warning("teddyCloud unreachable while serving %s: %s", request.url.path, error)
        return templates.TemplateResponse(
            request, "error.html", {"message": str(error)}, status_code=503,
        )

    @app.exception_handler(Refused)
    def refused(request: Request, error: Refused):
        """Say why a request was turned down, in the reader's language."""
        message = error.say(translator(request))
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=error.status)
        return templates.TemplateResponse(
            request, "refused.html", {"message": message}, status_code=error.status,
        )

    @app.get("/lang/{code}")
    def choose_language(code: str, next: str = "/"):
        """Remember the language chosen and go back to where it was chosen."""
        if code not in catalogs():
            raise Refused(404, "error.no_language")
        # Only a path on this site: an address from elsewhere in ``next``
        # would make this an open redirect.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, 303)
        response.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
        return response

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    # The models teddyCloud holds, kept for the album list. That page only
    # decorates with them, and it must never wait: a teddyCloud that does not
    # answer holds a request for its whole timeout, three times with retries.
    model_cache: Dict = {"at": float("-inf"), "models": None, "loading": False}
    model_lock = Lock()

    def cached_models() -> Optional[List[Dict]]:
        """The last models seen, or None before the first answer; never blocks.

        A stale copy starts one refresh in the background and is returned as
        it is; the page after next shows the newer one.
        """
        with model_lock:
            stale = time.monotonic() - model_cache["at"] > MODELS_TTL
            start = stale and not model_cache["loading"]
            if start:
                model_cache["loading"] = True
            models = model_cache["models"]
        if start:
            threading.Thread(target=load_models, name="models", daemon=True).start()
        return models

    def load_models() -> None:
        models = None
        try:
            models = list_models(client())
        except Exception as error:
            log.info("album list goes without the models this time: %s", error)
        finally:
            with model_lock:
                if models is not None:
                    model_cache["models"] = models
                # Also after a failure, so an absent teddyCloud is asked once
                # a minute rather than on every page.
                model_cache["at"] = time.monotonic()
                model_cache["loading"] = False

    def _albums_with_tonies(albums, models: Optional[List[Model]] = None) -> Dict[str, str]:
        """Album id to the tonie made from it, recorded or matched by name.

        What was recorded is trusted as it stands. The models come from a copy
        that can be a minute old, and checking a record against it would hide a
        tonie made in that minute. A model deleted through this app takes its
        record with it; one deleted in teddyCloud's own interface leaves the
        marker behind, which costs a wrong label and nothing else.

        ``models`` is for a caller that has just read them; the album list
        passes none and takes the cached copy.
        """
        done = {identifier: made_models[-1]
                for identifier, made_models in made(settings.data_dir).items()}
        if models is None:
            models = cached_models()
        if models is None:
            return done
        recorded = set(done.values())
        unrecorded = [model for model in models if model.id not in recorded]
        for identifier, model_id in guess_made(albums, unrecorded).items():
            done.setdefault(identifier, model_id)
        return done

    def work_in_progress() -> Dict:
        """What the checkup must not call left over: tafs jobs made or are making.

        Encode jobs are labelled with their target. A podcast refresh builds its
        taf under a name only known once it runs, so while one is under way the
        whole podcast folder is spared.
        """
        busy = {job.label for job in jobs.all() if job.kind == "encode"}
        with podcasts_lock:
            refreshing = bool(podcasts_in_flight)
        return {"busy": busy, "busy_folders": {f"/{PODCAST_SERIES}"} if refreshing else set()}

    ctx = Context(
        settings=settings, templates=templates, catalogue=catalogue, jobs=jobs, box=box, listening=listening,
        client=client, fetch=fetch, cached_models=cached_models,
        albums_with_tonies=_albums_with_tonies, work_in_progress=work_in_progress,
        queue_podcast=queue_podcast,
    )
    for part in (albums, batch, tonies, extras, upkeep, printing):
        part.register(app, ctx)
    return app
