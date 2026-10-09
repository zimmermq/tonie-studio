"""Several albums at once, and the stickers they are put on."""

from __future__ import annotations

import logging
from typing import Callable, List

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..album import read_album
from ..albumnotes import albums_with_tonies
from ..batch import AUTO, PLACE, Placing, Slot
from ..i18n import Problem
from ..jobs import JobState, new_id
from ..models import list_models
from ..planning import plan_batch
from ..publishing import (PLACE_RESULTS, Details, Outcome, encode_job, free_tags, place,
                          put_on_sticker)
from ..settings import Settings
from ..teddycloud import TeddyCloudClient
from ..thumbnails import DEFAULT_SIZE
from .common import Context, Refused, refused, translator

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
    groups = ctx.groups

    def _plan(connection, ids, series=None, episodes=None, models=()):
        """The batch for these album ids, or the reason it cannot be made."""
        albums = [(identifier, read_album(catalogue.resolve(identifier)))
                  for identifier in dict.fromkeys(ids)]
        try:
            return plan_batch(connection, albums, settings.archive_dir, settings.upload_dir,
                              series, episodes, models=models,
                              tonies=albums_with_tonies(settings.data_dir, catalogue.albums(),
                                                        models))
        except Problem as problem:
            raise refused(problem) from problem

    @app.get("/batch", response_class=HTMLResponse)
    def batch_page(request: Request, ids: List[str] = Query(default=[])):
        """Several albums at once, each with the sticker it will get."""
        if not ids:
            raise Refused(400, "error.no_album")
        connection = client()
        models = list_models(connection)
        rows = _plan(connection, ids, models=models)
        # Whoever always hands out stickers the same way should not have to
        # choose it again each time; a newcomer starts with placing.
        how = request.cookies.get(HOW_COOKIE)
        return templates.TemplateResponse(request, "batch.html", {
            "rows": rows,
            "how": how if how in (AUTO, PLACE) else PLACE,
            "free": len(free_tags(connection)),
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
        models = list_models(connection)
        rows = _plan(connection, ids, dict(zip(ids, series, strict=False)),
                     dict(zip(ids, episodes, strict=False)), models=models)
        job_ids = []
        # Both ways hand out stickers the same way: automatically, each slot
        # has its sticker reserved from the start; by hand, it gets one when
        # a sticker is placed. Made before any encode is queued, because one
        # that finishes at once must already find the slot it settles.
        placing = Placing([], by_hand=placing_wanted)
        for row in rows:
            if row.skipped:
                continue
            tags = [None] * len(row.parts) if placing_wanted or not row.tags else row.tags
            for part, ruid in zip(row.parts, tags, strict=True):
                details = Details(
                    series=row.series, episode=part.episode,
                    cover=str(row.album.cover_candidates[0]) if row.album.cover_candidates else None,
                    year=row.album.year, album=row.identifier)
                job_id = new_id()
                placing.add(Slot(job_id=job_id, episode=details.episode, ruid=ruid))
                job_ids.append(jobs.submit("encode", part.target, job_id=job_id, work=encode_job(
                    connection, part.tracks, part.target, details,
                    reuse=row.reusable,
                    after=_on_sticker_when_known(connection, settings, placing, job_id))))
        if not job_ids:
            raise Refused(400, "error.batch_nothing_new")
        group = groups.add(job_ids, batch=True, placing=placing)
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
        found = groups.get(group)
        placing = found.placing if found else None
        if placing is None:
            raise Refused(404, "error.parts_gone")
        translate = translator(request)
        status, slot = place(client(), settings.data_dir, placing, ruid, jobs.get)
        waiting = placing.waiting(lambda job_id: jobs.get(job_id) is None
                                  or jobs.get(job_id).state is JobState.FAILED)
        return JSONResponse({
            "status": status,
            "message": translate(PLACE_MESSAGES[status], episode=slot.episode if slot else ""),
            "then": (translate("place.next", episode=waiting.episode) if waiting
                     else translate("place.all_done")),
            "episode": slot.episode if slot else None,
            "job": slot.job_id if slot else None,
            "next": waiting.episode if waiting else None,
        })


# --- helpers --------------------------------------------------------------


# What the progress page says after a sticker was placed, by outcome.
PLACE_MESSAGES = {result: f"place.{result}" for result in PLACE_RESULTS}


def _on_sticker_when_known(client: TeddyCloudClient, settings: Settings, placing: Placing,
                           job_id: str) -> Callable[[Outcome], None]:
    """The step that puts a batch encode on its sticker once it is encoded.

    The sticker is the one reserved for it up front, or the one placed on the
    box meanwhile. Without one yet nothing happens here; a sticker placed
    later is put on by the request that reserves it.
    """
    def after(outcome: Outcome) -> None:
        ruid = placing.settle(job_id)
        if ruid is not None:
            put_on_sticker(client, settings.data_dir, ruid, outcome)
    return after
