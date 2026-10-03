"""FastAPI app: pick an album, correct what was guessed, encode it.

Three pages. The picker lists the archive and whatever has been uploaded, the
album page is where every guess the reader made can be overruled, and the job
page watches the encode. Nothing here talks to teddyCloud directly; it goes
through the client and the encode module, so the rules about staging and
cleanup hold no matter which page started the work.

Album ids carry a one letter prefix for the root they came from. Without it an
uploaded folder and an archive folder of the same name would hash to the same
id, and the wrong one would be encoded.
"""

from __future__ import annotations

import logging
import math
import mimetypes
import shutil
import threading
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .album import (AUDIO_EXTENSIONS, Album, AlbumSummary, album_id, cover_candidates,
                     list_albums, read_album)
from .albumnotes import first_sightings, forget_model, guess_made, is_new, made, remember_made
from .backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                     plan_restore, read_backup)
from .batch import AUTO, PLACE, Placing, Slot, allocate, episode_order
from .boxstate import BoxWatcher
from .calibration import build_calibration, expected_first_centre, read_shift, save_measurement
from .checkup import examine, model_repairs, repaired
from .i18n import COOKIE as LANG_COOKIE
from .i18n import DEFAULT as DEFAULT_LANGUAGE
from .i18n import Problem, Translator, catalogs, languages, negotiate
from .encode import (EncodeError, delete_taf, encode_files, library_path, list_tafs,
                     remove_orphaned_staging, taf_exists)
from .jobs import JobRunner, JobState, new_id
from .listening import ListeningLog, summarise
from .overview import Entry, build_overview
from .parts import MOST_CHAPTERS, fewest_parts, part_name, split
from .podcast import SERIES as PODCAST_SERIES
from .podcast import (DEFAULT_KEEP, LARGEST_FEED, Fetch, FeedError, Subscription, download,
                      add_subscription, load_subscriptions, parse_feed, refresh,
                      remove_subscription, update_subscription)
from .runtimes import forget, minutes, playing_times, remember
from .settings import Settings
from .sheet import (BACK_FADE, DIAMETER_MM, FILL, FIT, PER_PAGE, PIXELS, Back, Sticker,
                    back_lines, build_sheet, render_square)
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .thumbnails import DEFAULT_SIZE, crop, thumbnail
from .tonie import (CATEGORIES, DEFAULT_CATEGORY, TonieError, copy_tag, find_model,
                    find_model_by_audio, list_tags, publish, publish_stream, remove_model,
                    update_model, year_of)
from .uploads import remove_old_uploads

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"

# Which root an album id belongs to.
ARCHIVE = "a"
UPLOAD = "u"

# Where teddyCloud serves the covers of custom models.
CUSTOM_IMG_PREFIX = "/custom_img/"

# Offered as a starting point, not as an endorsement: a stream URL is hard to
# find and easy to get wrong, and a wrong one fails silently at the box.
RADIO_SUGGESTIONS = [
    ("KiRaKa (WDR)", "https://wdrhoerenlive-lh.akamaihd.net/i/wdrhoerenlive_kiraka@2334/master.m3u8"),
    ("Radio TEDDY", "https://stream.radioteddy.de/radioteddy/mp3-192/radioteddy"),
    ("Kinderradio Bayern 2 Klaro", "https://dispatcher.rndfnk.com/br/br2/live/mp3/mid"),
]

# How often the podcast updater wakes up, and how old a check has to be before
# it looks again. Children's podcasts publish weekly at most; twice a day finds
# a new episode before the next bedtime without asking a feed every hour.
PODCAST_WAKE_SECONDS = 3600
PODCAST_REFRESH_AFTER = timedelta(hours=12)

# How long the album list trusts its copy of teddyCloud's models.
MODELS_TTL = 60.0

# The periods the listening page offers, by the value in its address.
LISTENING_PERIODS = {"7": 7, "30": 30, "all": None}

# How long a scan of the archive stays good enough to reuse. One page load is
# one listing plus a preview request per album, and on a network share each of
# those walks costs more than rendering the page does. Short enough that an
# album copied onto the share shows up on the next reload; an upload made here
# clears it outright.
CATALOGUE_TTL = 10.0


def create_app(
    settings: Settings,
    client_factory: Optional[Callable[[], TeddyCloudClient]] = None,
    runner: Optional[JobRunner] = None,
    watcher: Optional[BoxWatcher] = None,
    fetch: Fetch = download,
) -> FastAPI:
    """Build the app. The dependencies are injected so tests can replace them."""
    templates = Jinja2Templates(directory=str(TEMPLATES), context_processors=[_language])
    jobs = runner or JobRunner()
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
        _clear_parked_restores(settings)
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        albums = _catalogue(settings)
        now = datetime.now(timezone.utc)
        sightings = first_sightings(settings.data_dir, [row[0] for row in albums], now)
        return templates.TemplateResponse(request, "index.html", {
            "albums": albums,
            "archive": [row for row in albums if row[2] == "archive"],
            "uploads": [row for row in albums if row[2] == "upload"],
            "jobs": jobs.all()[:10],
            "thumb_size": DEFAULT_SIZE,
            "new": {identifier for identifier, when in sightings.items() if is_new(when, now)},
            "done": _albums_with_tonies(albums),
        })

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
            models = client().custom_tonies()
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

    def _albums_with_tonies(albums, models: Optional[List[Dict]] = None) -> Dict[str, str]:
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
        unrecorded = [model for model in models if str(model.get("model", "")) not in recorded]
        for identifier, model_id in guess_made(albums, unrecorded).items():
            done.setdefault(identifier, model_id)
        return done

    @app.post("/upload")
    async def upload(files: List[UploadFile]):
        """Spool a browser upload into its own folder, then treat it as an album.

        A picked folder that holds one folder per episode arrives as one
        upload; each of those folders becomes an album of its own and they
        go on to the batch page together.
        """
        by_folder = _upload_folders(files)
        if not by_folder:
            raise Refused(400, "error.no_audio")

        folder = settings.upload_dir / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        destinations = ({key: folder for key in by_folder} if len(by_folder) == 1
                        else _subfolders(folder, by_folder))
        for key, members in by_folder.items():
            destinations[key].mkdir(parents=True, exist_ok=True)
            for file in members:
                with (destinations[key] / _base_name(file.filename)).open("wb") as target:
                    shutil.copyfileobj(file.file, target)

        # The new folder has to show up at once, not in ten seconds.
        _forget_catalogue()
        ids = [f"{UPLOAD}{album_id(settings.upload_dir, destination)}"
               for destination in sorted(destinations.values(),
                                         key=lambda path: episode_order(path.name))]
        if len(ids) == 1:
            return RedirectResponse(f"/album/{ids[0]}", 303)
        return RedirectResponse(f"/batch?{urlencode([('ids', identifier) for identifier in ids])}",
                                303)

    def _album_page(request: Request, identifier: str, album: Album, conflict: str = "",
                    replace_value: str = ""):
        return templates.TemplateResponse(request, "album.html", {
            "id": identifier,
            "album": album,
            "suggested_target": _suggest_target(album),
            "conflict": conflict,
            "replace_value": replace_value or conflict,
            "most_chapters": MOST_CHAPTERS,
        })

    # Which jobs belong to one album cut into parts. In memory like the jobs
    # themselves: a restart loses both together.
    groups: Dict[str, List[str]] = {}
    # The groups that are several albums rather than one album in parts.
    batches: set = set()
    # Batches whose stickers are handed out as they are placed on the box.
    placings: Dict[str, Placing] = {}

    @app.get("/album/{identifier}", response_class=HTMLResponse)
    def album_page(request: Request, identifier: str):
        return _album_page(request, identifier, read_album(_resolve(settings, identifier)))

    @app.get("/album/{identifier}/cover")
    def cover(identifier: str, index: int = 0, size: int = 0):
        """The album's cover, full size or as a square preview.

        The picker asks for a size; sixteen full covers would be two megabytes
        of JPEG to draw pictures the size of a stamp.
        """
        candidates = cover_candidates(_resolve(settings, identifier))
        if index >= len(candidates):
            raise Refused(404, "error.no_cover")

        path = candidates[index]
        if not size:
            return FileResponse(path)

        preview = thumbnail(path, size)
        if preview is None:
            raise Refused(404, "error.cover_unreadable")
        return Response(preview, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/album/{identifier}/encode")
    def start_encode(
        request: Request,
        identifier: str,
        series: str = Form(""),
        episode: str = Form(...),
        order: str = Form(""),
        selected: List[int] = Form(default=[]),
        titles: List[str] = Form(default=[]),
        replace: str = Form(""),
        parts: int = Form(1),
        cover_index: int = Form(0),
        cover_zoom: float = Form(1.0),
        cover_x: float = Form(0.0),
        cover_y: float = Form(0.0),
    ):
        album = read_album(_resolve(settings, identifier))
        chosen = _chosen_tracks(album, order, selected, titles)
        if not chosen:
            raise Refused(400, "error.no_track")
        if parts < 1:
            raise Refused(400, "error.no_parts")
        if parts > len(chosen):
            raise Refused(400, "error.too_many_parts", tracks=len(chosen), parts=parts)
        if parts < fewest_parts(len(chosen)):
            raise Refused(400, "error.too_few_parts", tracks=len(chosen), chapters=MOST_CHAPTERS,
                          fewest=fewest_parts(len(chosen)))
        try:
            groups_of_tracks = split([track.duration for track, _name in chosen], parts)
            targets = [library_path(series, part_name(episode, number, parts))
                       for number in range(1, parts + 1)]
        except (EncodeError, ValueError) as error:
            raise Refused(400, "error.failed", detail=error) from error

        # Asked here rather than inside the job: an occupied name is a question
        # to answer on the form, not a failure minutes later on a progress page.
        connection = client()
        occupied = [target for target in targets if taf_exists(connection, target)]
        everything = "*"
        overwrite = (replace == everything) or (len(targets) == 1 and replace == targets[0])
        if occupied and not overwrite:
            return _album_page(request, identifier, album, conflict=", ".join(occupied),
                               replace_value=targets[0] if len(targets) == 1 else everything)

        # The cover is decided here, on the page that shows the album, and
        # carried to the model step as the picture it will actually be: a
        # square already cropped the way the round sticker will cut it.
        chosen_cover = (album.cover_candidates[cover_index]
                        if 0 <= cover_index < len(album.cover_candidates) else None)
        cover = str(chosen_cover) if chosen_cover else None
        crop_settings = {"zoom": cover_zoom, "x": cover_x, "y": cover_y}

        job_ids = [
            jobs.submit("encode", target, _encode_job(
                connection, [chosen[index] for index in indices], target,
                replace=target in occupied,
                carried={"series": series, "episode": part_name(episode, number, parts),
                         "cover": cover, "crop": crop_settings, "year": album.year,
                         "album": identifier}))
            for number, (indices, target)
            in enumerate(zip(groups_of_tracks, targets, strict=True), 1)
        ]
        if len(job_ids) == 1:
            return RedirectResponse(f"/job/{job_ids[0]}", 303)
        group = uuid.uuid4().hex[:12]
        groups[group] = job_ids
        return RedirectResponse(f"/parts/{group}", 303)

    @app.get("/parts/{group}", response_class=HTMLResponse)
    def parts_page(request: Request, group: str):
        if group not in groups:
            raise Refused(404, "error.parts_gone")
        batch = group in batches
        return templates.TemplateResponse(request, "parts.html", {
            "jobs": [jobs.get(job_id) for job_id in groups[group] if jobs.get(job_id)],
            "title_key": "batch.progress_title" if batch else "parts.title",
            "heading_key": "batch.progress_title" if batch else "parts.heading",
            "intro_key": "batch.progress_intro" if batch else "parts.intro",
            "placing": group in placings,
            "group": group,
            "next_episode": _next_to_place(placings.get(group)),
        })

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise Refused(404, "error.no_job")
        # A podcast refresh puts its taf on the sticker itself; only an album
        # encode still has to be given to one.
        podcast = job.kind == "podcast"
        published = bool(job.result and job.result.get("model"))
        if podcast:
            done_key, next_href, next_key = "job.podcast_done", "/podcasts", "job.to_podcasts"
        elif published:
            done_key, next_href, next_key = "job.published", "/tonies", "job.to_tonies"
        else:
            done_key, next_href, next_key = "job.taf_done", f"/job/{job.id}/assign", "job.to_assign"
        return templates.TemplateResponse(request, "job.html", {
            "job": job, "done_key": done_key, "next_href": next_href, "next_key": next_key,
        })

    @app.get("/job/{job_id}/assign", response_class=HTMLResponse)
    def assign_page(request: Request, job_id: str, new: str = ""):
        result = _finished_result(jobs, job_id)
        connection = client()
        tags = list_tags(connection)
        encoded = result["encode"]
        # Two models holding the same audio_id and hash describe the same
        # recording, and they would fight each other for teddyCloud's automatic
        # matching. Worth saying before a second one is made.
        duplicate = find_model_by_audio(connection, encoded.audio_id, encoded.sha1)
        # `new` comes back from the page after the box read a tag teddyCloud
        # did not know; it is only honoured once that tag is really in the
        # index, so a stale link cannot preselect something that is not there.
        preselect = new if any(tag.ruid == new for tag in tags) else ""
        return templates.TemplateResponse(request, "assign.html", {
            "job_id": job_id,
            "result": result,
            "tags": tags,
            "categories": CATEGORIES,
            "preselect": preselect,
            "duplicate": duplicate,
        })

    @app.post("/job/{job_id}/assign")
    def assign(
        job_id: str,
        ruid: str = Form(...),
        series: str = Form(""),
        episode: str = Form(...),
        category: str = Form(DEFAULT_CATEGORY),
    ):
        result = _finished_result(jobs, job_id)
        try:
            _publish_result(client(), settings, ruid, result, series, episode, category)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)

    @app.get("/batch", response_class=HTMLResponse)
    def batch_page(request: Request, ids: List[str] = Query(default=[])):
        """Several albums at once, each with the sticker it will get."""
        if not ids:
            raise Refused(400, "error.no_album")
        connection = client()
        rows = _plan_batch(connection, settings, ids,
                           tonies=_albums_with_tonies(_catalogue(settings), connection.custom_tonies()))
        return templates.TemplateResponse(request, "batch.html", {
            "rows": rows,
            "free": len(_free_tags(connection)),
            "needed": sum(len(row.targets) for row in rows if not row.skipped),
            "thumb_size": DEFAULT_SIZE,
        })

    @app.post("/batch")
    def start_batch(
        ids: List[str] = Form(default=[]),
        series: List[str] = Form(default=[]),
        episodes: List[str] = Form(default=[]),
        stickers: str = Form(AUTO),
    ):
        """Encode every album that is not there yet and give each a sticker.

        ``stickers`` says how: AUTO hands out the free ones in order at once,
        PLACE waits for them to be put on the box one by one. The plan is made
        again here rather than taken from the page: a tag assigned in another
        tab meanwhile must not be handed out twice.
        """
        placing_wanted = stickers == PLACE
        if not ids:
            raise Refused(400, "error.no_album")
        connection = client()
        rows = _plan_batch(connection, settings, ids, dict(zip(ids, series, strict=False)),
                           dict(zip(ids, episodes, strict=False)),
                           tonies=_albums_with_tonies(_catalogue(settings), connection.custom_tonies()))
        job_ids = []
        # Made before any encode is queued: one that finishes at once must
        # already find the slot it settles.
        placing = Placing([]) if placing_wanted else None
        for row in rows:
            if row.skipped:
                continue
            tags = [None] * len(row.targets) if placing_wanted or not row.tags else row.tags
            for number, (indices, target, ruid) in enumerate(
                    zip(row.groups, row.targets, tags, strict=True), 1):
                episode = part_name(row.episode, number, len(row.targets))
                job_id = new_id()
                if placing is not None:
                    placing.add(Slot(job_id=job_id, episode=episode))
                    after = _publish_when_placed(connection, settings, placing, job_id,
                                                 row.series, episode)
                else:
                    after = (_publish_after(connection, settings, ruid, row.series, episode)
                             if ruid else None)
                job_ids.append(jobs.submit("encode", target, job_id=job_id, work=_encode_job(
                    connection, [row.chosen[index] for index in indices], target,
                    replace=False,
                    carried={"series": row.series, "episode": episode,
                             "cover": str(row.album.cover_candidates[0])
                             if row.album.cover_candidates else None,
                             "crop": {"zoom": 1.0, "x": 0.0, "y": 0.0},
                             "year": row.album.year, "album": row.identifier},
                    after=after)))
        if not job_ids:
            raise Refused(400, "error.batch_nothing_new")
        group = uuid.uuid4().hex[:12]
        groups[group] = job_ids
        batches.add(group)
        if placing is not None:
            placings[group] = placing
        return RedirectResponse(f"/parts/{group}", 303)

    @app.post("/api/batch/{group}/place")
    def place_sticker(request: Request, group: str, ruid: str = Form(...)):
        """Give the sticker lying on the box to the next episode of a batch.

        Called by the progress page each time the box reports a sticker. Only
        a free one is taken; a sticker that plays something is left as it is,
        and one teddyCloud has not filed yet is answered "unknown" so the page
        asks again.
        """
        placing = placings.get(group)
        if placing is None:
            raise Refused(404, "error.parts_gone")
        translate = translator(request)
        connection = client()

        def failed(job_id: str) -> bool:
            job = jobs.get(job_id)
            return job is None or job.state is JobState.FAILED

        def answer(status: str, slot: Optional[Slot] = None) -> JSONResponse:
            waiting = placing.waiting(failed)
            return JSONResponse({
                "status": status,
                "message": translate(PLACE_MESSAGES[status], episode=slot.episode if slot else ""),
                "then": (translate("place.next", episode=waiting.episode) if waiting
                         else translate("place.all_done")),
                "episode": slot.episode if slot else None,
                "job": slot.job_id if slot else None,
                "next": waiting.episode if waiting else None,
            })

        own = next((slot for slot in placing.slots if slot.ruid == ruid), None)
        tags = {tag.ruid: tag for tag in list_tags(connection)}
        if ruid not in tags:
            return answer("unknown")
        if tags[ruid].assigned:
            if own is not None:
                return answer("assigned", own)
            return answer("taken")

        slot, settled = placing.reserve(ruid, failed)
        if slot is None:
            return answer("complete")
        if not settled:
            return answer("reserved", slot)
        job = jobs.get(slot.job_id)
        if job is None or not job.done.wait(30) or not isinstance(job.result, dict):
            return answer("reserved", slot)
        try:
            job.result["model"] = _publish_result(connection, settings, ruid, job.result,
                                                  job.result["series"], slot.episode)
            job.result["ruid"] = ruid
        except (TonieError, TeddyCloudError) as error:
            log.warning("batch: could not put %s on %s: %s", slot.episode, ruid, error)
            job.result["assign_error"] = "batch.publish_failed"
            return answer("failed", slot)
        return answer("assigned", slot)

    @app.get("/tonies", response_class=HTMLResponse)
    def tonies(request: Request):
        return templates.TemplateResponse(request, "tonies.html", {
            "tags": list_tags(client()),
            "box": box.state,
        })

    @app.get("/tonies/{ruid}", response_class=HTMLResponse)
    def tonie_page(request: Request, ruid: str):
        connection = client()
        tags = list_tags(connection)
        tag = next((candidate for candidate in tags if candidate.ruid == ruid), None)
        if tag is None:
            raise Refused(404, "error.no_tag")
        return templates.TemplateResponse(request, "tonie.html", {
            "tag": tag,
            "model": find_model(connection, tag.model) if tag.model else None,
            "categories": CATEGORIES,
            # Where a copy could go, the empty stickers first.
            "others": [other for other in tags if other.ruid != ruid],
        })

    @app.post("/tonies/{ruid}/copy")
    def copy_tonie(ruid: str, target: str = Form(...)):
        try:
            copy_tag(client(), ruid, target)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)

    @app.post("/tonies/{ruid}")
    async def edit_tonie(
        request: Request,
        ruid: str,
        series: str = Form(""),
        episode: str = Form(""),
        category: str = Form(DEFAULT_CATEGORY),
        cover: Optional[UploadFile] = File(None),
    ):
        connection = client()
        tag = next((candidate for candidate in list_tags(connection)
                    if candidate.ruid == ruid), None)
        if tag is None or not tag.model:
            raise Refused(404, "error.no_model")

        picture = None
        suffix = "jpg"
        if cover is not None and _is_image_name(cover.filename):
            picture = cover.file.read()
            suffix = Path(cover.filename).suffix.lstrip(".") or "jpg"

        try:
            update_model(connection, tag.model, series=series, episode=episode,
                         category=category, cover=picture, cover_name=suffix)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)

    @app.post("/tonies/{ruid}/delete")
    def delete_tonie(ruid: str):
        """Drop the model. The tag keeps its audio and goes on playing."""
        connection = client()
        tag = next((candidate for candidate in list_tags(connection)
                    if candidate.ruid == ruid), None)
        if tag is None or not tag.model:
            raise Refused(404, "error.no_model")
        remove_model(connection, tag.model, ruid=ruid)
        forget(settings.data_dir, tag.model)
        forget_model(settings.data_dir, tag.model)
        return RedirectResponse("/tonies", 303)

    @app.get("/api/box")
    def box_state():
        """What is on the box, for the page to poll.

        Carries the tonie's name as well as its ruid: the header says what is
        playing, and looking that up per page would mean every page knowing how
        to read the tag index.
        """
        state = box.state
        label = ""
        if state.known and state.on_box:
            try:
                label = next((tag.label for tag in list_tags(client())
                              if tag.ruid == state.ruid), "")
            except TeddyCloudError:
                label = ""
        return JSONResponse({
            "label": label,
            "ruid": state.ruid if state.known else None,
            "on_box": state.on_box and state.known,
            "playing": state.playing and state.known,
            "since": state.since.isoformat() if state.since and state.known else None,
            "stale": state.stale,
            "connected": state.connected,
            # A tag teddyCloud does not know: a fresh sticker, just held
            # against the box. The assignment page waits for this.
            "unknown_ruid": state.fresh_unknown,
            "unknown_since": state.unknown_since.isoformat() if state.unknown_since else None,
        })

    @app.get("/listening", response_class=HTMLResponse)
    def listening_page(request: Request, period: str = "30"):
        """Which tonies were heard, how much, and which have gone quiet.

        Only tags that still play something are listed: a sticker that was
        emptied has nothing to recommend or to miss.
        """
        if period not in LISTENING_PERIODS:
            period = "30"
        days = LISTENING_PERIODS[period]
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days) if days else None

        # The last bundle from the box is held back until it is complete;
        # take it now, so what was just heard is on the page.
        box.flush()
        events = listening.events()
        tags = list_tags(client())
        # How long each tonie plays caps what one run of it can be credited.
        times = playing_times(settings.data_dir)
        content = {tag.ruid: timedelta(seconds=times[tag.model])
                   for tag in tags if tag.model in times}
        summary = summarise(events, now=now, since=since, content=content)
        heard, quiet = [], []
        for tag in tags:
            if not tag.assigned:
                continue
            entry = summary.get(tag.ruid)
            if entry is not None and (entry.placed or entry.on_box):
                heard.append((tag, entry))
            else:
                quiet.append((tag, entry))

        heard.sort(key=lambda pair: (pair[1].played, pair[1].on_box, pair[1].placed),
                   reverse=True)
        # Never heard first, then the longest silence: the top of the list is
        # what has been forgotten longest.
        quiet.sort(key=lambda pair: (pair[1] is not None and pair[1].last_placed is not None,
                                     pair[1].last_placed if pair[1] and pair[1].last_placed
                                     else now))
        return templates.TemplateResponse(request, "listening.html", {
            "heard": heard,
            "quiet": quiet,
            "period": period,
            "days": days,
            "recorded_since": events[0].at if events else None,
        })

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

    @app.get("/checkup", response_class=HTMLResponse)
    def checkup_page(request: Request, done: str = "", stale: str = ""):
        findings = examine(client(), **work_in_progress())
        return templates.TemplateResponse(request, "checkup.html", {
            "findings": findings,
            "done": done,
            "stale": stale,
        })

    @app.post("/checkup/fix")
    def checkup_fix(fix: str = Form(...), target: str = Form(...)):
        """Apply one repair the checkup offered.

        The checks run again first and the repair is only made if they still
        find exactly this: a page left open for an hour must not delete a taf
        that has since been given to a tonie.
        """
        connection = client()
        current = next((finding for finding in examine(connection, **work_in_progress())
                        if finding.fix == fix and finding.target == target), None)
        if current is None:
            return RedirectResponse("/checkup?stale=1", 303)

        try:
            _apply_fix(connection, settings, fix, target)
        except (TonieError, EncodeError, TeddyCloudError, ValueError) as error:
            raise Refused(400, "error.failed", detail=error) from error
        log.info("checkup: %s applied to %s", fix, target)
        return RedirectResponse(f"/checkup?done={fix}", 303)

    @app.get("/podcasts", response_class=HTMLResponse)
    def podcasts_page(request: Request):
        tags = list_tags(client())
        labels = {tag.ruid: tag.label for tag in tags}
        return templates.TemplateResponse(request, "podcasts.html", {
            "subscriptions": load_subscriptions(settings.data_dir),
            "labels": labels,
            "tags": tags,
            "keep": DEFAULT_KEEP,
        })

    @app.post("/podcasts")
    def subscribe(feed: str = Form(...), ruid: str = Form(...), keep: int = Form(DEFAULT_KEEP)):
        """Check the feed now, so a wrong address is said on the form, then start."""
        feed = feed.strip()
        try:
            parsed = parse_feed(fetch(feed, LARGEST_FEED))
        except (FeedError, requests.RequestException) as error:
            raise Refused(400, "error.feed_unreadable", detail=error) from error
        if not parsed.episodes:
            raise Refused(400, "feed.no_audio")
        if not any(tag.ruid == ruid for tag in list_tags(client())):
            raise Refused(400, "error.no_tag")

        subscription = Subscription(id=uuid.uuid4().hex[:10], feed=feed, ruid=ruid,
                                    keep=max(1, min(keep, 20)), title=parsed.title)
        add_subscription(settings.data_dir, subscription)
        job_id = queue_podcast(subscription.id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)

    @app.post("/podcasts/{subscription_id}/refresh")
    def refresh_now(subscription_id: str):
        job_id = queue_podcast(subscription_id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)

    @app.post("/podcasts/{subscription_id}/delete")
    def unsubscribe(subscription_id: str):
        """Stop updating. The sticker keeps playing the episodes it has."""
        remove_subscription(settings.data_dir, subscription_id)
        return RedirectResponse("/podcasts", 303)

    @app.get("/backup", response_class=HTMLResponse)
    def backup_page(request: Request):
        parts = describe_backup(client(), settings.data_dir)
        return templates.TemplateResponse(request, "backup.html", {
            "parts": parts,
            "total": sum(part.size for part in parts),
        })

    @app.get("/backup/download")
    def backup_download():
        data = create_backup(client(), settings.data_dir)
        name = f"tonie-studio-{datetime.now().strftime('%Y-%m-%d')}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/restore", response_class=HTMLResponse)
    async def restore_preview(request: Request, file: UploadFile = File(...)):
        """Show what a restore would do. Nothing is written until confirmed.

        The upload is parked on the volume under a random name, so the
        confirmation acts on exactly the file that was previewed.
        """
        # Read at most one byte past the limit: enough to know it is too big
        # without holding all of it.
        data = await file.read(LARGEST_BACKUP + 1)
        try:
            backup = read_backup(data)
        except BackupError as error:
            raise Refused(400, "error.failed", detail=error) from error

        token = uuid.uuid4().hex
        _restore_dir(settings).mkdir(parents=True, exist_ok=True)
        (_restore_dir(settings) / f"{token}.zip").write_bytes(data)
        return templates.TemplateResponse(request, "restore.html", {
            "steps": plan_restore(client(), backup, settings.data_dir),
            "manifest": backup.manifest,
            "token": token,
            "done": False,
        })

    @app.post("/restore/{token}", response_class=HTMLResponse)
    def restore_apply(request: Request, token: str):
        if not token.isalnum():
            raise Refused(400, "error.not_a_restore")
        parked = _restore_dir(settings) / f"{token}.zip"
        if not parked.is_file():
            raise Refused(404, "error.restore_gone")
        try:
            backup = read_backup(parked.read_bytes())
            steps = apply_restore(client(), backup, settings.data_dir)
        except BackupError as error:
            raise Refused(400, "error.failed", detail=error) from error
        finally:
            parked.unlink(missing_ok=True)
        return templates.TemplateResponse(request, "restore.html", {
            "steps": steps,
            "manifest": backup.manifest,
            "token": token,
            "done": True,
        })

    @app.get("/cover/custom/{name}")
    def custom_cover(name: str, mode: Optional[str] = None, size: int = 0):
        """Pass a cover through from teddyCloud, optionally as the sticker.

        The browser can reach this app but not teddyCloud's own address, which
        sits behind the gateway under a different name, so linking straight to
        the image would give a broken picture.

        With a ``mode`` the picture comes back the way the sheet would print it
        in that mode, so a preview on screen and the sticker in hand are the
        same rendering rather than two that merely resemble each other.
        """
        if "/" in name or ".." in name:
            raise Refused(400, "error.not_an_image")
        try:
            data = client().fetch(f"/custom_img/{name}")
        except TeddyCloudError as error:
            raise Refused(502, "error.failed", detail=error) from error

        if mode in (FILL, FIT):
            try:
                data = render_square(data, mode, size=max(64, min(size or PIXELS, PIXELS)))
            except (OSError, ValueError) as error:
                # A cover teddyCloud holds but Pillow cannot read still has to
                # reach the page; the untouched bytes may well render there.
                log.debug("cannot render %s as a sticker: %s", name, error)
            else:
                return Response(data, media_type="image/jpeg")
        return Response(data, media_type=mimetypes.guess_type(name)[0] or "image/jpeg")

    @app.get("/album/{identifier}/crop")
    def crop_preview(identifier: str, index: int = 0, zoom: float = 1.0,
                     x: float = 0.0, y: float = 0.0, size: int = 256):
        """The cover as the round sticker will cut it."""
        candidates = cover_candidates(_resolve(settings, identifier))
        if index >= len(candidates):
            raise Refused(404, "error.no_cover")
        rendered = crop(candidates[index], zoom, x, y, max(32, min(size, 800)))
        if rendered is None:
            raise Refused(404, "error.cover_unreadable")
        return Response(rendered, media_type="image/jpeg")

    @app.get("/radio", response_class=HTMLResponse)
    def radio_page(request: Request):
        connection = client()
        tags = list_tags(connection)
        return templates.TemplateResponse(request, "radio.html", {
            "tags": tags,
            # Not "://" in source: a library path is lib:// and would match.
            "streams": [tag for tag in tags
                        if tag.source.lower().startswith(("http://", "https://"))],
            "categories": CATEGORIES,
            "stations": RADIO_SUGGESTIONS,
        })

    @app.post("/radio")
    async def make_radio(
        ruid: str = Form(...),
        url: str = Form(...),
        series: str = Form(""),
        episode: str = Form(""),
        category: str = Form(DEFAULT_CATEGORY),
        cover: Optional[UploadFile] = File(None),
    ):
        picture = None
        suffix = "jpg"
        if cover is not None and _is_image_name(cover.filename):
            picture = cover.file.read()
            suffix = Path(cover.filename).suffix.lstrip(".") or "jpg"
        try:
            publish_stream(client(), ruid, url, series=series, episode=episode,
                           cover=picture, cover_name=suffix, category=category)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request):
        connection = client()
        tafs = list_tafs(connection)
        # Which tag plays which taf, so deleting one can say what it breaks.
        users: Dict[str, List] = {}
        for tag in list_tags(connection):
            if tag.source:
                users.setdefault(unicodedata.normalize("NFC", tag.source), []).append(tag)
        for taf in tafs:
            taf["used_by"] = users.get(unicodedata.normalize("NFC", taf["source"]), [])
        return templates.TemplateResponse(request, "library.html", {
            "tafs": tafs,
            "total": sum(taf["size"] for taf in tafs),
        })

    @app.post("/library/delete")
    def library_delete(path: str = Form(...)):
        try:
            delete_taf(client(), path)
        except (EncodeError, TeddyCloudError) as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/library", 303)

    @app.get("/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request, model: str = "", calibrated: str = ""):
        """The sheet, optionally with one motif already asked for.

        Coming from a tonie that was just recorded, hunting for it again in
        this list is the step that was missing.
        """
        models = _printable_models(client())
        return templates.TemplateResponse(request, "sheet.html", {
            "models": models,
            "per_page": PER_PAGE,
            "fill": FILL,
            "fit": FIT,
            "preset": model,
            "backs": _back_preview(models, playing_times(settings.data_dir), translator(request)),
            "back_fade": BACK_FADE,
            "diameter_mm": DIAMETER_MM,
            "shift": read_shift(settings.data_dir),
            "expected": expected_first_centre(),
            "calibrated": bool(calibrated),
        })

    @app.get("/sheet/calibration")
    def calibration_sheet(request: Request):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"calibration-{uuid.uuid4().hex[:8]}.pdf"
        # Printed with the current correction, so measuring it shows what is
        # still off rather than what was off before.
        build_calibration(target, read_shift(settings.data_dir), translator(request))
        return FileResponse(
            target,
            media_type="application/pdf",
            filename="kalibrierblatt.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/sheet/calibration")
    def calibrate(left_mm: str = Form(...), top_mm: str = Form(...)):
        try:
            left, top = _millimetres(left_mm), _millimetres(top_mm)
        except ValueError as error:
            raise Refused(400, "error.millimetres") from error
        save_measurement(settings.data_dir, left, top)
        return RedirectResponse("/sheet?calibrated=1#printer", 303)

    @app.post("/sheet/calibration/reset")
    def reset_calibration():
        want_left, want_top = expected_first_centre()
        # Measured exactly where it belongs, minus what is stored, is no shift.
        right, down = read_shift(settings.data_dir)
        save_measurement(settings.data_dir, want_left + right, want_top + down)
        return RedirectResponse("/sheet#printer", 303)

    @app.get("/sheet/overview")
    def overview(request: Request):
        translate = translator(request)
        entries = _overview_entries(client(), playing_times(settings.data_dir), translate)
        if not entries:
            raise Refused(400, "error.nothing_to_list")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"overview-{uuid.uuid4().hex[:8]}.pdf"
        build_overview(entries, target, translate)
        return FileResponse(
            target,
            media_type="application/pdf",
            filename="tonies-uebersicht.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/sheet")
    def sheet(
        request: Request,
        models: List[str] = Form(default=[]),
        counts: List[int] = Form(default=[]),
        modes: List[str] = Form(default=[]),
        files: List[UploadFile] = Form(default=[]),
        upload_mode: str = Form(FILL),
        back: str = Form(""),
        back_tracks: str = Form(""),
    ):
        # models, counts and modes are three parallel lists in row order, the
        # same shape the album page uses for its tracks.
        connection = client()
        stickers = []
        described = {entry["model"]: entry for entry in _printable_models(connection)}
        times = playing_times(settings.data_dir)

        for position, model in enumerate(models):
            count = counts[position] if position < len(counts) else 0
            if count < 1:
                continue
            picture = _model_picture(connection, model)
            if picture is None:
                continue
            mode = modes[position] if position < len(modes) else FILL
            stickers.append(Sticker(image=picture, count=count, label=model,
                                    mode=FIT if mode == FIT else FILL))
            # One text side per copy, right after it, so a sheet cut up in
            # order leaves each pair together.
            if back and model in described:
                description = _back_of(described[model], times.get(model), translator(request))
                if not back_tracks:
                    description.tracks = ()
                stickers.append(Sticker(image=picture, count=count, label=f"{model} back",
                                        back=description))

        for upload_file in files:
            if _is_image_name(upload_file.filename):
                stickers.append(Sticker(image=upload_file.file.read(),
                                        label=upload_file.filename or "upload",
                                        mode=FIT if upload_mode == FIT else FILL))

        if not stickers:
            raise Refused(400, "error.nothing_to_print")

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"sheet-{uuid.uuid4().hex[:8]}.pdf"
        skipped = build_sheet(stickers, target, read_shift(settings.data_dir))
        if skipped:
            log.warning("left off the sheet: %s", ", ".join(skipped))

        return FileResponse(
            target,
            media_type="application/pdf",
            filename="traveltonies.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.get("/api/job/{job_id}")
    def job_status(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise Refused(404, "error.no_job")
        result = job.result if isinstance(job.result, dict) else {}
        return JSONResponse({
            "state": job.state.value,
            "step": job.step,
            "done": job.done_count,
            "total": job.total,
            "percent": job.percent,
            "error": job.error,
            "target": job.label,
            "assigned": bool(result.get("model")),
            # Why a batch encode did not get its sticker, for the reader.
            "note": translator(request)(result["assign_error"]) if result.get("assign_error") else "",
        })

    return app


# --- helpers --------------------------------------------------------------


def _printable_models(client: TeddyCloudClient) -> List[Dict]:
    """The custom models that have a cover of their own to print.

    A model without a picture, or one pointing at an image hosted elsewhere,
    has nothing this app can put on a sticker.
    """
    return [info for info in map(_model_info, client.custom_tonies()) if info["image"]]


def _model_info(model: Dict) -> Dict:
    """What the printing pages need of one model; ``image`` is empty without a cover here."""
    picture = str(model.get("pic") or "")
    return {
        "model": model.get("model", ""),
        "series": model.get("series", ""),
        "episodes": model.get("episodes", ""),
        "image": picture[len(CUSTOM_IMG_PREFIX):] if picture.startswith(CUSTOM_IMG_PREFIX) else "",
        "tracks": [str(track) for track in model.get("tracks") or []],
        "year": year_of(model),
    }


def _overview_entries(client: TeddyCloudClient, times: Dict[str, int],
                      translate: Optional[Translator] = None) -> List[Entry]:
    """One entry per model that sits on a sticker, in shelf order.

    A model no sticker names is not a tonie anyone can pick up, so it does not
    belong on a list of what is in the tin.
    """
    on_stickers = {tag.model for tag in list_tags(client) if tag.model}
    entries = []
    for info in map(_model_info, client.custom_tonies()):
        if info["model"] not in on_stickers:
            continue
        image = None
        if info["image"]:
            try:
                image = client.fetch(f"{CUSTOM_IMG_PREFIX}{info['image']}")
            except TeddyCloudError as error:
                log.warning("overview: no cover for %s: %s", info["model"], error)
        back = _back_of(info, times.get(info["model"]), translate)
        entries.append(Entry(image=image, title=back.title, series=back.series,
                             facts=list(back.notes)))
    return sorted(entries, key=lambda entry: (entry.series.casefold(), entry.title.casefold()))


def _back_of(model: Dict, seconds: Optional[int] = None,
             translate: Optional[Translator] = None) -> Back:
    """What the text side of one model says.

    The episode names the album and the series names who made it, which is the
    order they are looked for in: a shelf of tonies is searched by album.
    Facts the model does not carry are left out rather than guessed at, so a
    tonie recorded before the app stored them simply says less.
    """
    translate = translate or Translator(DEFAULT_LANGUAGE)
    tracks = model.get("tracks") or []
    facts = [
        str(model.get("year") or ""),
        translate("pdf.tracks", count=len(tracks)) if tracks else "",
        translate("pdf.minutes", count=minutes(seconds)) if minutes(seconds) else "",
    ]
    notes = [" - ".join(fact for fact in facts if fact), model.get("model", "")]
    return Back(
        title=model.get("episodes") or model.get("series") or model.get("model", ""),
        series=model.get("series", ""),
        notes=[note for note in notes if note],
        tracks=tracks,
        language=translate.lang,
    )


def _back_preview(models: Sequence[Dict], times: Dict[str, int],
                  translate: Optional[Translator] = None) -> Dict[str, Dict[str, List[Dict]]]:
    """The laid-out text side of every model, for the page to draw.

    The lines come from the same call the sheet makes, so what the preview
    shows is what gets printed; only the font differs.
    """
    preview = {}
    for model in models:
        back = _back_of(model, times.get(model["model"]), translate)
        preview[model["model"]] = {
            variant: [{"text": line.text, "size": line.size, "bold": line.bold}
                      for line in back_lines(back, with_tracks=with_tracks)]
            for variant, with_tracks in (("tracks", True), ("plain", False))
        }
    return preview


def _model_picture(client: TeddyCloudClient, model_id: str) -> Optional[bytes]:
    """The cover bytes for one model, or None when it cannot be read."""
    for model in _printable_models(client):
        if model["model"] == model_id:
            try:
                return client.fetch(f"{CUSTOM_IMG_PREFIX}{model['image']}")
            except TeddyCloudError as error:
                log.warning("cannot read the cover of %s: %s", model_id, error)
                return None
    return None


def _encode_job(connection: TeddyCloudClient, chosen: Sequence[Tuple[object, str]], target: str,
                replace: bool, carried: Dict,
                after: Optional[Callable[[Dict], None]] = None) -> Callable:
    """The work of one encode, returning what the tag and model steps need.

    ``carried`` goes into the result unchanged: the names a person approved
    rather than the ones that were guessed, the cover, and what the printed
    text side and the album list read back. ``after`` runs once the taf
    exists and may add to the result; it must not raise for a failure the
    encode itself does not share.
    """
    paths = [track.path for track, _name in chosen]
    names = [name for _track, name in chosen]
    # The playing time turns the encode from a dead bar into a position;
    # teddyCloud itself reports nothing while ffmpeg runs.
    playing_time = sum(track.duration or 0 for track, _name in chosen) or None

    def work(progress):
        encoded = encode_files(connection, paths, target, progress=progress,
                               replace=replace, total_seconds=playing_time)
        result = {"encode": encoded, "tracks": names, "seconds": playing_time, **carried}
        if after is not None:
            after(result)
        return result
    return work


def _publish_result(connection: TeddyCloudClient, settings: Settings, ruid: str, result: Dict,
                    series: str, episode: str, category: str = DEFAULT_CATEGORY) -> str:
    """Put a finished encode on a tag, and record what the app keeps about it."""
    model_id = publish(
        connection,
        ruid,
        result["encode"],
        series=series,
        episode=episode,
        tracks=result["tracks"],
        cover=_cropped_cover(result),
        category=category,
        year=result.get("year"),
    )
    remember(settings.data_dir, model_id, result.get("seconds"))
    remember_made(settings.data_dir, result.get("album", ""), model_id)
    return model_id


def _publish_after(connection: TeddyCloudClient, settings: Settings, ruid: str,
                   series: str, episode: str) -> Callable[[Dict], None]:
    """The step that puts a batch encode on the tag planned for it.

    A tag that was given something else since the plan is left alone: the
    encode still counts, and its page offers the ordinary assignment. A
    failure to publish is recorded the same way rather than failing a job
    whose taf is fine.
    """
    def after(result: Dict) -> None:
        if ruid not in _free_tags(connection):
            result["assign_error"] = "batch.tag_taken"
            return
        try:
            result["model"] = _publish_result(connection, settings, ruid, result, series, episode)
            result["ruid"] = ruid
        except (TonieError, TeddyCloudError) as error:
            log.warning("batch: could not put %s on %s: %s", episode, ruid, error)
            result["assign_error"] = "batch.publish_failed"
    return after


# What the progress page says after a sticker was placed, by outcome.
PLACE_MESSAGES = {
    "unknown": "place.unknown",
    "taken": "place.taken",
    "reserved": "place.reserved",
    "assigned": "place.assigned",
    "complete": "place.complete",
    "failed": "place.failed",
}


def _publish_when_placed(connection: TeddyCloudClient, settings: Settings, placing: Placing,
                         job_id: str, series: str, episode: str) -> Callable[[Dict], None]:
    """The step that puts a batch encode on the sticker placed for it meanwhile.

    Without a sticker yet nothing happens here; the one placed later is
    published by the request that reserves it.
    """
    def after(result: Dict) -> None:
        ruid = placing.settle(job_id)
        if ruid is None:
            return
        try:
            result["model"] = _publish_result(connection, settings, ruid, result, series, episode)
            result["ruid"] = ruid
        except (TonieError, TeddyCloudError) as error:
            log.warning("batch: could not put %s on %s: %s", episode, ruid, error)
            result["assign_error"] = "batch.publish_failed"
    return after


def _next_to_place(placing: Optional[Placing]) -> str:
    """The episode the next sticker put on the box will get, for the page."""
    if placing is None:
        return ""
    waiting = placing.waiting(lambda job_id: False)
    return waiting.episode if waiting else ""


def _free_tags(connection: TeddyCloudClient) -> List[str]:
    """Tags the box has read that play nothing yet, in the order they are handed out."""
    return [tag.ruid for tag in list_tags(connection) if not tag.assigned]


@dataclass
class BatchRow:
    """One album of a batch, as planned."""

    identifier: str
    album: Album
    series: str
    episode: str
    chosen: List[Tuple[object, str]]
    groups: List[List[int]]
    targets: List[str]
    occupied: bool
    # Whether a tag plays an existing taf; only meaningful when occupied.
    in_use: bool = False
    # The model already made from this album, if any.
    tonie: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        """Left out of the encoding: its taf or its tonie exists already."""
        return self.occupied or bool(self.tonie)


def _plan_batch(connection: TeddyCloudClient, settings: Settings, ids: Sequence[str],
                series: Optional[Dict[str, str]] = None, episodes: Optional[Dict[str, str]] = None,
                tonies: Optional[Dict[str, str]] = None) -> List[BatchRow]:
    """Each album with its default tracks, its taf paths and its stickers.

    Every album takes what its own page would offer untouched: the tracks
    that are not karaoke takes, in the album's order, the first cover, and as
    few parts as teddyCloud allows. An album whose taf already exists, or
    that a tonie was already made from (``tonies``, by album id), is left out
    rather than encoded again: the name typed back then may differ from the
    suggestion, and a second copy is a decision for the album's own page.

    ``series`` and ``episodes`` hold what the page sent, by album id; an
    album missing from them gets the suggestion. A series sent empty stays
    empty, since that is a choice the album page allows too.
    """
    series, episodes = series or {}, episodes or {}
    played = {tag.source for tag in list_tags(connection)}
    rows = []
    for identifier in dict.fromkeys(ids):
        album = read_album(_resolve(settings, identifier))
        row_series = (series[identifier] if identifier in series
                      else _suggest_series(settings, album)).strip()
        episode = (episodes.get(identifier) or "").strip() or album.title
        chosen = [(track, track.title) for track in album.tracks if not track.extra]
        parts = fewest_parts(len(chosen))
        try:
            targets = [library_path(row_series, part_name(episode, number, parts))
                       for number in range(1, parts + 1)]
        except EncodeError as error:
            raise Refused(400, "error.failed", detail=error) from error
        rows.append(BatchRow(
            identifier=identifier, album=album, series=row_series, episode=episode,
            chosen=chosen,
            groups=split([track.duration for track, _name in chosen], parts),
            targets=targets,
            occupied=any(taf_exists(connection, target) for target in targets),
            in_use=any(f"lib://{target.lstrip('/')}" in played for target in targets),
            tonie=(tonies or {}).get(identifier, ""),
        ))
    # Grouped by series and in episode order within one, which is also the
    # order the stickers are handed out in.
    rows.sort(key=lambda row: (row.series.casefold(), episode_order(row.episode)))

    waiting = [row for row in rows if not row.skipped]
    for row, tags in zip(waiting, allocate([len(row.targets) for row in waiting],
                                           _free_tags(connection)), strict=True):
        row.tags = tags
    return rows


def _suggest_series(settings: Settings, album: Album) -> str:
    """The series an album most likely belongs to.

    Its artist when the tags name one; otherwise the folder holding it, the
    way a series is usually kept on disk. A root of the archive or an upload
    is not a name anyone chose.
    """
    if album.artist:
        return album.artist
    parent = album.path.parent
    if parent in (settings.archive_dir, settings.upload_dir) or parent.parent == settings.upload_dir:
        return ""
    return parent.name


def _upload_folders(files: Sequence[UploadFile]) -> Dict[str, List[UploadFile]]:
    """The uploaded files that belong to an album, by the folder they came from.

    A folder upload names every file by its path below the folder picked.
    Images come along so a cover can be uploaded with the tracks; a folder
    with no audio at all, such as a series folder holding only a picture,
    is not an album and is dropped.
    """
    folders: Dict[str, List[UploadFile]] = {}
    for file in files:
        if not (_is_audio_name(file.filename) or _is_image_name(file.filename)):
            continue
        folder = str(PurePosixPath((file.filename or "").replace("\\", "/")).parent)
        folders.setdefault(folder, []).append(file)
    return {folder: members for folder, members in folders.items()
            if any(_is_audio_name(member.filename) for member in members)}


def _subfolders(root: Path, folders: Dict[str, List[UploadFile]]) -> Dict[str, Path]:
    """One folder below ``root`` per uploaded folder, named like it and its parent.

    The names matter: an album without tags is called after its folder, and
    a series without an artist tag after the folder holding its episodes.
    Only those two levels are kept; anything deeper would sit below what the
    album scan reaches. Two folders that end alike get a number.
    """
    taken: set = set()
    destinations = {}
    for folder in folders:
        parts = [part for part in map(_base_name, PurePosixPath(folder).parts) if part][-2:]
        parts = parts or ["album"]
        name, number = parts[-1], 2
        while str(PurePosixPath(*parts)).casefold() in taken:
            parts[-1], number = f"{name} ({number})", number + 1
        taken.add(str(PurePosixPath(*parts)).casefold())
        destinations[folder] = root.joinpath(*parts)
    return destinations


def _base_name(name: Optional[str]) -> str:
    """The last part of a path a browser sent, safe to use as a file name.

    Browsers send folder uploads with "/" and some Windows ones with "\\";
    either would put a file somewhere other than the folder it is written to.
    """
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return "" if base in (".", "..") else base


def _cropped_cover(result: Dict) -> Optional[bytes]:
    """The album cover, cut the way the album page showed it.

    Cropping here rather than at print time means what teddyCloud displays and
    what comes out of the printer are the same picture.
    """
    cover = result.get("cover")
    if not cover:
        return None
    crop_settings = result.get("crop") or {}
    return crop(
        Path(cover),
        zoom=crop_settings.get("zoom", 1.0),
        offset_x=crop_settings.get("x", 0.0),
        offset_y=crop_settings.get("y", 0.0),
    )


def _finished_result(jobs: JobRunner, job_id: str) -> Dict:
    """The result of a finished encode, or a refusal explaining which it is not."""
    job = jobs.get(job_id)
    if job is None:
        raise Refused(404, "error.no_job")
    if job.state is not JobState.DONE:
        raise Refused(409, "error.no_taf_yet")
    return job.result


# Keyed by the directories scanned, not only by `describe`: two apps in one
# process - which is every test run - must not hand each other their archives.
_catalogue_cache: Dict[Tuple, Tuple[float, List[Tuple[str, AlbumSummary, str]]]] = {}
_catalogue_lock = Lock()


def _forget_catalogue() -> None:
    with _catalogue_lock:
        _catalogue_cache.clear()


def _catalogue(
    settings: Settings, describe: bool = True,
) -> List[Tuple[str, AlbumSummary, str]]:
    """Cached front for _scan_albums; see CATALOGUE_TTL."""
    key = (str(settings.archive_dir), str(settings.upload_dir), describe)
    now = time.monotonic()
    with _catalogue_lock:
        cached = _catalogue_cache.get(key)
        if cached and now - cached[0] < CATALOGUE_TTL:
            return cached[1]

    scanned = _scan_albums(settings, describe)
    with _catalogue_lock:
        _catalogue_cache[key] = (now, scanned)
    return scanned


def _scan_albums(
    settings: Settings, describe: bool = True,
) -> List[Tuple[str, AlbumSummary, str]]:
    """Every album on offer, as (id, summary, where it came from).

    Uploads come first: what was just added is what someone is looking for.
    """
    entries = []
    for prefix, root, origin in (
        (UPLOAD, settings.upload_dir, "upload"),
        (ARCHIVE, settings.archive_dir, "archive"),
    ):
        if not root.is_dir():
            continue
        for summary in list_albums(root, describe=describe):
            entries.append((f"{prefix}{summary.id}", summary, origin))
    return entries


def _resolve(settings: Settings, identifier: str) -> Path:
    """Find the folder an album id names, or refuse.

    Deliberately without the descriptions: the picker draws one preview per
    album, and each of those requests lands here. Reading every album's tags
    again for each of them turned a page load into seconds of network file
    opens.
    """
    for candidate_id, summary, _origin in _catalogue(settings, describe=False):
        if candidate_id == identifier:
            return summary.path
    raise Refused(404, "error.album_gone")


def _chosen_tracks(
    album: Album, order: str, selected: List[int], titles: List[str],
) -> List[Tuple[object, str]]:
    """The tracks to encode with their names, in the order the page put them in.

    ``order`` carries the track numbers as the list was arranged and is what
    the titles are aligned to: both are submitted in the order the rows sit in,
    so the nth title belongs to the nth number. Reading a title by its track
    number instead would hand the wrong name to every reordered track.
    """
    by_number = {track.number: track for track in album.tracks}
    numbers = [int(part) for part in order.split(",") if part.strip().isdigit()]
    if not numbers:
        numbers = list(by_number)

    keep = set(selected)
    chosen = []
    for position, number in enumerate(numbers):
        track = by_number.get(number)
        if track is None or number not in keep:
            continue
        name = titles[position].strip() if position < len(titles) else ""
        chosen.append((track, name or track.title))
    return chosen


def _suggest_target(album: Album) -> Dict[str, str]:
    """What to prefill the series and episode fields with."""
    return {"series": album.artist or "", "episode": album.title}


def _millimetres(text: str) -> float:
    """A length as typed, with a decimal comma or point.

    float() also reads "nan" and "inf", which are not lengths and would turn
    into the largest allowed shift without anyone noticing.
    """
    value = float(text.strip().replace(",", ".").removesuffix("mm").strip())
    if not math.isfinite(value):
        raise ValueError(f"not a length: {text!r}")
    return value


def _restore_dir(settings: Settings) -> Path:
    """Where an uploaded backup waits between preview and confirmation."""
    return settings.data_dir / "restore"


def _clear_parked_restores(settings: Settings, older_than: float = 86400.0) -> None:
    """Drop backups uploaded for a preview that was never confirmed."""
    folder = _restore_dir(settings)
    if not folder.is_dir():
        return
    cutoff = time.time() - older_than
    for parked in folder.glob("*.zip"):
        try:
            if parked.stat().st_mtime < cutoff:
                parked.unlink()
        except OSError as error:
            log.warning("cannot remove %s: %s", parked, error)


def _apply_fix(client: TeddyCloudClient, settings: Settings, fix: str, target: str) -> None:
    """Make one checkup repair. The caller has just confirmed it is still due."""
    if fix == "nocloud":
        client.set_tag(target, nocloud=True)
    elif fix == "repair_source":
        entry = next((tag for tag in client.tags() if tag.get("ruid") == target), None)
        fixed = repaired((entry or {}).get("source"))
        if not fixed:
            raise ValueError(f"nothing to repair on {target}")
        client.set_tag(target, source=fixed)
    elif fix == "repair_model":
        model = find_model(client, target)
        if model is None:
            raise ValueError(f"there is no model {target}")
        changes = model_repairs(model)
        update_model(client, target, series=changes.get("series"),
                     episode=changes.get("episodes"), tracks=changes.get("tracks"))
    elif fix == "delete_taf":
        delete_taf(client, target)
    elif fix == "delete_model":
        remove_model(client, target)
        forget(settings.data_dir, target)
        forget_model(settings.data_dir, target)
    else:
        raise ValueError(f"unknown repair {fix!r}")


def _duration(value: Optional[timedelta]) -> str:
    """A listening time as a person says it: "1 h 24 min", "24 min"."""
    if not value:
        return "-"
    total = int(value.total_seconds() // 60)
    if total < 1:
        return "< 1 min"
    hours, rest = divmod(total, 60)
    return f"{hours} h {rest} min" if hours else f"{rest} min"


class Refused(Problem):
    """A request turned down, with the HTTP status to answer it with."""

    def __init__(self, status: int, key: str, **params):
        super().__init__(key, **params)
        self.status = status


def translator(request: Request) -> Translator:
    """The translator for whoever sent ``request``."""
    return Translator(negotiate(request.cookies.get(LANG_COOKIE),
                                request.headers.get("accept-language")))


def _language(request: Request) -> Dict:
    """What every page needs to speak the reader's language."""
    translate = translator(request)
    return {"t": translate, "lang": translate.lang, "languages": languages()}


def _is_audio_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _is_image_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
