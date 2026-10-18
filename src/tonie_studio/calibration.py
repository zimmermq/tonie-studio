"""Lines the printer up with the sticker sheet.

Home printers place a page a millimetre or two off where they are asked to,
and on 46 mm circles that is the difference between cutting along the line and
cutting into the picture. The calibration sheet prints a cross at the centre of
every sticker and says where the first one should be; a person measures where
it actually landed, and from then on every sheet is moved by the difference.

Only a shift is corrected, never a scale. A 100 mm line that comes out shorter
means the print dialog is shrinking the page to fit, and the honest fix is to
print at actual size there; compensating here would hide the setting and break
the moment someone prints from another computer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .i18n import DEFAULT, Translator
from .sheet import CUT_LINE_GREY, SHEET, font_name, sticker_positions
from .store import LOCK, read_json, write_json

FILE_NAME = "print-shift.json"

# Larger than any printer is off by; smaller than the page margin, so a misread
# ruler cannot push a sticker off the paper.
LARGEST_SHIFT_MM = 5.0

RULER_MM = 100


def expected_first_centre() -> Tuple[float, float]:
    """Where the first sticker's centre belongs, in mm from the left and top edge."""
    first = sticker_positions(1)[0]
    return first.x / mm, (A4[1] - first.y) / mm


def read_shift(data_dir: Path) -> Tuple[float, float]:
    """The stored correction as (right, down) in mm; nothing stored is no shift."""
    stored = read_json(data_dir / FILE_NAME, {})
    try:
        return float(stored.get("right_mm", 0.0)), float(stored.get("down_mm", 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0, 0.0


def save_measurement(data_dir: Path, left_mm: float, top_mm: float) -> Tuple[float, float]:
    """Record where the first cross was measured and return the new correction.

    The sheet that was measured already carried the old correction, so the new
    one is the old plus what is still off. Measuring again therefore homes in
    rather than starting over.
    """
    want_left, want_top = expected_first_centre()
    with LOCK:
        right, down = read_shift(data_dir)
        right = _cap(right + want_left - left_mm)
        down = _cap(down + want_top - top_mm)
        write_json(data_dir / FILE_NAME, {"right_mm": right, "down_mm": down})
    return right, down


def build_calibration(target: Path, shift_mm: Tuple[float, float] = (0.0, 0.0),
                      translate: Optional[Translator] = None) -> None:
    """A sheet with a cross on every sticker centre and a ruler to check the scale."""
    translate = translate or Translator(DEFAULT)
    page_width, page_height = A4
    sheet = canvas.Canvas(str(target), pagesize=A4)
    sheet.setTitle(translate("pdf.calibration_title"))
    radius = SHEET.diameter_mm * mm / 2

    for number, position in enumerate(sticker_positions(SHEET.per_page, shift_mm), 1):
        sheet.setStrokeGray(CUT_LINE_GREY)
        sheet.setLineWidth(0.25)
        sheet.circle(position.x, position.y, radius, stroke=1, fill=0)
        sheet.setStrokeGray(0)
        sheet.setLineWidth(0.3 if number > 1 else 0.6)
        arm = 6 * mm
        sheet.line(position.x - arm, position.y, position.x + arm, position.y)
        sheet.line(position.x, position.y - arm, position.x, position.y + arm)
        if number == 1:
            sheet.setFont(font_name(True), 9)
            sheet.drawString(position.x + 2 * mm, position.y + 2 * mm, "1")

    want_left, want_top = expected_first_centre()
    sheet.setFillGray(0.1)
    sheet.setFont(font_name(True), 10)
    sheet.drawString(12 * mm, page_height - 10 * mm, translate("pdf.calibration_title"))
    sheet.setFont(font_name(False), 8)
    lines = [
        translate("pdf.calibration_measure", left=translate.number(want_left),
                  top=translate.number(want_top)),
        translate("pdf.calibration_ruler", length=RULER_MM),
    ]
    for offset, text in enumerate(lines):
        sheet.drawString(12 * mm, page_height - (15 + 4 * offset) * mm, text)

    _ruler(sheet, (page_width - RULER_MM * mm) / 2, 12 * mm)
    sheet.save()


def _ruler(sheet: canvas.Canvas, left: float, baseline: float) -> None:
    sheet.setStrokeGray(0)
    sheet.setLineWidth(0.4)
    sheet.line(left, baseline, left + RULER_MM * mm, baseline)
    sheet.setFont(font_name(False), 6)
    for step in range(RULER_MM + 1):
        length = 3 * mm if step % 10 == 0 else (2 * mm if step % 5 == 0 else 1 * mm)
        x = left + step * mm
        sheet.line(x, baseline, x, baseline + length)
        if step % 10 == 0:
            sheet.drawCentredString(x, baseline - 3 * mm, str(step))


def _cap(value: float) -> float:
    return max(-LARGEST_SHIFT_MM, min(LARGEST_SHIFT_MM, round(value, 1)))
