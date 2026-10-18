"""The sticker sheet, the overview of every tonie and the printer calibration."""

from __future__ import annotations

import logging
import math
import uuid
from pathlib import Path
from typing import Callable, List, Tuple

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from ..calibration import (build_calibration, expected_first_centre, is_calibrated, read_shift,
                           reset, save_measurement)
from ..jobs import JobState, new_id
from ..models import images_cached, is_cover_name
from ..printing import (PrintError, back_of, known_models, make_sheet, overview_entries,
                        sheet_rows, stickers_for)
from ..overview import build_overview
from ..runtimes import playing_times
from ..sheet import (BACK_FADE, FILL, FIT, LAYOUTS, SHEET, Sticker, back_lines)
from .common import Context, Refused, failure_text, job_or_404, read_picture, translator

log = logging.getLogger(__name__)

# The most copies of one motif the sheet page lets a person ask for.
MOST_COPIES = 99

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
        uploads = [(upload.filename or "upload", read_picture(upload)) for upload in files
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
                      lambda progress: make_sheet(stickers, target, read_shift(settings.data_dir),
                                                   progress, LAYOUTS.get(layout, SHEET)),
                      job_id=job_id)
        return RedirectResponse(f"/sheet/print/{job_id}", 303)

    @app.get("/sheet/print/{job_id}", response_class=HTMLResponse)
    def sheet_job_page(request: Request, job_id: str):
        job = job_or_404(prints, job_id, "sheet")
        return templates.TemplateResponse(request, "sheet_job.html", {"job": job})

    @app.get("/api/sheet/print/{job_id}")
    def sheet_job_state(request: Request, job_id: str):
        job = job_or_404(prints, job_id, "sheet")
        translate = translator(request)
        problem = failure_text(job, translate) if job.state is JobState.FAILED else None
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
        job = job_or_404(prints, job_id, "sheet")
        if job.state is JobState.FAILED:
            if isinstance(job.problem, PrintError):
                raise Refused(400, job.problem.key, **job.problem.params)
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


def _millimetres(text: str) -> float:
    """A length as typed, with a decimal comma or point.

    float() also reads "nan" and "inf", which are not lengths and would turn
    into the largest allowed shift without anyone noticing.
    """
    value = float(text.strip().replace(",", ".").removesuffix("mm").strip())
    if not math.isfinite(value):
        raise ValueError(f"not a length: {text!r}")
    return value
