"""The sticker sheet, the overview of every tonie and the printer calibration."""

from __future__ import annotations

import logging
import math
import uuid
from typing import Dict, List, Optional, Sequence

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from ..calibration import FILE_NAME as CALIBRATION_FILE
from ..calibration import build_calibration, expected_first_centre, read_shift, save_measurement
from ..i18n import DEFAULT as DEFAULT_LANGUAGE
from ..i18n import Translator
from ..models import Model, fetch_cover, list_models
from ..overview import Entry, build_overview
from ..runtimes import minutes, playing_times
from ..sheet import (BACK_FADE, DIAMETER_MM, FILL, FIT, PER_PAGE, Back, Sticker, back_lines,
                     build_sheet)
from ..tags import list_tags
from ..teddycloud import TeddyCloudClient, TeddyCloudError
from .common import Context, Refused, is_image_name, translator

log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    client = ctx.client

    @app.get("/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request, model: List[str] = Query(default=[]), calibrated: str = ""):
        """The sheet, optionally with motifs already asked for.

        Coming from tonies that were just recorded, hunting for them again in
        this list is the step that was missing. Asked for that way, the page
        is the last step of the walkthrough and says so.
        """
        models = _printable_models(client())
        return templates.TemplateResponse(request, "sheet.html", {
            "models": models,
            "per_page": PER_PAGE,
            "fill": FILL,
            "fit": FIT,
            "preset": set(model),
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
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"calibration-{uuid.uuid4().hex[:8]}.pdf"
        # Printed with the current correction, so measuring it shows what is
        # still off rather than what was off before.
        build_calibration(target, read_shift(settings.data_dir), translator(request))
        return FileResponse(
            target,
            media_type="application/pdf",
            filename="kalibrierblatt.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )


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
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"overview-{uuid.uuid4().hex[:8]}.pdf"
        build_overview(entries, target, translate)
        return FileResponse(
            target,
            media_type="application/pdf",
            filename="tonies-uebersicht.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )


    @app.post("/sheet")
    def sheet(
        request: Request,
        models: List[str] = Form(default=[]),
        counts: List[int] = Form(default=[]),
        modes: List[str] = Form(default=[]),
        files: List[UploadFile] = Form(default=[]),
        upload_mode: str = Form(FILL),
        back: str = Form(""),
        back_tracks: str = Form(""),
    ):
        # models, counts and modes are three parallel lists in row order, the
        # same shape the album page uses for its tracks.
        connection = client()
        stickers = []
        described = {model.id: model for model in _printable_models(connection)}
        times = playing_times(settings.data_dir)

        for position, model in enumerate(models):
            count = counts[position] if position < len(counts) else 0
            if count < 1:
                continue
            picture = _model_picture(connection, model)
            if picture is None:
                continue
            mode = modes[position] if position < len(modes) else FILL
            stickers.append(Sticker(image=picture, count=count, label=model,
                                    mode=FIT if mode == FIT else FILL))
            # One text side per copy, right after it, so a sheet cut up in
            # order leaves each pair together.
            if back and model in described:
                description = _back_of(described[model], times.get(model), translator(request))
                if not back_tracks:
                    description.tracks = ()
                stickers.append(Sticker(image=picture, count=count, label=f"{model} back",
                                        back=description))

        for upload_file in files:
            if is_image_name(upload_file.filename):
                stickers.append(Sticker(image=upload_file.file.read(),
                                        label=upload_file.filename or "upload",
                                        mode=FIT if upload_mode == FIT else FILL))

        if not stickers:
            raise Refused(400, "error.nothing_to_print")

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        target = settings.data_dir / f"sheet-{uuid.uuid4().hex[:8]}.pdf"
        skipped = build_sheet(stickers, target, read_shift(settings.data_dir))
        if skipped:
            log.warning("left off the sheet: %s", ", ".join(skipped))

        return FileResponse(
            target,
            media_type="application/pdf",
            filename="traveltonies.pdf",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )


# --- helpers --------------------------------------------------------------


def _printable_models(client: TeddyCloudClient) -> List[Model]:
    """The custom models that have a cover of their own to print.

    A model without a picture, or one pointing at an image hosted elsewhere,
    has nothing this app can put on a sticker.
    """
    return [model for model in list_models(client) if model.cover]


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
        if model.cover:
            try:
                image = fetch_cover(client, model.cover)
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


def _model_picture(client: TeddyCloudClient, model_id: str) -> Optional[bytes]:
    """The cover bytes for one model, or None when it cannot be read."""
    for model in _printable_models(client):
        if model.id == model_id:
            try:
                return fetch_cover(client, model.cover)
            except TeddyCloudError as error:
                log.warning("cannot read the cover of %s: %s", model_id, error)
                return None
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
