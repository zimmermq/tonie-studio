"""The album list, uploads, one album's page and starting its encode."""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..album import Album, album_id, cover_candidates, read_album
from ..albumnotes import albums_with_tonies, first_sightings, is_new
from ..batch import episode_order
from ..encode import taf_exists
from ..i18n import Problem
from ..parts import MOST_CHAPTERS
from ..planning import chosen_tracks, plan_parts
from ..publishing import Crop, Details, encode_job
from ..thumbnails import DEFAULT_SIZE, crop, thumbnail
from .common import UPLOAD, Context, Refused, is_audio_name, is_image_name, refused

log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    catalogue = ctx.catalogue
    templates = ctx.templates
    jobs = ctx.jobs
    client = ctx.client
    groups = ctx.groups

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        albums = catalogue.albums()
        now = datetime.now(timezone.utc)
        sightings = first_sightings(settings.data_dir, [row[0] for row in albums], now)
        return templates.TemplateResponse(request, "index.html", {
            "albums": albums,
            "archive": [row for row in albums if row[2] == "archive"],
            "uploads": [row for row in albums if row[2] == "upload"],
            "jobs": jobs.all()[:10],
            "thumb_size": DEFAULT_SIZE,
            "new": {identifier for identifier, when in sightings.items() if is_new(when, now)},
            "done": albums_with_tonies(settings.data_dir, albums, ctx.models.get()),
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
        catalogue.forget()
        ids = [f"{UPLOAD}{album_id(settings.upload_dir, destination)}"
               for destination in sorted(destinations.values(),
                                         key=lambda path: episode_order(path.name))]
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
        return _album_page(request, identifier, read_album(catalogue.resolve(identifier)))


    @app.get("/album/{identifier}/cover")
    def cover(identifier: str, index: int = 0, size: int = 0):
        """The album's cover, full size or as a square preview.

        The picker asks for a size; sixteen full covers would be two megabytes
        of JPEG to draw pictures the size of a stamp.
        """
        candidates = cover_candidates(catalogue.resolve(identifier))
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
        album = read_album(catalogue.resolve(identifier))
        try:
            parts_planned = plan_parts(chosen_tracks(album, order, selected, titles),
                                       series, episode, parts)
        except Problem as problem:
            raise refused(problem) from problem
        targets = [part.target for part in parts_planned]

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
        cut = Crop(zoom=cover_zoom, x=cover_x, y=cover_y)

        job_ids = [
            jobs.submit("encode", part.target, encode_job(
                connection, part.tracks, part.target,
                Details(series=series, episode=part.episode, cover=cover, crop=cut,
                        year=album.year, album=identifier),
                replace=part.target in occupied))
            for part in parts_planned
        ]
        if len(job_ids) == 1:
            return RedirectResponse(f"/job/{job_ids[0]}", 303)
        group = uuid.uuid4().hex[:12]
        groups[group] = job_ids
        return RedirectResponse(f"/parts/{group}", 303)


    @app.get("/album/{identifier}/crop")
    def crop_preview(identifier: str, index: int = 0, zoom: float = 1.0,
                     x: float = 0.0, y: float = 0.0, size: int = 256):
        """The cover as the round sticker will cut it."""
        candidates = cover_candidates(catalogue.resolve(identifier))
        if index >= len(candidates):
            raise Refused(404, "error.no_cover")
        rendered = crop(candidates[index], zoom, x, y, max(32, min(size, 800)))
        if rendered is None:
            raise Refused(404, "error.cover_unreadable")
        return Response(rendered, media_type="image/jpeg")


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
        if not (is_audio_name(file.filename) or is_image_name(file.filename)):
            continue
        folder = str(PurePosixPath((file.filename or "").replace("\\", "/")).parent)
        folders.setdefault(folder, []).append(file)
    return {folder: members for folder, members in folders.items()
            if any(is_audio_name(member.filename) for member in members)}


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


def _suggest_target(album: Album) -> Dict[str, str]:
    """What to prefill the series and episode fields with."""
    return {"series": album.artist or "", "episode": album.title}


