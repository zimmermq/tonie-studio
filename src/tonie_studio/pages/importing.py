"""Importing tonies from Flipper Zero dumps: start a run, and watch it go.

The work happens on the importer's own thread (see :mod:`importing`); these
routes only start, pause and stop it and show where it stands. A run takes
days, so the page is something to look in on rather than to wait at.
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..github import LARGEST_DUMP, RepoError, parse_repo
from ..importing import (CACHED, DUPLICATE, FAILED, IMPORTED, NEW, PENDING, UNREADABLE,
                         ImportProblem, Pace, Run)
from .common import Context, Refused, refused

# The order the counts are shown in.
STATES = (PENDING, IMPORTED, CACHED, FAILED, DUPLICATE, UNREADABLE)

# How many of the latest settled tonies the page lists.
RECENT = 10


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    templates = ctx.templates
    importer = ctx.importer

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        run = importer.current()
        return templates.TemplateResponse(request, "import.html", {
            "run": run,
            "states": STATES,
            "counts": _counts(run) if run else {},
            "failed": [item for item in run.items if item.state in (FAILED, UNREADABLE)] if run else [],
            "recent": _recent(run) if run else [],
            "next_at": datetime.fromtimestamp(run.next_at) if run and run.next_at else None,
            "pace": run.pace if run else Pace(),
        })

    @app.get("/api/import")
    def import_state():
        run = importer.current()
        if run is None:
            return JSONResponse({"phase": None})
        return JSONResponse({"phase": run.phase, "paused": run.paused, "counts": _counts(run),
                             "next_at": run.next_at})

    @app.post("/import/github")
    def start_github(repo: str = Form(...), branch: str = Form(""), subfolder: str = Form(""),
                     token: str = Form(""), per_hour: float = Form(...), jitter: float = Form(...),
                     max_failures: int = Form(...)):
        try:
            found = parse_repo(repo, branch=branch, subfolder=subfolder)
        except RepoError as error:
            raise refused(error) from error
        _start(lambda pace: importer.begin_github(found, pace, token=token.strip()),
               per_hour, jitter, max_failures)
        return RedirectResponse("/import", 303)

    @app.post("/import/files")
    def start_files(files: List[UploadFile], per_hour: float = Form(...),
                    jitter: float = Form(...), max_failures: int = Form(...)):
        dumps = []
        for upload in files:
            name = upload.filename or ""
            if not name.lower().endswith(".nfc"):
                continue
            # A dump is a few hundred bytes; reading past the limit tells a
            # file that is not one apart without holding all of it.
            data = upload.file.read(LARGEST_DUMP + 1)
            text = "" if len(data) > LARGEST_DUMP else data.decode("utf-8", errors="replace")
            dumps.append((name, text))
        if not dumps:
            raise Refused(400, "import.no_dumps")
        label = _common_folder([name for name, _ in dumps])
        _start(lambda pace: importer.begin(dumps, pace, label), per_hour, jitter, max_failures)
        return RedirectResponse("/import", 303)

    @app.post("/import/pause")
    def pause():
        importer.pause()
        return RedirectResponse("/import", 303)

    @app.post("/import/resume")
    def resume():
        importer.resume()
        return RedirectResponse("/import", 303)

    @app.post("/import/stop")
    def stop():
        importer.stop()
        return RedirectResponse("/import", 303)


def _start(begin, per_hour: float, jitter: float, max_failures: int) -> Run:
    """Start a run with the pace from the form; the jitter arrives in percent."""
    try:
        return begin(Pace(per_hour=per_hour, jitter=jitter / 100, max_failures=max_failures))
    except ImportProblem as error:
        raise refused(error, 409 if error.key == "import.busy" else 400) from error


def _counts(run: Run) -> dict:
    counts = {state: run.count(state) for state in STATES}
    counts[PENDING] += run.count(NEW)
    return counts


def _recent(run: Run) -> list:
    settled = [item for item in run.items if item.state in (IMPORTED, CACHED)]
    return settled[-RECENT:][::-1]


def _common_folder(paths: List[str]) -> str:
    """The folder all uploaded files share, or how many files there were."""
    tops = {path.split("/", 1)[0] for path in paths if "/" in path}
    if len(tops) == 1 and all("/" in path for path in paths):
        return tops.pop()
    return paths[0] if len(paths) == 1 else f"{len(paths)} .nfc"
