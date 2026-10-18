"""The sticker sheet, the overview of every tonie and the printer calibration."""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from ..calibration import FILE_NAME as CALIBRATION_FILE
from ..calibration import build_calibration, expected_first_centre, read_shift, save_measurement
from ..i18n import DEFAULT as DEFAULT_LANGUAGE
from ..i18n import Translator
from ..models import Model, cache_name, fetch_picture, images_cached, is_cover_name, list_models
from ..jobs import Job, JobRunner, JobState, new_id
from ..naming import Catalog
from ..overview import Entry, build_overview
from ..runtimes import minutes, playing_times
from ..sheet import (BACK_FADE, DIAMETER_MM, FILL, FIT, PER_PAGE, Back, Sticker, back_lines,
                     build_sheet)
from ..tags import list_tags
from ..teddycloud import TeddyCloudClient, TeddyCloudError
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
        rows = _sheet_rows(connection, namer.catalog()[0], cached)
        preset = set(model)
        for row in rows:
            if row.model.id in preset:
                row.ticked = True
                preset.discard(row.model.id)
        models = list({row.model.id: row.model for row in rows}.values())
        return templates.TemplateResponse(request, "sheet.html", {
            "rows": rows,
            "images_cached": cached,
            "query": q.strip(),
            "per_page": PER_PAGE,
            "most_copies": MOST_COPIES,
            "fill": FILL,
            "fit": FIT,
            "preset": bool(model),
            "backs": _back_preview(models, playing_times(settings.data_dir), translator(request)),
            "back_fade": BACK_FADE,
            "diameter_mm": DIAMETER_MM,
            "shift": read_shift(settings.data_dir),
            "expected": expected_first_centre(),
            "calibrated": bool(calibrated),
            # Never calibrated: the first sheet would be cut wherever this
            # printer happens to shift it.
            "first_print": not (settings.data_dir / CALIBRATION_FILE).exists(),
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
        want_left, want_top = expected_first_centre()
        # Measured exactly where it belongs, minus what is stored, is no shift.
        right, down = read_shift(settings.data_dir)
        save_measurement(settings.data_dir, want_left + right, want_top + down)
        return RedirectResponse("/sheet#printer", 303)


    @app.get("/sheet/overview")
    def overview(request: Request):
        translate = translator(request)
        entries = _overview_entries(client(), playing_times(settings.data_dir), translate)
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
    ):
        """Start making the sheet and send the browser to watch it.

        A sheet of hundreds of covers takes longer than the gateway waits for
        an answer, so it is made in the background, like an encode.
        """
        connection = client()
        known = _known_models(connection, namer.catalog()[0])
        wanted = [(known[model_id], count, mode) for model_id, count, mode in map(_pick, picks)
                  if count and model_id in known]
        # Read now: an uploaded file is gone once this request has its answer.
        uploads = [(upload.filename or "upload", upload.file.read()) for upload in files
                   if is_cover_name(upload.filename)]
        if not wanted and not uploads:
            raise Refused(400, "error.nothing_to_print")

        translate = translator(request)
        times = playing_times(settings.data_dir)
        stickers = _stickers(client, wanted, bool(back), bool(back_tracks), times, translate)
        stickers += [Sticker(image=data, label=name, mode=FIT if upload_mode == FIT else FILL)
                     for name, data in uploads]
        job_id = new_id()
        target = settings.data_dir / f"sheet-{job_id}.pdf"
        prints.submit("sheet", f"sheet of {len(stickers)} motif(s)",
                      lambda progress: _make_sheet(stickers, target, read_shift(settings.data_dir),
                                                   progress),
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
    if not data_dir.is_dir():
        return
    cutoff = time.time() - OLD_PDF_SECONDS
    for leftover in data_dir.glob("*.pdf"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError as error:
            log.warning("cannot remove %s: %s", leftover, error)


@dataclass
class SheetRow:
    """One cover the sheet can print: a sticker and the model it plays, or a model on none."""

    model: Model
    uid: str = ""  # as teddyCloud writes it; empty for a model on no sticker
    ruid: str = ""
    ticked: bool = False

    @property
    def search(self) -> str:
        """What the page's search looks through: the names, the model and the UID.

        The UID is there without its colons too, the way it is often printed
        on a chip, so either spelling finds it.
        """
        return " ".join((self.model.label, self.model.id, self.uid,
                         self.uid.replace(":", ""))).lower()


def _printable(model: Model, cached: Optional[bool]) -> bool:
    """Whether a model has a picture this app can put on a sheet.

    That is a cover in custom_img, or, while teddyCloud keeps the pictures it
    is shown, one hosted elsewhere, which then comes through its cache.
    """
    return bool(model.cover or (cached is not False and cache_name(model.picture)))


def _known_models(client: TeddyCloudClient, catalog: Catalog,
                  cached: Optional[bool] = True) -> Dict[str, Model]:
    """Every printable model on a sticker, and every printable model of this app, by id.

    Only catalogue tonies on a sticker: the catalogue lists thousands nobody
    here owns. Their picture comes through teddyCloud's image cache.
    """
    custom = {model.id: model for model in list_models(client)}
    known = {model_id: model for model_id, model in custom.items() if _printable(model, cached)}
    for tag in list_tags(client):
        if tag.model and tag.model not in custom and tag.model in catalog.by_model:
            model = Model.from_json(catalog.by_model[tag.model])
            if _printable(model, cached):
                known[model.id] = model
    return known


def _sheet_rows(client: TeddyCloudClient, catalog: Catalog,
                cached: Optional[bool] = True) -> List[SheetRow]:
    """A row per sticker with a printable model, then the models of this app on none.

    Two stickers that play the same tonie are two rows: each chip in hand
    gets a cover of its own.
    """
    known = _known_models(client, catalog, cached)
    rows = [SheetRow(known[tag.model], tag.uid, tag.ruid)
            for tag in list_tags(client) if tag.model in known]
    on_stickers = {row.model.id for row in rows}
    rows += [SheetRow(model) for model in known.values() if model.id not in on_stickers]
    return sorted(rows, key=lambda row: (row.model.label.casefold(), row.uid))


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


def _stickers(client: Callable[[], TeddyCloudClient], wanted: Sequence[Tuple[Model, int, str]],
              back: bool, back_tracks: bool, times: Dict[str, int],
              translate: Translator) -> List[Sticker]:
    """The motifs of a sheet, each fetching its picture only when the sheet gets to it.

    The text side follows its cover, and both use one download: the last
    picture fetched is kept until the next motif asks for another.
    """
    last: Dict[str, Optional[bytes]] = {}

    def picture_of(model: Model) -> Callable[[], Optional[bytes]]:
        def fetch() -> Optional[bytes]:
            if model.id not in last:
                last.clear()
                last[model.id] = _cover_bytes(client(), model)
            return last[model.id]
        return fetch

    stickers = []
    for model, count, mode in wanted:
        stickers.append(Sticker(image=picture_of(model), count=count, label=model.label,
                                mode=mode))
        # One text side per copy, right after it, so a sheet cut up in order
        # leaves each pair together.
        if back:
            description = _back_of(model, times.get(model.id), translate)
            if not back_tracks:
                description.tracks = ()
            stickers.append(Sticker(image=picture_of(model), count=count,
                                    label=f"{model.label} back", back=description))
    return stickers


@dataclass
class MadeSheet:
    path: Path
    skipped: List[str] = field(default_factory=list)


def _make_sheet(stickers: Sequence[Sticker], target: Path, shift: Tuple[float, float],
                progress: Callable[[str, int, int], None]) -> MadeSheet:
    """Build the sheet at ``target``, leaving nothing behind when that fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        skipped = build_sheet(stickers, target, shift,
                              progress=lambda done, total: progress("pictures", done, total))
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


def _overview_entries(client: TeddyCloudClient, times: Dict[str, int],
                      translate: Optional[Translator] = None) -> List[Entry]:
    """One entry per model that sits on a sticker, in shelf order.

    A model no sticker names is not a tonie anyone can pick up, so it does not
    belong on a list of what is in the tin.
    """
    on_stickers = {tag.model for tag in list_tags(client) if tag.model}
    entries = []
    for model in list_models(client):
        if model.id not in on_stickers:
            continue
        image = None
        if model.cover or cache_name(model.picture):
            try:
                image = fetch_picture(client, model.picture)
            except TeddyCloudError as error:
                log.warning("overview: no cover for %s: %s", model.id, error)
        back = _back_of(model, times.get(model.id), translate)
        entries.append(Entry(image=image, title=back.title, series=back.series,
                             facts=list(back.notes)))
    return sorted(entries, key=lambda entry: (entry.series.casefold(), entry.title.casefold()))


def _back_of(model: Model, seconds: Optional[int] = None,
             translate: Optional[Translator] = None) -> Back:
    """What the text side of one model says.

    The episode names the album and the series names who made it, which is the
    order they are looked for in: a shelf of tonies is searched by album.
    Facts the model does not carry are left out rather than guessed at, so a
    tonie recorded before the app stored them simply says less.
    """
    translate = translate or Translator(DEFAULT_LANGUAGE)
    facts = [
        str(model.year or ""),
        translate("pdf.tracks", count=len(model.tracks)) if model.tracks else "",
        translate("pdf.minutes", count=minutes(seconds)) if minutes(seconds) else "",
    ]
    notes = [" - ".join(fact for fact in facts if fact), model.id]
    return Back(
        title=model.episode or model.series or model.id,
        series=model.series,
        notes=[note for note in notes if note],
        tracks=list(model.tracks),
        language=translate.lang,
    )


def _back_preview(models: Sequence[Model], times: Dict[str, int],
                  translate: Optional[Translator] = None) -> Dict[str, Dict[str, List[Dict]]]:
    """The laid-out text side of every model, for the page to draw.

    The lines come from the same call the sheet makes, so what the preview
    shows is what gets printed; only the font differs.
    """
    preview = {}
    for model in models:
        back = _back_of(model, times.get(model.id), translate)
        preview[model.id] = {
            variant: [{"text": line.text, "size": line.size, "bold": line.bold}
                      for line in back_lines(back, with_tracks=with_tracks)]
            for variant, with_tracks in (("tracks", True), ("plain", False))
        }
    return preview


def _cover_bytes(client: TeddyCloudClient, model: Optional[Model]) -> Optional[bytes]:
    """The cover of a printable model, or None when there is none to be read."""
    if model is None or not (model.cover or cache_name(model.picture)):
        return None
    try:
        return fetch_picture(client, model.picture)
    except TeddyCloudError as error:
        log.warning("cannot read the cover of %s: %s", model.id, error)
        return None


def _millimetres(text: str) -> float:
    """A length as typed, with a decimal comma or point.

    float() also reads "nan" and "inf", which are not lengths and would turn
    into the largest allowed shift without anyone noticing.
    """
    value = float(text.strip().replace(",", ".").removesuffix("mm").strip())
    if not math.isfinite(value):
        raise ValueError(f"not a length: {text!r}")
    return value
