"""Several albums at once, and the stickers they are put on."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..album import Album, read_album
from ..albumnotes import match_by_tracks
from ..batch import AUTO, PLACE, Placing, Slot, allocate, episode_order
from ..encode import EncodeError, existing_taf, library_path, taf_exists
from ..jobs import JobState, new_id
from ..library import played, same, source_of
from ..parts import fewest_parts, part_name, split
from ..settings import Settings
from ..teddycloud import TeddyCloudClient, TeddyCloudError
from ..thumbnails import DEFAULT_SIZE
from ..tonie import TonieError, list_tags
from .common import (Catalogue, Context, Refused, _encode_job, _free_tags, _publish_result,
                     translator)

log = logging.getLogger(__name__)

# Remembers how the last batch handed out its stickers.
HOW_COOKIE = "stickers"


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    catalogue = ctx.catalogue
    templates = ctx.templates
    jobs = ctx.jobs
    client = ctx.client
    _albums_with_tonies = ctx.albums_with_tonies
    groups = ctx.groups
    batches = ctx.batches
    placings = ctx.placings

    @app.get("/batch", response_class=HTMLResponse)
    def batch_page(request: Request, ids: List[str] = Query(default=[])):
        """Several albums at once, each with the sticker it will get."""
        if not ids:
            raise Refused(400, "error.no_album")
        connection = client()
        models = connection.custom_tonies()
        rows = _plan_batch(connection, settings, catalogue, ids, models=models,
                           tonies=_albums_with_tonies(catalogue.albums(), models))
        # Whoever always hands out stickers the same way should not have to
        # choose it again each time; a newcomer starts with placing.
        how = request.cookies.get(HOW_COOKIE)
        return templates.TemplateResponse(request, "batch.html", {
            "rows": rows,
            "how": how if how in (AUTO, PLACE) else PLACE,
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
        models = connection.custom_tonies()
        rows = _plan_batch(connection, settings, catalogue, ids, dict(zip(ids, series, strict=False)),
                           dict(zip(ids, episodes, strict=False)), models=models,
                           tonies=_albums_with_tonies(catalogue.albums(), models))
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
                    replace=False, reuse=row.reusable,
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
        response = RedirectResponse(f"/parts/{group}", 303)
        response.set_cookie(HOW_COOKIE, PLACE if placing_wanted else AUTO,
                            max_age=365 * 24 * 3600, samesite="lax")
        return response


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


# --- helpers --------------------------------------------------------------


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
    # Its tafs are there, fit the album and no tag plays them: what an
    # interrupted batch leaves behind. Such a row only needs its stickers.
    reusable: bool = False
    # The model already made from this album, if any.
    tonie: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        """Left out: its tonie exists, or its taf is there and cannot be reused."""
        return bool(self.tonie) or (self.occupied and not self.reusable)


def _plan_batch(connection: TeddyCloudClient, settings: Settings, catalogue: Catalogue,
                ids: Sequence[str],
                series: Optional[Dict[str, str]] = None, episodes: Optional[Dict[str, str]] = None,
                tonies: Optional[Dict[str, str]] = None,
                models: Sequence[Dict] = ()) -> List[BatchRow]:
    """Each album with its default tracks, its taf paths and its stickers.

    Every album takes what its own page would offer untouched: the tracks
    that are not karaoke takes, in the album's order, the first cover, and as
    few parts as teddyCloud allows.

    An album a tonie was already made from is left out: known from the
    records (``tonies``, by album id) or recognised by its track list among
    ``models``, since the name typed back then may differ from the
    suggestion. A second copy is a decision for the album's own page. An
    album whose tafs exist but play on no tag is not encoded again, only
    given its stickers - if every part is there with as many chapters as the
    part has tracks; anything else there is left alone.

    ``series`` and ``episodes`` hold what the page sent, by album id; an
    album missing from them gets the suggestion. A series sent empty stays
    empty, since that is a choice the album page allows too.
    """
    series, episodes = series or {}, episodes or {}
    playing = played(tag.source for tag in list_tags(connection))
    rows = []
    for identifier in dict.fromkeys(ids):
        album = read_album(catalogue.resolve(identifier))
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
        groups = split([track.duration for track, _name in chosen], parts)
        occupied = any(taf_exists(connection, target) for target in targets)
        in_use = any(same(source_of(target)) in playing for target in targets)
        rows.append(BatchRow(
            identifier=identifier, album=album, series=row_series, episode=episode,
            chosen=chosen, groups=groups, targets=targets, occupied=occupied, in_use=in_use,
            reusable=occupied and not in_use and _tafs_fit(connection, targets, groups),
            tonie=((tonies or {}).get(identifier)
                   or match_by_tracks([track.title for track in album.tracks], models) or ""),
        ))
    # Grouped by series and in episode order within one, which is also the
    # order the stickers are handed out in.
    rows.sort(key=lambda row: (row.series.casefold(), episode_order(row.episode)))

    waiting = [row for row in rows if not row.skipped]
    for row, tags in zip(waiting, allocate([len(row.targets) for row in waiting],
                                           _free_tags(connection)), strict=True):
        row.tags = tags
    return rows


def _tafs_fit(connection: TeddyCloudClient, targets: Sequence[str],
              groups: Sequence[Sequence[int]]) -> bool:
    """Whether every part's taf is there with exactly that part's tracks as chapters."""
    for target, group in zip(targets, groups, strict=True):
        found = existing_taf(connection, target)
        if found is None or found.tracks != len(group):
            return False
    return True


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
