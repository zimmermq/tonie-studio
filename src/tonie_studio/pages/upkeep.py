"""The checkup that finds what is broken, and the backup that keeps what cannot be rebuilt."""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import backup as backup_module
from ..albumnotes import made
from ..backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                      drop_parked, park, plan_restore, read_backup, read_parked)
from ..checkup import BULK_FIXES, TAG_KINDS, apply_fix, examine
from ..encode import EncodeError, list_tafs
from ..jobs import JobState
from ..library import is_library, played, same
from ..podcast import SERIES as PODCAST_SERIES
from ..publishing import Outcome
from ..tags import TonieError, list_tags
from ..teddycloud import TeddyCloudClient, TeddyCloudError
from .common import Context, Refused, refusing, translator

log = logging.getLogger(__name__)

# How many restore results stay on show.
KEEP_RESTORED = 5


# The job kinds of a repair run for all stickers at once and of a restore.
BULK_KIND = "checkup"
RESTORE_KIND = "restore"


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    client = ctx.client

    def work_in_progress() -> Dict:
        """What the checkup must not call left over: tafs jobs are making or made for a sticker.

        Encode jobs are labelled with their target. One queued or running is
        writing it; one done whose taf is on no sticker yet waits for a person
        to pick one. A failed encode and one already on its sticker spare
        nothing: what is left of the first is exactly what the checkup is
        for, and the second's taf is spared by the sticker playing it.

        A podcast refresh builds its taf under a name only known once it
        runs, so while one is under way the whole podcast folder is spared.
        """
        busy = {job.label for job in ctx.jobs.all() if job.kind == "encode" and (
            job.state in (JobState.QUEUED, JobState.RUNNING)
            or (job.state is JobState.DONE and isinstance(job.result, Outcome)
                and not job.result.model))}
        folders = {f"/{PODCAST_SERIES}"} if ctx.podcasts.refreshing else set()
        return {"busy": busy, "busy_folders": folders}

    @app.get("/checkup", response_class=HTMLResponse)
    def checkup_page(request: Request, done: str = "", stale: str = "", running: str = ""):
        failed = None
        if running:
            # A run for all stickers, watched until it ends. While it runs
            # the page shows only how far it got: checking everything again
            # every few seconds would compete with the run for teddyCloud.
            job = ctx.prints.get(running)
            if job is None or job.kind != BULK_KIND:
                return RedirectResponse("/checkup", 303)
            if job.state is JobState.DONE:
                return RedirectResponse(f"/checkup?done={job.label}", 303)
            if job.state is not JobState.FAILED:
                return templates.TemplateResponse(request, "checkup.html", {
                    "running": job, "findings": [], "album_of": {}, "tag_kinds": TAG_KINDS,
                })
            failed = job.problem.say(translator(request)) if job.problem else job.error
        findings = examine(client(), **work_in_progress())
        # A silent tonie made from an album here can be encoded again from it.
        album_of = {model: album for album, models in made(settings.data_dir).items()
                    for model in models}
        return templates.TemplateResponse(request, "checkup.html", {
            "findings": findings,
            "album_of": album_of,
            "tag_kinds": TAG_KINDS,
            "done": done,
            "stale": stale,
            "failed": failed,
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

        with refusing(TonieError, EncodeError):
            apply_fix(connection, settings.data_dir, fix, target)
        log.info("checkup: %s applied to %s", fix, target)
        return RedirectResponse(f"/checkup?done={fix}", 303)


    @app.post("/checkup/fix_all")
    def checkup_fix_all(fix: str = Form(...)):
        """Apply one harmless repair to every sticker the checks find it for, now.

        Only a repair that flips a flag is offered for all at once: switching
        the cloud off for hundreds of imported tonies one click at a time helps
        nobody. Deleting and emptying stay one at a time, each shown first.
        """
        if fix not in BULK_FIXES:
            raise Refused(400, "checkup.not_for_all")
        # Hundreds of stickers take longer than a proxy in front of the app
        # waits for an answer, so the run goes on behind the page, which
        # watches it. One run at a time: a second click joins the first.
        under_way = next((job for job in ctx.prints.all() if job.kind == BULK_KIND
                          and job.state in (JobState.QUEUED, JobState.RUNNING)), None)
        if under_way is None:
            job_id = ctx.prints.submit(BULK_KIND, fix, lambda progress: fix_everywhere(fix, progress))
        else:
            job_id = under_way.id
        return RedirectResponse(f"/checkup?running={job_id}", 303)

    def fix_everywhere(fix: str, progress) -> int:
        """Apply ``fix`` wherever the checks find it due; returns how many."""
        connection = client()
        targets = [finding.target for finding in examine(connection, **work_in_progress())
                   if finding.fix == fix]
        for number, target in enumerate(targets):
            progress(fix, number, len(targets))
            apply_fix(connection, settings.data_dir, fix, target)
        log.info("checkup: %s applied to %d sticker(s)", fix, len(targets))
        return len(targets)


    @app.get("/backup", response_class=HTMLResponse)
    def backup_page(request: Request):
        parts = describe_backup(client(), settings.data_dir)
        total = sum(part.size for part in parts)
        return templates.TemplateResponse(request, "backup.html", {
            "parts": parts,
            "total": total,
            # A restore refuses a file above this; better said before saving one.
            "too_large": total > backup_module.LARGEST_BACKUP,
            "largest": backup_module.LARGEST_BACKUP,
        })


    @app.get("/backup/download")
    def backup_download():
        data = create_backup(client(), settings.data_dir)
        name = f"tonie-studio-{datetime.now().strftime('%Y-%m-%d')}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


    @app.post("/restore", response_class=HTMLResponse)
    def restore_preview(request: Request, file: UploadFile = File(...)):
        """Show what a restore would do. Nothing is written until confirmed."""
        # Read at most one byte past the limit: enough to know it is too big
        # without holding all of it.
        data = file.file.read(LARGEST_BACKUP + 1)
        with refusing(BackupError):
            backup = read_backup(data)

        token = park(settings.data_dir, data)
        return templates.TemplateResponse(request, "restore.html", {
            "steps": plan_restore(client(), backup, settings.data_dir),
            "manifest": backup.manifest,
            "token": token,
            "done": False,
        })


    # What the last restores did, by token, for their result page. Kept in
    # memory: the page is looked at once, right after, and a few will do.
    restored: "OrderedDict[str, Tuple[list, dict]]" = OrderedDict()

    def restore_run(token: str):
        """The restore run of ``token`` the app still knows, newest first."""
        return next((job for job in ctx.prints.all()
                     if job.kind == RESTORE_KIND and job.label == token), None)

    @app.post("/restore/{token}")
    def restore_apply(token: str):
        """Start the restore and watch it on a page of its own.

        Its own address, so reloading it shows the run or the result again
        rather than sending the confirmation a second time. The restore runs
        behind that page: hundreds of stickers take longer than a proxy in
        front of the app waits for an answer.
        """
        if not token.isalnum():
            raise Refused(400, "error.not_a_restore")
        run = restore_run(token)
        if run is not None and run.state in (JobState.QUEUED, JobState.RUNNING):
            return RedirectResponse(f"/restore/{token}/done", 303)
        data = read_parked(settings.data_dir, token)
        if data is None:
            raise Refused(404, "error.restore_gone")
        with refusing(BackupError):
            backup = read_backup(data)

        def work(progress) -> int:
            steps = apply_restore(client(), backup, settings.data_dir, progress)
            # Kept until here: a run that fails can be confirmed again
            # without uploading the backup a second time.
            drop_parked(settings.data_dir, token)
            restored[token] = (steps, backup.manifest)
            while len(restored) > KEEP_RESTORED:
                restored.popitem(last=False)
            return len(steps)

        ctx.prints.submit(RESTORE_KIND, token, work)
        return RedirectResponse(f"/restore/{token}/done", 303)

    @app.get("/restore/{token}/done", response_class=HTMLResponse)
    def restore_done(request: Request, token: str):
        if token in restored:
            steps, manifest = restored[token]
            return templates.TemplateResponse(request, "restore.html", {
                "steps": steps,
                "manifest": manifest,
                "token": token,
                "done": True,
                "silent_albums": _silent_albums(client(), settings.data_dir),
            })
        run = restore_run(token)
        data = read_parked(settings.data_dir, token) if token.isalnum() else None
        if data is None:
            raise Refused(404, "error.restore_gone")
        with refusing(BackupError):
            backup = read_backup(data)
        if run is not None and run.state is not JobState.FAILED:
            return templates.TemplateResponse(request, "restore.html", {
                "running": run, "steps": [], "manifest": backup.manifest, "token": token,
                "done": False,
            })
        # Stopped half way, or in a process before a restart that took the run
        # with it: the parked file is dropped only once a restore went through,
        # so what is there is shown again, to be confirmed anew.
        return templates.TemplateResponse(request, "restore.html", {
            "steps": plan_restore(client(), backup, settings.data_dir),
            "manifest": backup.manifest,
            "token": token,
            "done": False,
            "failed": (None if run is None else
                       run.problem.say(translator(request)) if run.problem else run.error),
            "unknown_run": run is None,
        })


def _silent_albums(client: TeddyCloudClient, data_dir: Path) -> List[str]:
    """The albums whose tonies point at audio the library does not have.

    A backup holds no audio; after a lost volume the stickers come back
    pointing at tafs that are gone. Those made from an album here can be
    encoded again from it, by the batch page.
    """
    try:
        present = played(taf.source for taf in list_tafs(client, strict=True))
    except TeddyCloudError:
        return []
    silent = {tag.model for tag in list_tags(client)
              if tag.model and is_library(tag.source) and same(tag.source) not in present}
    return [album for album, models in made(data_dir).items()
            if any(model in silent for model in models)]

