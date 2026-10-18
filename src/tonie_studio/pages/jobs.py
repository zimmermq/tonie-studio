"""Watching encodes run, and giving a finished one its sticker by hand."""

from __future__ import annotations

import logging
from typing import Dict, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..i18n import Translator
from ..jobs import Job, JobRunner, JobState
from ..models import CATEGORIES, DEFAULT_CATEGORY, find_model_by_audio
from ..publishing import Outcome, job_states, publish_outcome
from ..tags import TonieError, list_tags
from .common import Context, Refused, refusing, translator


log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    box = ctx.box
    templates = ctx.templates
    jobs = ctx.jobs
    client = ctx.client
    groups = ctx.groups

    @app.get("/parts/{group}", response_class=HTMLResponse)
    def parts_page(request: Request, group: str):
        found = groups.get(group)
        if found is None:
            raise Refused(404, "error.parts_gone")
        batch = found.batch
        by_hand = bool(found.placing and found.placing.by_hand)
        members = [job for job in map(jobs.get, found.jobs) if job]
        made_models = [job.result.model for job in members
                       if isinstance(job.result, Outcome) and job.result.model]
        return templates.TemplateResponse(request, "parts.html", {
            "jobs": members,
            "batch": batch,
            "finished": all(job.state in (JobState.DONE, JobState.FAILED) for job in members),
            # The last step of the walkthrough: print exactly what was made.
            "print_href": "/sheet?" + urlencode([("model", model) for model in made_models])
            if made_models else "",
            "title_key": "batch.progress_title" if batch else "parts.title",
            "heading_key": "batch.progress_title" if batch else "parts.heading",
            "intro_key": ("batch.progress_intro_place" if by_hand
                          else "batch.progress_intro" if batch else "parts.intro"),
            "placing": by_hand,
            "group": group,
            "next_episode": (found.placing.next_episode(job_states(jobs.get)[1])
                             if found.placing else ""),
        })

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise Refused(404, "error.no_job")
        # A podcast refresh puts its taf on the sticker itself; only an album
        # encode still has to be given to one.
        podcast = job.kind == "podcast"
        published = isinstance(job.result, Outcome) and bool(job.result.model)
        if podcast:
            done_key, next_href, next_key = "job.podcast_done", "/podcasts", "job.to_podcasts"
        elif published:
            done_key, next_href, next_key = "job.published", "/tonies", "job.to_tonies"
        else:
            done_key, next_href, next_key = "job.taf_done", f"/job/{job.id}/assign", "job.to_assign"
        return templates.TemplateResponse(request, "job.html", {
            "job": job, "done_key": done_key, "next_href": next_href, "next_key": next_key,
            # How the page ends should the encode go on its sticker by itself.
            "assigned_ending": (None if podcast or published else
                                {"doneKey": "job.published", "href": "/tonies",
                                 "nextKey": "job.to_tonies"}),
            "error": _error_text(job, translator(request)),
        })

    @app.get("/job/{job_id}/assign", response_class=HTMLResponse)
    def assign_page(request: Request, job_id: str, new: str = ""):
        outcome = _finished_outcome(jobs, job_id)
        connection = client()
        tags = list_tags(connection)
        encoded = outcome.encoded
        # Two models holding the same audio_id and hash describe the same
        # recording, and they would fight each other for teddyCloud's automatic
        # matching. Worth saying before a second one is made.
        duplicate = find_model_by_audio(connection, encoded.audio_id, encoded.sha1)
        # `new` comes back from the page after the box read a tag teddyCloud
        # did not know; it is only honoured once that tag is really in the
        # index, so a stale link cannot preselect something that is not there.
        preselect = new if any(tag.ruid == new for tag in tags) else ""
        # A free sticker lying on the box is the one being held up to be
        # given this taf; choosing it for the reader saves matching ids.
        state = box.state
        if not preselect and state.known and state.on_box and any(
                tag.ruid == state.ruid and not tag.assigned for tag in tags):
            preselect = state.ruid
        return templates.TemplateResponse(request, "assign.html", {
            "job_id": job_id,
            "result": outcome,
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
        outcome = _finished_outcome(jobs, job_id)
        with refusing(TonieError):
            publish_outcome(client(), settings.data_dir, ruid, outcome, series, episode, category)
        return RedirectResponse("/tonies", 303)

    @app.get("/api/job/{job_id}")
    def job_status(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise Refused(404, "error.no_job")
        return JSONResponse(_job_json(job, translator(request)))

    @app.get("/api/group/{group}")
    def group_status(request: Request, group: str):
        """Every job of a group in one answer, for the progress page."""
        found = groups.get(group)
        if found is None:
            raise Refused(404, "error.parts_gone")
        translate = translator(request)
        members = [jobs.get(job_id) for job_id in found.jobs]
        return JSONResponse({"jobs": [_job_json(job, translate) for job in members if job]})


# --- helpers --------------------------------------------------------------


def _finished_outcome(jobs: JobRunner, job_id: str) -> Outcome:
    """The outcome of a finished encode, or a refusal explaining which it is not."""
    job = jobs.get(job_id)
    if job is None:
        raise Refused(404, "error.no_job")
    if job.state is not JobState.DONE or not isinstance(job.result, Outcome):
        raise Refused(409, "error.no_taf_yet")
    return job.result


def _error_text(job: Job, translate: Translator) -> Optional[str]:
    """Why a job failed, in the reader's language when the app described it."""
    return job.problem.say(translate) if job.problem else job.error


def _job_json(job: Job, translate: Translator) -> Dict:
    """One job as the pages watching it read it."""
    outcome = job.result if isinstance(job.result, Outcome) else None
    return {
        "id": job.id,
        "state": job.state.value,
        "step": job.step,
        "done": job.done_count,
        "total": job.total,
        "percent": job.percent,
        "error": _error_text(job, translate),
        "target": job.label,
        "assigned": bool(outcome and outcome.model),
        "model": (outcome and outcome.model) or None,
        # Why a batch encode did not get its sticker, for the reader.
        "note": translate(outcome.assign_error) if outcome and outcome.assign_error else "",
    }
