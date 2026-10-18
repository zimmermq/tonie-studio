"""The checkup that finds what is broken, and the backup that keeps what cannot be rebuilt."""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..albumnotes import made
from ..backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                      park, plan_restore, read_backup, take_parked)
from ..checkup import BULK_FIXES, apply_fix, examine
from ..encode import EncodeError, list_tafs
from ..library import is_library, played, same
from ..podcast import SERIES as PODCAST_SERIES
from ..tags import TonieError, list_tags
from ..teddycloud import TeddyCloudClient, TeddyCloudError
from .common import Context, Refused, refusing

log = logging.getLogger(__name__)

# How many restore results stay on show.
KEEP_RESTORED = 5


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    client = ctx.client

    def work_in_progress() -> Dict:
        """What the checkup must not call left over: tafs jobs made or are making.

        Encode jobs are labelled with their target. A podcast refresh builds
        its taf under a name only known once it runs, so while one is under
        way the whole podcast folder is spared.
        """
        busy = {job.label for job in ctx.jobs.all() if job.kind == "encode"}
        folders = {f"/{PODCAST_SERIES}"} if ctx.podcasts.refreshing else set()
        return {"busy": busy, "busy_folders": folders}

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
        connection = client()
        targets = [finding.target for finding in examine(connection, **work_in_progress())
                   if finding.fix == fix]
        with refusing(TonieError, EncodeError):
            for target in targets:
                apply_fix(connection, settings.data_dir, fix, target)
        log.info("checkup: %s applied to %d sticker(s)", fix, len(targets))
        return RedirectResponse(f"/checkup?done={fix}", 303)


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

    @app.post("/restore/{token}")
    def restore_apply(token: str):
        """Restore, then show what was done on a page of its own.

        Its own address, so reloading it shows the result again rather than
        sending the confirmation a second time.
        """
        if not token.isalnum():
            raise Refused(400, "error.not_a_restore")
        data = take_parked(settings.data_dir, token)
        if data is None:
            raise Refused(404, "error.restore_gone")
        with refusing(BackupError):
            backup = read_backup(data)
            steps = apply_restore(client(), backup, settings.data_dir)
        restored[token] = (steps, backup.manifest)
        while len(restored) > KEEP_RESTORED:
            restored.popitem(last=False)
        return RedirectResponse(f"/restore/{token}/done", 303)

    @app.get("/restore/{token}/done", response_class=HTMLResponse)
    def restore_done(request: Request, token: str):
        if token not in restored:
            raise Refused(404, "error.restore_gone")
        steps, manifest = restored[token]
        return templates.TemplateResponse(request, "restore.html", {
            "steps": steps,
            "manifest": manifest,
            "token": token,
            "done": True,
            "silent_albums": _silent_albums(client(), settings.data_dir),
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

