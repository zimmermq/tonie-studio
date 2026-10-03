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
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .album import (AUDIO_EXTENSIONS, Album, AlbumSummary, album_id, cover_candidates,
                     list_albums, read_album)
from .albumnotes import first_sightings, forget_model, guess_made, is_new, made, remember_made
from .backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                     plan_restore, read_backup)
from .boxstate import BoxWatcher
from .calibration import build_calibration, expected_first_centre, read_shift, save_measurement
from .checkup import examine, model_repairs, repaired
from .encode import (EncodeError, delete_taf, encode_files, library_path, list_tafs,
                     remove_orphaned_staging, taf_exists)
from .jobs import JobRunner, JobState
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

# What a job state is called in the interface; the values stay English because
# they are the API's own.
JOB_STATES = {
    "queued": "wartet",
    "running": "läuft",
    "done": "fertig",
    "failed": "fehlgeschlagen",
}

# What `album.ordering` is called in the interface. The values themselves stay
# in English because they are identifiers the tests pin.
ORDERING_NAMES = {
    "filename number": "Dateinummer",
    "track tag": "Track-Tag",
    "playlist": "Playlist",
    "name": "Dateiname",
}

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
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["duration"] = _duration
    templates.env.filters["when"] = _when
    templates.env.filters["filesize"] = _filesize
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
                    # why the episodes are old, not only the job list.
                    subscription.error = str(error) or error.__class__.__name__
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
            "states": JOB_STATES,
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

    def _albums_with_tonies(albums) -> Dict[str, str]:
        """Album id to the tonie made from it, recorded or matched by name.

        What was recorded is trusted as it stands. The models come from a copy
        that can be a minute old, and checking a record against it would hide a
        tonie made in that minute. A model deleted through this app takes its
        record with it; one deleted in teddyCloud's own interface leaves the
        marker behind, which costs a wrong label and nothing else.
        """
        done = {identifier: models[-1] for identifier, models in made(settings.data_dir).items()}
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
        """Spool a browser upload into its own folder, then treat it as an album."""
        audio = [file for file in files if _is_audio_name(file.filename)]
        if not audio:
            raise HTTPException(400, "None of those files is audio teddyCloud can encode.")

        folder = settings.upload_dir / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        for file in files:
            # Images come along so a cover can be uploaded with the tracks.
            if not (_is_audio_name(file.filename) or _is_image_name(file.filename)):
                continue
            with (folder / Path(file.filename).name).open("wb") as target:
                shutil.copyfileobj(file.file, target)

        # The new folder has to show up at once, not in ten seconds.
        _forget_catalogue()
        return RedirectResponse(f"/album/{UPLOAD}{album_id(settings.upload_dir, folder)}", 303)

    def _album_page(request: Request, identifier: str, album: Album, conflict: str = "",
                    replace_value: str = ""):
        return templates.TemplateResponse(request, "album.html", {
            "id": identifier,
            "album": album,
            "suggested_target": _suggest_target(album),
            "conflict": conflict,
            "replace_value": replace_value or conflict,
            "orderings": ORDERING_NAMES,
            "most_chapters": MOST_CHAPTERS,
        })

    # Which jobs belong to one album cut into parts. In memory like the jobs
    # themselves: a restart loses both together.
    groups: Dict[str, List[str]] = {}

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
            raise HTTPException(404, "No cover image here.")

        path = candidates[index]
        if not size:
            return FileResponse(path)

        preview = thumbnail(path, size)
        if preview is None:
            raise HTTPException(404, "That cover cannot be read.")
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
            raise HTTPException(400, "No track selected.")
        if parts < 1:
            raise HTTPException(400, "Mindestens ein Teil.")
        if parts > len(chosen):
            raise HTTPException(
                400, f"{len(chosen)} Tracks lassen sich nicht in {parts} Teile teilen; "
                     f"jeder Teil braucht mindestens einen Track.")
        if parts < fewest_parts(len(chosen)):
            raise HTTPException(
                400,
                f"{len(chosen)} Tracks passen nicht in ein Taf: teddyCloud kann höchstens "
                f"{MOST_CHAPTERS} Kapitel anspringen und hängt den Rest still an das letzte. "
                f"Mindestens {fewest_parts(len(chosen))} Teile wählen.",
            )
        try:
            groups_of_tracks = split([track.duration for track, _name in chosen], parts)
            targets = [library_path(series, part_name(episode, number, parts))
                       for number in range(1, parts + 1)]
        except (EncodeError, ValueError) as error:
            raise HTTPException(400, str(error)) from error

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

        def job_for(number: int, indices: List[int], target: str) -> Callable:
            part = [chosen[index] for index in indices]
            paths = [track.path for track, _name in part]
            names = [name for _track, name in part]
            # The playing time turns the encode from a dead bar into a
            # position; teddyCloud itself reports nothing while ffmpeg runs.
            playing_time = sum(track.duration or 0 for track, _name in part) or None

            def work(progress):
                result = encode_files(connection, paths, target, progress=progress,
                                      replace=target in occupied, total_seconds=playing_time)
                # Carried along for the tag and model steps, which need the
                # names a person approved rather than the ones that were guessed.
                return {"encode": result, "series": series,
                        "episode": part_name(episode, number, parts),
                        "tracks": names, "cover": cover, "crop": crop_settings,
                        # Both only for the printed text side; nothing plays
                        # differently because of them.
                        "year": album.year, "seconds": playing_time,
                        # So the album list can say this one is done.
                        "album": identifier}
            return work

        job_ids = [jobs.submit("encode", target, job_for(number, indices, target))
                   for number, (indices, target)
                   in enumerate(zip(groups_of_tracks, targets, strict=True), 1)]
        if len(job_ids) == 1:
            return RedirectResponse(f"/job/{job_ids[0]}", 303)
        group = uuid.uuid4().hex[:12]
        groups[group] = job_ids
        return RedirectResponse(f"/parts/{group}", 303)

    @app.get("/parts/{group}", response_class=HTMLResponse)
    def parts_page(request: Request, group: str):
        if group not in groups:
            raise HTTPException(404, "Those parts are not known; the app may have restarted.")
        return templates.TemplateResponse(request, "parts.html", {
            "jobs": [jobs.get(job_id) for job_id in groups[group] if jobs.get(job_id)],
            "states": JOB_STATES,
        })

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        # A podcast refresh puts its taf on the sticker itself; only an album
        # encode still has to be given to one.
        podcast = job.kind == "podcast"
        return templates.TemplateResponse(request, "job.html", {
            "job": job,
            "states": JOB_STATES,
            "done_text": "Die neuen Folgen sind auf dem Sticker." if podcast else "Taf fertig.",
            "next_href": "/podcasts" if podcast else f"/job/{job.id}/assign",
            "next_label": "Zu den Podcasts" if podcast else "Auf einen Tonie legen",
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
            model_id = publish(
                client(),
                ruid,
                result["encode"],
                series=series,
                episode=episode,
                tracks=result["tracks"],
                cover=_cropped_cover(result),
                category=category,
                year=result.get("year"),
            )
        except TonieError as error:
            raise HTTPException(400, str(error)) from error
        remember(settings.data_dir, model_id, result.get("seconds"))
        remember_made(settings.data_dir, result.get("album", ""), model_id)
        return RedirectResponse("/tonies", 303)

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
            raise HTTPException(404, "teddyCloud does not know that tag.")
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
            raise HTTPException(400, str(error)) from error
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
            raise HTTPException(404, "That tag has no model to edit.")

        picture = None
        suffix = "jpg"
        if cover is not None and _is_image_name(cover.filename):
            picture = cover.file.read()
            suffix = Path(cover.filename).suffix.lstrip(".") or "jpg"

        try:
            update_model(connection, tag.model, series=series, episode=episode,
                         category=category, cover=picture, cover_name=suffix)
        except TonieError as error:
            raise HTTPException(400, str(error)) from error
        return RedirectResponse("/tonies", 303)

    @app.post("/tonies/{ruid}/delete")
    def delete_tonie(ruid: str):
        """Drop the model. The tag keeps its audio and goes on playing."""
        connection = client()
        tag = next((candidate for candidate in list_tags(connection)
                    if candidate.ruid == ruid), None)
        if tag is None or not tag.model:
            raise HTTPException(404, "That tag has no model to remove.")
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
            raise HTTPException(400, str(error)) from error
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
            raise HTTPException(400, f"Dieser Feed lässt sich nicht lesen: {error}") from error
        if not parsed.episodes:
            raise HTTPException(400, "Der Feed enthält keine Folge mit Audio.")
        if not any(tag.ruid == ruid for tag in list_tags(client())):
            raise HTTPException(400, "Die Box kennt diesen Sticker nicht.")

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
            raise HTTPException(400, str(error)) from error

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
            raise HTTPException(400, "Not a restore.")
        parked = _restore_dir(settings) / f"{token}.zip"
        if not parked.is_file():
            raise HTTPException(404, "That restore is gone; upload the backup again.")
        try:
            backup = read_backup(parked.read_bytes())
            steps = apply_restore(client(), backup, settings.data_dir)
        except BackupError as error:
            raise HTTPException(400, str(error)) from error
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
            raise HTTPException(400, "Not an image name.")
        try:
            data = client().fetch(f"/custom_img/{name}")
        except TeddyCloudError as error:
            raise HTTPException(502, str(error)) from error

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
            raise HTTPException(404, "No cover image here.")
        rendered = crop(candidates[index], zoom, x, y, max(32, min(size, 800)))
        if rendered is None:
            raise HTTPException(404, "That cover cannot be read.")
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
            raise HTTPException(400, str(error)) from error
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
            raise HTTPException(400, str(error)) from error
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
            "backs": _back_preview(models, playing_times(settings.data_dir)),
            "back_fade": BACK_FADE,
            "diameter_mm": DIAMETER_MM,
            "shift": read_shift(settings.data_dir),
            "expected": expected_first_centre(),
            "calibrated": bool(calibrated),
        })

    @app.get("/sheet/calibration")
    def calibration_sheet():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"calibration-{uuid.uuid4().hex[:8]}.pdf"
        # Printed with the current correction, so measuring it shows what is
        # still off rather than what was off before.
        build_calibration(target, read_shift(settings.data_dir))
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
            raise HTTPException(400, "Bitte zwei Zahlen in Millimetern eintragen.") from error
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
    def overview():
        entries = _overview_entries(client(), playing_times(settings.data_dir))
        if not entries:
            raise HTTPException(400, "No tonie with a model to list yet.")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"overview-{uuid.uuid4().hex[:8]}.pdf"
        build_overview(entries, target)
        return FileResponse(
            target,
            media_type="application/pdf",
            filename="tonies-uebersicht.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/sheet")
    def sheet(
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
                description = _back_of(described[model], times.get(model))
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
            raise HTTPException(400, "Nothing selected to print.")

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
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        return JSONResponse({
            "state": job.state.value,
            "step": job.step,
            "done": job.done_count,
            "total": job.total,
            "percent": job.percent,
            "error": job.error,
            "target": job.label,
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


def _overview_entries(client: TeddyCloudClient, times: Dict[str, int]) -> List[Entry]:
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
        back = _back_of(info, times.get(info["model"]))
        entries.append(Entry(image=image, title=back.title, series=back.series,
                             facts=list(back.notes)))
    return sorted(entries, key=lambda entry: (entry.series.casefold(), entry.title.casefold()))


def _back_of(model: Dict, seconds: Optional[int] = None) -> Back:
    """What the text side of one model says.

    The episode names the album and the series names who made it, which is the
    order they are looked for in: a shelf of tonies is searched by album.
    Facts the model does not carry are left out rather than guessed at, so a
    tonie recorded before the app stored them simply says less.
    """
    tracks = model.get("tracks") or []
    facts = [
        str(model.get("year") or ""),
        f"{len(tracks)} Tracks" if tracks else "",
        f"{minutes(seconds)} Minuten" if minutes(seconds) else "",
    ]
    notes = [" - ".join(fact for fact in facts if fact), model.get("model", "")]
    return Back(
        title=model.get("episodes") or model.get("series") or model.get("model", ""),
        series=model.get("series", ""),
        notes=[note for note in notes if note],
        tracks=tracks,
    )


def _back_preview(models: Sequence[Dict], times: Dict[str, int]
                  ) -> Dict[str, Dict[str, List[Dict]]]:
    """The laid-out text side of every model, for the page to draw.

    The lines come from the same call the sheet makes, so what the preview
    shows is what gets printed; only the font differs.
    """
    preview = {}
    for model in models:
        back = _back_of(model, times.get(model["model"]))
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
        raise HTTPException(404, "No such job.")
    if job.state is not JobState.DONE:
        raise HTTPException(409, f"That encode is {job.state.value}, so there is no taf yet.")
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
    raise HTTPException(404, "That album is not there any more.")


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


def _filesize(value: int) -> str:
    """A size as people read it, with a decimal comma: "1,2 MB", "340 KB"."""
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB".replace(".", ",")
    if value >= 1024:
        return f"{round(value / 1024)} KB"
    return f"{value} Byte"


def _when(value: Optional[datetime]) -> str:
    """A moment in the box's own time zone, which is the container's."""
    if value is None:
        return "-"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _is_audio_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _is_image_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
