"""The checkup that finds what is broken, and the backup that keeps what cannot be rebuilt."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..albumnotes import forget_model
from ..backup import (LARGEST_BACKUP, BackupError, apply_restore, create_backup, describe_backup,
                      plan_restore, read_backup)
from ..checkup import examine, model_repairs, repaired
from ..encode import EncodeError, delete_taf
from ..models import find_model, remove_model, update_model
from ..podcast import SERIES as PODCAST_SERIES
from ..runtimes import forget
from ..settings import Settings
from ..tags import TonieError, find_tag
from ..teddycloud import TeddyCloudClient, TeddyCloudError
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
            _apply_fix(connection, settings, fix, target)
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
                _apply_fix(connection, settings, fix, target)
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
    folder = _restore_dir(settings)
    if not folder.is_dir():
        return
    cutoff = time.time() - older_than
    for parked in folder.glob("*.zip"):
        try:
            if parked.stat().st_mtime < cutoff:
                parked.unlink()
        except OSError as error:
            log.warning("cannot remove %s: %s", parked, error)


def _apply_fix(client: TeddyCloudClient, settings: Settings, fix: str, target: str) -> None:
    """Make one checkup repair. The caller has just confirmed it is still due."""
    if fix == "nocloud":
        client.set_tag(target, nocloud=True)
    elif fix == "repair_source":
        tag = find_tag(client, target)
        fixed = repaired(tag.source if tag else None)
        if not fixed:
            raise TonieError("checkup.nothing_to_repair", target=target)
        client.set_tag(target, source=fixed)
    elif fix == "repair_model":
        model = find_model(client, target)
        if model is None:
            raise TonieError("tonie.model_missing", model=target)
        changes = model_repairs(model)
        update_model(client, target, series=changes.get("series"),
                     episode=changes.get("episode"), tracks=changes.get("tracks"))
    elif fix == "empty_sticker":
        # The model goes too: without it, the empty sticker would still carry
        # the old name and cover in teddyCloud.
        client.set_tag(target, source="", tonie_model="")
    elif fix == "delete_taf":
        delete_taf(client, target)
    elif fix == "delete_model":
        remove_model(client, target)
        forget(settings.data_dir, target)
        forget_model(settings.data_dir, target)
    else:
        raise ValueError(f"unknown repair {fix!r}")
