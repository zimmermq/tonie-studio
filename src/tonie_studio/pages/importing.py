"""Importing tonies from Flipper Zero dumps: start a run, and watch it go.

The work happens on the importer's own thread (see :mod:`importing`); these
routes only start, pause and stop it and show where it stands. A run takes
days, so the page is something to look in on rather than to wait at.
"""

from __future__ import annotations

from typing import List

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..github import LARGEST_DUMP, RepoError, parse_repo
from ..teddycloud import TeddyCloudError
from ..times import from_seconds
from ..importing import (CACHED, DUPLICATE, FAILED, IMPORTED, NEW, PENDING, UNREADABLE,
                         ImportProblem, Pace, Run)
from .. import naming
from ..jobs import JobState
from .common import Context, Refused, refused, upload_form

# The order the counts are shown in.
STATES = (PENDING, IMPORTED, CACHED, FAILED, DUPLICATE, UNREADABLE)

# How many of the latest settled tonies the page lists.
RECENT = 10

# The order the naming steps are listed in.
KINDS = (naming.RENAME, naming.PROMOTE, naming.RETITLE, naming.NEW, naming.REUSE,
         naming.OFFICIAL)


# The job kind of writing the names a person looked at.
NAMES_KIND = "names"


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    templates = ctx.templates
    importer = ctx.importer
    namer = ctx.namer

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        run = importer.current()
        return templates.TemplateResponse(request, "import.html", {
            "run": run,
            "states": STATES,
            "counts": _counts(run) if run else {},
            "failed": _problems(run) if run else [],
            "recent": _recent(run) if run else [],
            "next_at": from_seconds(run.next_at) if run else None,
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
    def start_files(form: FormData = Depends(upload_form)):
        try:
            per_hour = float(form["per_hour"])
            jitter = float(form["jitter"])
            max_failures = int(form["max_failures"])
        except (KeyError, TypeError, ValueError) as error:
            raise Refused(400, "import.bad_pace") from error
        files = [part for part in form.getlist("files") if isinstance(part, StarletteUploadFile)]
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

    def names_run(job_id: str):
        job = ctx.prints.get(job_id)
        return job if job is not None and job.kind == NAMES_KIND else None

    @app.get("/import/names", response_class=HTMLResponse)
    def names_page(request: Request, named: int = -1, deleted: int = 0, changed: int = 0,
                   running: str = ""):
        """What the upkeep would change now, renaming the old models included."""
        if running:
            job = names_run(running)
            if job is None:
                return RedirectResponse("/import/names", 303)
            if job.state is JobState.DONE:
                return RedirectResponse(f"/import/names?named={len(job.result.steps)}"
                                        f"&deleted={len(job.result.retired)}", 303)
            if job.state is JobState.FAILED:
                return names_failed(job)
            return templates.TemplateResponse(request, "names.html", {"running": job})
        todo = namer.preview(migrate=True)
        steps = sorted(todo.steps, key=lambda step: (KINDS.index(step.kind), step.path))
        return templates.TemplateResponse(request, "names.html", {
            "plan": todo, "steps": steps, "kinds": KINDS,
            "counts": {kind: sum(1 for step in steps if step.kind == kind) for kind in KINDS},
            "named": named, "deleted": deleted, "changed": bool(changed),
            "fingerprint": naming.fingerprint(todo),
        })

    def names_failed(job):
        """Say why a run stopped. A plan changed meanwhile is shown again;
        teddyCloud answering with a refusal is not teddyCloud being away,
        which is what the app's handler for its errors would say."""
        error = job.exception
        if isinstance(error, naming.PlanChanged):
            return RedirectResponse("/import/names?changed=1", 303)
        if isinstance(error, TeddyCloudError) and not error.refused:
            raise error
        raise Refused(409, "names.refused", detail=job.error)

    @app.post("/import/names")
    def apply_names(plan: str = Form("")):
        """Start writing the names, behind a page that watches.

        Only what the person looked at is written: a plan that changed since,
        say because an import moved on, is shown again instead. It is checked
        here, so that is said at once, and again under the lock as the run
        starts. Hundreds of old models take longer than a proxy in front of
        the app waits, hence the run. One at a time: a second click joins it.
        """
        under_way = next((job for job in ctx.prints.all() if job.kind == NAMES_KIND
                          and job.state in (JobState.QUEUED, JobState.RUNNING)), None)
        if under_way is None:
            if naming.fingerprint(namer.preview(migrate=True)) != plan:
                return RedirectResponse("/import/names?changed=1", 303)
            job_id = ctx.prints.submit(
                NAMES_KIND, "names",
                lambda progress: namer.upkeep(migrate=True, expect=plan, progress=progress))
        else:
            job_id = under_way.id
        return RedirectResponse(f"/import/names?running={job_id}", 303)

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
    """What the run did lately, newest first, each with when.

    Tonies teddyCloud had before the run began are left out: the run did
    nothing for them, and they would crowd out what it did.
    """
    settled = [item for item in run.items
               if item.state in (IMPORTED, CACHED) and item.settled_at]
    settled.sort(key=lambda item: item.settled_at, reverse=True)
    return [(item, from_seconds(item.settled_at))
            for item in settled[:RECENT]]


def _problems(run: Run) -> list:
    """The tonies that failed, newest first with when, then the unreadable dumps.

    An unreadable dump never reached teddyCloud and has no time.
    """
    failed = sorted((item for item in run.items if item.state == FAILED),
                    key=lambda item: item.settled_at, reverse=True)
    unreadable = [item for item in run.items if item.state == UNREADABLE]
    return [(item, from_seconds(item.settled_at))
            for item in failed + unreadable]


def _common_folder(paths: List[str]) -> str:
    """The folder all uploaded files share, or how many files there were."""
    tops = {path.split("/", 1)[0] for path in paths if "/" in path}
    if len(tops) == 1 and all("/" in path for path in paths):
        return tops.pop()
    return paths[0] if len(paths) == 1 else f"{len(paths)} .nfc"
