"""The album list, uploads, one album's page, its encode and the assignment of the result."""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from ..album import Album, album_id, cover_candidates, read_album
from ..albumnotes import first_sightings, is_new
from ..batch import episode_order
from ..encode import EncodeError, library_path, taf_exists
from ..parts import MOST_CHAPTERS, fewest_parts, part_name, split
from ..thumbnails import DEFAULT_SIZE, crop, thumbnail
from ..tonie import CATEGORIES, DEFAULT_CATEGORY, TonieError, find_model_by_audio, list_tags
from .common import (UPLOAD, Context, Refused, _catalogue, _encode_job, _finished_result,
                     _forget_catalogue, _is_audio_name, _is_image_name, _next_to_place,
                     _publish_result, _resolve, translator)

log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    jobs = ctx.jobs
    client = ctx.client
    _albums_with_tonies = ctx.albums_with_tonies
    groups = ctx.groups
    batches = ctx.batches
    placings = ctx.placings

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
