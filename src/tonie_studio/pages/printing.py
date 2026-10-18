"""The sticker sheet, the overview of every tonie and the printer calibration."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from ..calibration import (build_calibration, expected_first_centre, is_calibrated, read_shift,
                           reset, save_measurement)
from ..jobs import Job, JobRunner, JobState, new_id
from ..models import images_cached, is_cover_name
from ..printing import (back_of, known_models, overview_entries, sheet_rows,
                        stickers_for)
from ..overview import build_overview
from ..runtimes import playing_times
from ..sheet import (BACK_FADE, FILL, FIT, LAYOUTS, SHEET, Layout, Sticker, back_lines,
                     build_sheet)
from ..store import sweep
from .common import Context, Refused, translator

log = logging.getLogger(__name__)

# The most copies of one motif the sheet page lets a person ask for.
MOST_COPIES = 99

# How many finished sheets are kept for downloading again; older ones go
# when a newer one is done. A sheet of every tonie is some 20 MB.
KEEP_SHEETS = 3


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    client = ctx.client
    namer = ctx.namer
    prints = ctx.prints

    @app.get("/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request, model: List[str] = Query(default=[]), calibrated: str = "",
                   q: str = ""):
        """The sheet, with a row for every sticker that has a cover to print.

        Asked for with models, the first row of each comes ticked: the tonie
        page and the walkthrough send people here for exactly those. ``q``
        fills the search, which the page itself applies.
        """
        connection = client()
        cached = images_cached(connection)
        rows = sheet_rows(connection, namer.official(), cached)
        preset = set(model)
        for row in rows:
            if row.model.id in preset:
                row.ticked = True
                preset.discard(row.model.id)
        return templates.TemplateResponse(request, "sheet.html", {
            "rows": rows,
            "images_cached": cached,
            "query": q.strip(),
            "layouts": list(LAYOUTS.values()),
            # What the page's script needs of each: the count of sheets and
            # the size the text side is previewed at.
            "layout_sizes": {layout.key: {"perPage": layout.per_page,
                                          "diameterMm": layout.diameter_mm}
                             for layout in LAYOUTS.values()},
            "most_copies": MOST_COPIES,
            "fill": FILL,
            "fit": FIT,
            "preset": bool(model),
            "back_fade": BACK_FADE,
            "shift": read_shift(settings.data_dir),
            "expected": expected_first_centre(),
            "calibrated": bool(calibrated),
            # Never calibrated: the first sheet would be cut wherever this
            # printer happens to shift it.
            "first_print": not is_calibrated(settings.data_dir),
        })


    @app.get("/sheet/calibration")
    def calibration_sheet(request: Request):
        # Printed with the current correction, so measuring it shows what is
        # still off rather than what was off before.
        return _send_pdf(settings.data_dir, "calibration", "kalibrierblatt.pdf",
                         lambda target: build_calibration(target, read_shift(settings.data_dir),
                                                          translator(request)))


    @app.post("/sheet/calibration")
    def calibrate(left_mm: str = Form(...), top_mm: str = Form(...)):
        try:
            left, top = _millimetres(left_mm), _millimetres(top_mm)
        except ValueError as error:
            raise Refused(400, "error.millimetres") from error
        save_measurement(settings.data_dir, left, top)
        return RedirectResponse("/sheet?calibrated=1#printer", 303)


    @app.post("/sheet/calibration/reset")
    def reset_calibration():
        reset(settings.data_dir)
        return RedirectResponse("/sheet#printer", 303)


    @app.get("/sheet/overview")
    def overview(request: Request):
        translate = translator(request)
        entries = overview_entries(client(), playing_times(settings.data_dir), translate)
        if not entries:
            raise Refused(400, "error.nothing_to_list")
        return _send_pdf(settings.data_dir, "overview", "tonies-uebersicht.pdf",
                         lambda target: build_overview(entries, target, translate))


    @app.post("/sheet")
    def sheet(
        request: Request,
        picks: List[str] = Form(default=[]),
        files: List[UploadFile] = Form(default=[]),
        upload_mode: str = Form(FILL),
        back: str = Form(""),
        back_tracks: str = Form(""),
        layout: str = Form(SHEET.key),
    ):
        """Start making the sheet and send the browser to watch it.

        A sheet of hundreds of covers takes longer than the gateway waits for
        an answer, so it is made in the background, like an encode.
        """
        connection = client()
        known = known_models(connection, namer.official())
        wanted = [(known[model_id], count, mode) for model_id, count, mode in map(_pick, picks)
                  if count and model_id in known]
        # Read now: an uploaded file is gone once this request has its answer.
        uploads = [(upload.filename or "upload", upload.file.read()) for upload in files
                   if is_cover_name(upload.filename)]
        if not wanted and not uploads:
            raise Refused(400, "error.nothing_to_print")

        translate = translator(request)
        times = playing_times(settings.data_dir)
        stickers = stickers_for(client, wanted, bool(back), bool(back_tracks), times, translate)
        stickers += [Sticker(image=data, label=name, mode=FIT if upload_mode == FIT else FILL)
                     for name, data in uploads]
        job_id = new_id()
        target = settings.data_dir / f"sheet-{job_id}.pdf"
        prints.submit("sheet", f"sheet of {len(stickers)} motif(s)",
                      lambda progress: _make_sheet(stickers, target, read_shift(settings.data_dir),
                                                   progress, LAYOUTS.get(layout, SHEET)),
                      job_id=job_id)
        return RedirectResponse(f"/sheet/print/{job_id}", 303)

    @app.get("/sheet/print/{job_id}", response_class=HTMLResponse)
    def sheet_job_page(request: Request, job_id: str):
        job = _sheet_job(prints, job_id)
        return templates.TemplateResponse(request, "sheet_job.html", {"job": job})

    @app.get("/api/sheet/print/{job_id}")
    def sheet_job_state(request: Request, job_id: str):
        job = _sheet_job(prints, job_id)
        translate = translator(request)
        problem = None
        if job.state is JobState.FAILED:
            problem = (job.problem.say(translate) if job.problem
                       else translate("error.failed", detail=job.error))
        return {
            "state": job.state.value,
            "percent": job.percent or 0,
            "done": job.done_count,
            "total": job.total,
            "skipped": job.result.skipped if job.state is JobState.DONE else [],
            "problem": problem,
        }

    @app.get("/sheet/print/{job_id}/pdf")
    def sheet_job_pdf(job_id: str):
        job = _sheet_job(prints, job_id)
        if job.state is JobState.FAILED:
            if isinstance(job.problem, Refused):
                raise job.problem
            raise Refused(500, "error.failed", detail=job.error)
        if job.state is not JobState.DONE:
            raise Refused(409, "error.sheet_not_ready")
        if not job.result.path.exists():
            raise Refused(404, "error.sheet_gone")
        return FileResponse(job.result.path, media_type="application/pdf",
                            filename="traveltonies.pdf")

    @app.get("/api/sheet/back")
    def back_preview(request: Request, model: str, layout: str = SHEET.key):
        """The laid-out text side of one model, for the page to draw.

        Asked for when the page shows it rather than sent along with the page:
        the lines depend on the layout, and a page of every tonie would carry
        them all twice over.
        """
        known = known_models(client(), namer.official())
        if model not in known:
            raise Refused(404, "tonie.model_missing", model=model)
        back = back_of(known[model], playing_times(settings.data_dir).get(model),
                        translator(request))
        return {
            variant: [{"text": line.text, "size": line.size, "bold": line.bold}
                      for line in back_lines(back, with_tracks, LAYOUTS.get(layout, SHEET))]
            for variant, with_tracks in (("tracks", True), ("plain", False))
        }


# --- helpers --------------------------------------------------------------


# How old a PDF left in the data folder must be before housekeeping removes
# it. One being sent right now is seconds old.
OLD_PDF_SECONDS = 86400


def _send_pdf(data_dir: Path, prefix: str, filename: str,
              build: Callable[[Path], None]) -> FileResponse:
    """Build a PDF in ``data_dir``, send it as ``filename`` and remove it once sent.

    A build that fails removes what it had written before the error goes on.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"{prefix}-{uuid.uuid4().hex[:8]}.pdf"
    try:
        build(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return FileResponse(target, media_type="application/pdf", filename=filename,
                        background=BackgroundTask(target.unlink, missing_ok=True))


def clear_old_pdfs(data_dir: Path) -> None:
    """Remove PDFs a process that died while sending them left behind."""
    sweep(data_dir, "*.pdf", OLD_PDF_SECONDS)


def _pick(text: str) -> Tuple[str, int, str]:
    """One row's choice as the page sends it, "count:mode:model", as (model, count, mode).

    One field per row rather than three keeps a sheet of every tonie under
    the number of form fields a request may carry. A count is held to what
    the page lets a person ask for; anything unreadable counts as none.
    """
    count, mode, model_id = (text.split(":", 2) + ["", ""])[:3]
    try:
        copies = max(0, min(MOST_COPIES, int(count)))
    except ValueError:
        copies = 0
    return model_id, copies, FIT if mode == FIT else FILL


@dataclass
class MadeSheet:
    path: Path
    skipped: List[str] = field(default_factory=list)


def _make_sheet(stickers: Sequence[Sticker], target: Path, shift: Tuple[float, float],
                progress: Callable[[str, int, int], None], layout: Layout = SHEET) -> MadeSheet:
    """Build the sheet at ``target``, leaving nothing behind when that fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        skipped = build_sheet(stickers, target, shift,
                              progress=lambda done, total: progress("pictures", done, total),
                              layout=layout)
    except ValueError as error:
        # Every picture asked for was unreadable, so there is nothing to print.
        target.unlink(missing_ok=True)
        raise Refused(400, "error.nothing_to_print") from error
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if skipped:
        log.warning("left off the sheet: %s", ", ".join(skipped))
    _keep_latest_sheets(target.parent)
    return MadeSheet(target, skipped)


def _keep_latest_sheets(data_dir: Path) -> None:
    """Remove all but the KEEP_SHEETS newest sheets."""
    sheets = sorted(data_dir.glob("sheet-*.pdf"), key=lambda path: path.stat().st_mtime,
                    reverse=True)
    for old in sheets[KEEP_SHEETS:]:
        old.unlink(missing_ok=True)


def _sheet_job(prints: JobRunner, job_id: str) -> Job:
    job = prints.get(job_id)
    if job is None or job.kind != "sheet":
        raise Refused(404, "error.no_job")
    return job


def _millimetres(text: str) -> float:
    """A length as typed, with a decimal comma or point.

    float() also reads "nan" and "inf", which are not lengths and would turn
    into the largest allowed shift without anyone noticing.
    """
    value = float(text.strip().replace(",", ".").removesuffix("mm").strip())
    if not math.isfinite(value):
        raise ValueError(f"not a length: {text!r}")
    return value
