"""The checkup that finds what is broken, and the backup that keeps what cannot be rebuilt."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                      plan_restore, read_backup)
from ..checkup import apply_fix, examine
from ..encode import EncodeError
from ..podcast import SERIES as PODCAST_SERIES
from ..settings import Settings
from ..store import sweep
from ..tags import TonieError
from ..teddycloud import TeddyCloudError
from .common import Context, Refused, refusing

# The repairs the checkup page offers for all findings at once.
BULK_FIXES = frozenset({"nocloud"})

log = logging.getLogger(__name__)


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

        with refusing(TonieError, EncodeError, TeddyCloudError, ValueError):
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
        with refusing(TonieError, EncodeError, TeddyCloudError, ValueError):
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
        """Show what a restore would do. Nothing is written until confirmed.

        The upload is parked on the volume under a random name, so the
        confirmation acts on exactly the file that was previewed.
        """
        # Read at most one byte past the limit: enough to know it is too big
        # without holding all of it.
        data = file.file.read(LARGEST_BACKUP + 1)
        with refusing(BackupError):
            backup = read_backup(data)

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
            raise Refused(400, "error.not_a_restore")
        parked = _restore_dir(settings) / f"{token}.zip"
        if not parked.is_file():
            raise Refused(404, "error.restore_gone")
        try:
            with refusing(BackupError):
                backup = read_backup(parked.read_bytes())
                steps = apply_restore(client(), backup, settings.data_dir)
        finally:
            parked.unlink(missing_ok=True)
        return templates.TemplateResponse(request, "restore.html", {
            "steps": steps,
            "manifest": backup.manifest,
            "token": token,
            "done": True,
        })


# --- helpers --------------------------------------------------------------


def _restore_dir(settings: Settings) -> Path:
    """Where an uploaded backup waits between preview and confirmation."""
    return settings.data_dir / "restore"


def clear_parked_restores(settings: Settings, older_than: float = 86400.0) -> None:
    """Drop backups uploaded for a preview that was never confirmed."""
    sweep(_restore_dir(settings), "*.zip", older_than)
