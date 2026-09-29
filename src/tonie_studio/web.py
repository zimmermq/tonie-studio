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
import mimetypes
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .album import AUDIO_EXTENSIONS, Album, AlbumSummary, album_id, list_albums, read_album
from .encode import EncodeError, encode_files, library_path, remove_orphaned_staging
from .jobs import JobRunner, JobState
from .settings import Settings
from .sheet import PER_PAGE, Sticker, build_sheet
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import CATEGORIES, DEFAULT_CATEGORY, TonieError, list_tags, publish

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"

# Which root an album id belongs to.
ARCHIVE = "a"
UPLOAD = "u"

# Where teddyCloud serves the covers of custom models.
CUSTOM_IMG_PREFIX = "/custom_img/"


def create_app(
    settings: Settings,
    client_factory: Optional[Callable[[], TeddyCloudClient]] = None,
    runner: Optional[JobRunner] = None,
) -> FastAPI:
    """Build the app. The dependencies are injected so tests can replace them."""
    templates = Jinja2Templates(directory=str(TEMPLATES))
    jobs = runner or JobRunner()

    def client() -> TeddyCloudClient:
        if client_factory:
            return client_factory()
        return TeddyCloudClient(settings.teddycloud_url, overlay=settings.overlay)

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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Off the startup path on purpose. An unreachable teddyCloud answers
        # only after DNS timeouts and retries, and blocking here would hold the
        # readiness probe down for a service outage this app can survive.
        cleanup = threading.Thread(target=clear_abandoned_staging,
                                   name="staging-cleanup", daemon=True)
        cleanup.start()
        yield

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
        return templates.TemplateResponse(request, "index.html", {
            "albums": _catalogue(settings),
            "jobs": jobs.all()[:10],
        })

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

        return RedirectResponse(f"/album/{UPLOAD}{album_id(settings.upload_dir, folder)}", 303)

    @app.get("/album/{identifier}", response_class=HTMLResponse)
    def album_page(request: Request, identifier: str):
        album = read_album(_resolve(settings, identifier))
        return templates.TemplateResponse(request, "album.html", {
            "id": identifier,
            "album": album,
            "suggested_target": _suggest_target(album),
        })

    @app.get("/album/{identifier}/cover")
    def cover(identifier: str, index: int = 0):
        album = read_album(_resolve(settings, identifier))
        if index >= len(album.cover_candidates):
            raise HTTPException(404, "No cover image here.")
        return FileResponse(album.cover_candidates[index])

    @app.post("/album/{identifier}/encode")
    def start_encode(
        identifier: str,
        series: str = Form(""),
        episode: str = Form(...),
        order: str = Form(""),
        selected: List[int] = Form(default=[]),
        titles: List[str] = Form(default=[]),
    ):
        album = read_album(_resolve(settings, identifier))
        chosen = _chosen_tracks(album, order, selected, titles)
        if not chosen:
            raise HTTPException(400, "No track selected.")

        try:
            target = library_path(series, episode)
        except EncodeError as error:
            raise HTTPException(400, str(error)) from error

        paths = [track.path for track, _name in chosen]
        names = [name for _track, name in chosen]
        connection = client()

        cover = str(album.cover) if album.cover else None

        def work(progress):
            result = encode_files(connection, paths, target, progress=progress)
            # Carried along for the tag and model steps, which need the names a
            # person approved rather than the ones that were guessed.
            return {"encode": result, "series": series, "episode": episode,
                    "tracks": names, "cover": cover}

        return RedirectResponse(f"/job/{jobs.submit('encode', target, work)}", 303)

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        return templates.TemplateResponse(request, "job.html", {"job": job})

    @app.get("/job/{job_id}/assign", response_class=HTMLResponse)
    def assign_page(request: Request, job_id: str):
        result = _finished_result(jobs, job_id)
        return templates.TemplateResponse(request, "assign.html", {
            "job_id": job_id,
            "result": result,
            "tags": list_tags(client()),
            "categories": CATEGORIES,
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
        cover = result.get("cover")
        try:
            publish(
                client(),
                ruid,
                result["encode"],
                series=series,
                episode=episode,
                tracks=result["tracks"],
                cover=Path(cover) if cover else None,
                category=category,
            )
        except TonieError as error:
            raise HTTPException(400, str(error)) from error
        return RedirectResponse("/tonies", 303)

    @app.get("/tonies", response_class=HTMLResponse)
    def tonies(request: Request):
        return templates.TemplateResponse(request, "tonies.html", {
            "tags": list_tags(client()),
            "teddycloud_url": settings.teddycloud_url,
        })

    @app.get("/cover/custom/{name}")
    def custom_cover(name: str):
        """Pass a cover through from teddyCloud.

        The browser can reach this app but not teddyCloud's own address, which
        sits behind the gateway under a different name, so linking straight to
        the image would give a broken picture.
        """
        if "/" in name or ".." in name:
            raise HTTPException(400, "Not an image name.")
        try:
            data = client().fetch(f"/custom_img/{name}")
        except TeddyCloudError as error:
            raise HTTPException(502, str(error)) from error
        return Response(data, media_type=mimetypes.guess_type(name)[0] or "image/jpeg")

    @app.get("/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request):
        return templates.TemplateResponse(request, "sheet.html", {
            "models": _printable_models(client()),
            "per_page": PER_PAGE,
        })

    @app.post("/sheet")
    def sheet(
        models: List[str] = Form(default=[]),
        counts: List[int] = Form(default=[]),
        files: List[UploadFile] = Form(default=[]),
    ):
        connection = client()
        stickers = []

        for position, model in enumerate(models):
            count = counts[position] if position < len(counts) else 0
            if count < 1:
                continue
            picture = _model_picture(connection, model)
            if picture is None:
                continue
            stickers.append(Sticker(image=picture, count=count, label=model))

        for upload_file in files:
            if _is_image_name(upload_file.filename):
                stickers.append(Sticker(image=upload_file.file.read(),
                                        label=upload_file.filename or "upload"))

        if not stickers:
            raise HTTPException(400, "Nothing selected to print.")

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"sheet-{uuid.uuid4().hex[:8]}.pdf"
        skipped = build_sheet(stickers, target)
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
    printable = []
    for model in client.custom_tonies():
        picture = str(model.get("pic") or "")
        if not picture.startswith(CUSTOM_IMG_PREFIX):
            continue
        printable.append({
            "model": model.get("model", ""),
            "series": model.get("series", ""),
            "episodes": model.get("episodes", ""),
            "image": picture[len(CUSTOM_IMG_PREFIX):],
        })
    return printable


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


def _finished_result(jobs: JobRunner, job_id: str) -> Dict:
    """The result of a finished encode, or a refusal explaining which it is not."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    if job.state is not JobState.DONE:
        raise HTTPException(409, f"That encode is {job.state.value}, so there is no taf yet.")
    return job.result


def _catalogue(settings: Settings) -> List[Tuple[str, AlbumSummary, str]]:
    """Every album on offer, as (id, summary, where it came from)."""
    entries = []
    for prefix, root, origin in (
        (ARCHIVE, settings.archive_dir, "archive"),
        (UPLOAD, settings.upload_dir, "upload"),
    ):
        if not root.is_dir():
            continue
        for summary in list_albums(root):
            entries.append((f"{prefix}{summary.id}", summary, origin))
    return entries


def _resolve(settings: Settings, identifier: str) -> Path:
    """Find the folder an album id names, or refuse."""
    for candidate_id, summary, _origin in _catalogue(settings):
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


def _is_audio_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _is_image_name(name: Optional[str]) -> bool:
    return bool(name) and Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
