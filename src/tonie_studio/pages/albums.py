"""The album list, uploads, one album's page and starting its encode."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..album import Album, Track, album_id, cover_candidates, read_album
from ..albumnotes import albums_with_tonies, first_sightings, is_new
from ..catalogue import UPLOAD, Catalogue
from ..encode import taf_exists
from ..i18n import Problem
from ..library import same
from ..listing import Column, filters
from ..parts import MOST_CHAPTERS
from ..pictures import DEFAULT_SIZE, crop, thumbnail
from ..planning import chosen_tracks, expected_targets, ordered_tracks, plan_parts
from ..jobs import Groups, JobRunner
from ..publishing import Crop, Details, encode_job, job_states
from ..uploads import UploadError, spool
from .common import Context, Refused, album_or_404, refused, refusing, upload_form

log = logging.getLogger(__name__)

# Joins the occupied tafs in the value of the page's replace button. No library
# path holds a newline, so the list comes back apart unchanged.
REPLACE_SEPARATOR = "\n"

# From how many ticked albums the list warns that setting them up takes a
# while: the batch page reads every album's tags, about a second each over a
# network share.
SLOW_PICK = 20


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
        done = albums_with_tonies(settings.data_dir, albums, ctx.models.get())
        new = {identifier for identifier, when in sightings.items() if is_new(when, now)}
        in_library = _in_library(albums, done, ctx.library.get(),
                                 settings.archive_dir, settings.upload_dir)
        tags = {identifier: _album_tags(identifier, done, new, in_library)
                for identifier, _album, _origin in albums}
        counts, offered = filters(tags.values(), (NO_TONIE, NOT_IN_LIBRARY, NEW))
        return templates.TemplateResponse(request, "index.html", {
            "albums": albums,
            "archive": [row for row in albums if row[2] == "archive"],
            "uploads": [row for row in albums if row[2] == "upload"],
            "jobs": jobs.all()[:10],
            "placing": _waiting_for_stickers(groups, jobs),
            "thumb_size": DEFAULT_SIZE,
            "slow_pick": SLOW_PICK,
            "new": new,
            "done": done,
            "in_library": in_library,
            "tags": tags,
            "counts": counts,
            "filters": offered,
            "columns": ALBUM_COLUMNS,
        })


    @app.post("/upload")
    def upload(form: FormData = Depends(upload_form)):
        """Spool a browser upload, then hand its albums to the batch page together."""
        files = [part for part in form.getlist("files") if isinstance(part, StarletteUploadFile)]
        with refusing(UploadError, status=507):
            albums = spool(settings.upload_dir, [(file.filename, file.file) for file in files])
        if not albums:
            raise Refused(400, "error.no_audio")

        # The new folder has to show up at once, not in ten seconds.
        catalogue.forget()
        ids = [f"{UPLOAD}{album_id(settings.upload_dir, folder)}" for folder in albums]
        return RedirectResponse(f"/batch?{urlencode([('ids', identifier) for identifier in ids])}",
                                303)


    def _album_page(request: Request, identifier: str, album: Album, form: AlbumForm,
                    occupied: Sequence[str] = ()):
        return templates.TemplateResponse(request, "album.html", {
            "id": identifier,
            "album": album,
            "form": form,
            "conflict": ", ".join(occupied),
            "replace_value": REPLACE_SEPARATOR.join(occupied),
            "most_chapters": MOST_CHAPTERS,
        })


    @app.get("/album/{identifier}", response_class=HTMLResponse)
    def album_page(request: Request, identifier: str):
        album = read_album(album_or_404(catalogue, identifier))
        return _album_page(request, identifier, album, AlbumForm.defaults(album))


    @app.get("/album/{identifier}/cover")
    def cover(identifier: str, index: int = 0, size: int = 0):
        """The album's cover, full size or as a square preview.

        The picker asks for a size; sixteen full covers would be two megabytes
        of JPEG to draw pictures the size of a stamp.
        """
        path = _cover_or_404(catalogue, identifier, index)
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
        album = read_album(album_or_404(catalogue, identifier))
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
        # Replacing is only ever the answer to the page that named these very
        # tafs. A form changed since then names others and is asked again.
        if occupied and replace.split(REPLACE_SEPARATOR) != occupied:
            entered = AlbumForm.submitted(album, series, episode, order, selected, titles,
                                          parts, cover_index, cover_zoom, cover_x, cover_y)
            return _album_page(request, identifier, album, entered, occupied)

        # The cover is decided here, on the page that shows the album, and
        # carried to the model step as the picture it will actually be: a
        # square already cropped the way the round sticker will cut it.
        chosen_cover = (album.cover_candidates[cover_index]
                        if 0 <= cover_index < len(album.cover_candidates) else None)
        cover = str(chosen_cover) if chosen_cover else None
        cut = Crop(zoom=cover_zoom, x=cover_x, y=cover_y)

        job_ids = [
            jobs.submit("encode", part.target, encode_job(
                client, part.tracks, part.target,
                Details(series=series, episode=part.episode, cover=cover, crop=cut,
                        year=album.year, album=identifier),
                replace=part.target in occupied))
            for part in parts_planned
        ]
        if len(job_ids) == 1:
            return RedirectResponse(f"/job/{job_ids[0]}", 303)
        group = groups.add(job_ids)
        return RedirectResponse(f"/parts/{group}", 303)


    @app.get("/album/{identifier}/crop")
    def crop_preview(identifier: str, index: int = 0, zoom: float = 1.0,
                     x: float = 0.0, y: float = 0.0, size: int = 256):
        """The cover as the round sticker will cut it."""
        rendered = crop(_cover_or_404(catalogue, identifier, index), max(32, min(size, 800)),
                        zoom, x, y)
        if rendered is None:
            raise Refused(404, "error.cover_unreadable")
        return Response(rendered, media_type="image/jpeg")


# --- helpers --------------------------------------------------------------


def _waiting_for_stickers(groups: Groups, jobs: JobRunner) -> List[Tuple[str, str]]:
    """Batches waiting for stickers to be laid on the box, as (group id, next episode).

    Placing works only while the batch's page is open; the album list leads
    back to it.
    """
    failed, played_on = job_states(jobs.get)
    waiting = []
    for group_id, group in groups.all():
        if group.placing and group.placing.by_hand:
            slot = group.placing.waiting(failed, played_on)
            if slot is not None:
                waiting.append((group_id, slot.episode))
    return waiting


def _cover_or_404(catalogue: Catalogue, identifier: str, index: int) -> Path:
    """The ``index``-th picture of the album that could be its cover, or a refusal."""
    candidates = cover_candidates(album_or_404(catalogue, identifier))
    if not 0 <= index < len(candidates):
        raise Refused(404, "error.no_cover")
    return candidates[index]


def _in_library(albums, done: Dict[str, str], library: Optional[Set[str]],
                archive_dir: Path, upload_dir: Path) -> Optional[Set[str]]:
    """Ids of the albums that are encoded, or None while the library is not known.

    An album with a tonie counts as encoded; any other counts when a taf sits
    where the batch page would put it.
    """
    if library is None:
        return None
    return {identifier for identifier, summary, _origin in albums
            if identifier in done
            or any(same(target) in library
                   for target in expected_targets(summary, archive_dir, upload_dir))}


@dataclass
class TrackRow:
    """One row of the album page's track list."""

    track: Track
    title: str
    checked: bool


@dataclass
class AlbumForm:
    """What the album page's form shows: the album's suggestion, or what was sent."""

    series: str
    episode: str
    rows: List[TrackRow]
    parts: int = 1
    cover_index: int = 0
    cover_zoom: float = 1.0
    cover_x: float = 0.0
    cover_y: float = 0.0

    @property
    def order(self) -> str:
        return ",".join(str(row.track.number) for row in self.rows)

    @classmethod
    def defaults(cls, album: Album) -> "AlbumForm":
        """Artist as series, title as episode, every track but the karaoke takes."""
        return cls(series=album.artist or "", episode=album.title,
                   rows=[TrackRow(track, track.title, not track.extra) for track in album.tracks])

    @classmethod
    def submitted(cls, album: Album, series: str, episode: str, order: str,
                  selected: Sequence[int], titles: Sequence[str], parts: int,
                  cover_index: int, cover_zoom: float, cover_x: float,
                  cover_y: float) -> "AlbumForm":
        """The form as it was sent, read the way ``chosen_tracks`` reads it.

        A track the order leaves out keeps its place at the end, unticked, so
        no row of the album disappears from the page.
        """
        keep = set(selected)
        ordered = ordered_tracks(album, order)
        rows = [TrackRow(track, (titles[position].strip() if position < len(titles) else "")
                         or track.title, track.number in keep)
                for position, track in ordered]
        seen = {track.number for _position, track in ordered}
        rows += [TrackRow(track, track.title, False)
                 for track in album.tracks if track.number not in seen]
        return cls(series=series, episode=episode, rows=rows, parts=parts,
                   cover_index=cover_index, cover_zoom=cover_zoom,
                   cover_x=cover_x, cover_y=cover_y)




# The album list's orders: by title, by artist, newest year first.
ALBUM_COLUMNS = (
    Column("name", "albums.col_title"),
    Column("artist", "albums.col_artist"),
    Column("year", "albums.col_year", number=True, desc=True),
)

# What the album list's filters match. Each is what is still to do: an album
# without a tonie, one not encoded yet, one that arrived lately.
NO_TONIE, NOT_IN_LIBRARY, NEW = "notonie", "notinlibrary", "new"


def _album_tags(identifier: str, done: Dict, new: Set[str],
                in_library: Optional[Set[str]]) -> List[str]:
    """The filters an album answers to. Without a copy of the library nothing
    is claimed to be missing from it."""
    tags = [] if identifier in done else [NO_TONIE]
    if in_library is not None and identifier not in in_library:
        tags.append(NOT_IN_LIBRARY)
    if identifier in new:
        tags.append(NEW)
    return tags
