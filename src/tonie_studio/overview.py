"""An A4 list of every tonie, to keep in the tin or pin to a door.

The stickers say what one tonie is; this says what there is: the tonies made
here and the bought ones alike. Each entry is a small round cover beside the
album's name, who made it and the facts that are known, three columns to a
page, so that a collection of several hundred stays a list of some twenty
pages. A name that does not fit is wrapped rather than shrunk.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .pictures import PICTURE_ERRORS
from .i18n import DEFAULT, Translator
from .sheet import CUT_LINE_GREY, FILL, composed, font_name, render_square, wrap_text

log = logging.getLogger(__name__)

COLUMNS = 3
ROWS_PER_PAGE = 12
MARGIN_MM = 12
HEADING_MM = 12
COVER_MM = 17
GAP_MM = 3

TITLE_PT = 8.5
SERIES_PT = 7.0
FACT_PT = 6.5
LEADING = 1.2

# Enough lines for a long title; more would run into the next entry.
MOST_TITLE_LINES = 3


@dataclass
class Entry:
    # The cover, or what fetches it when the entry is drawn; None or a fetcher
    # answering None leaves an empty ring.
    image: Union[bytes, Callable[[], Optional[bytes]], None]
    title: str
    series: str = ""
    facts: Sequence[str] = field(default_factory=list)


def build_overview(entries: Sequence[Entry], target: Path,
                   translate: Optional[Translator] = None) -> None:
    """Write the overview to ``target``, as many pages as the entries need."""
    translate = translate or Translator(DEFAULT)
    heading = translate("pdf.overview_title")
    if not entries:
        raise ValueError("there is no tonie to list")

    page_width, page_height = A4
    column_width = (page_width - 2 * MARGIN_MM * mm) / COLUMNS
    row_height = (page_height - 2 * MARGIN_MM * mm - HEADING_MM * mm) / ROWS_PER_PAGE
    per_page = COLUMNS * ROWS_PER_PAGE

    sheet = canvas.Canvas(str(target), pagesize=A4)
    sheet.setTitle(heading)
    pages = (len(entries) + per_page - 1) // per_page

    for index, entry in enumerate(entries):
        slot = index % per_page
        if slot == 0:
            if index:
                sheet.showPage()
            _heading(sheet, heading, translate("pdf.page", page=index // per_page + 1, pages=pages),
                     pages)
        # Down each column before the next, the way a list is read.
        column, row = divmod(slot, ROWS_PER_PAGE)
        left = MARGIN_MM * mm + column * column_width
        top = page_height - MARGIN_MM * mm - HEADING_MM * mm - row * row_height
        _entry(sheet, entry, left, top, column_width, row_height)

    sheet.save()


def _heading(sheet: canvas.Canvas, heading: str, page: str, pages: int) -> None:
    page_width, page_height = A4
    baseline = page_height - MARGIN_MM * mm - 7 * mm
    sheet.setFillGray(0.1)
    sheet.setFont(font_name(True), 16)
    sheet.drawString(MARGIN_MM * mm, baseline, heading)
    if pages > 1:
        sheet.setFont(font_name(False), 9)
        sheet.drawRightString(page_width - MARGIN_MM * mm, baseline, page)


def _entry(sheet: canvas.Canvas, entry: Entry, left: float, top: float,
           width: float, height: float) -> None:
    cover = COVER_MM * mm
    centre_y = top - height / 2
    image = entry.image() if callable(entry.image) else entry.image
    _cover(sheet, image, left + cover / 2, centre_y, cover / 2)

    text_left = left + cover + GAP_MM * mm
    text_width = width - cover - 2 * GAP_MM * mm

    title = wrap_text(composed(entry.title), TITLE_PT, True, text_width)
    lines = [(line, TITLE_PT, True) for line in title[:MOST_TITLE_LINES]]
    if entry.series:
        lines += [(line, SERIES_PT, False)
                  for line in wrap_text(composed(entry.series), SERIES_PT, False, text_width)[:1]]
    for fact in entry.facts:
        lines += [(line, FACT_PT, False)
                  for line in wrap_text(composed(fact), FACT_PT, False, text_width)[:1]]

    # The block is centred beside the cover, so a short entry does not sit at
    # the top of its row with empty space below.
    block = sum(size * LEADING for _text, size, _bold in lines)
    cursor = centre_y + block / 2
    sheet.setFillGray(0.1)
    for text, size, bold in lines:
        cursor -= size * LEADING
        sheet.setFont(font_name(bold), size)
        sheet.drawString(text_left, cursor + size * 0.2, text)


def _cover(sheet: canvas.Canvas, image: Optional[bytes], x: float, y: float, radius: float) -> None:
    """The cover clipped round, or an empty ring where there is none."""
    reader = None
    if image:
        try:
            reader = ImageReader(io.BytesIO(render_square(image, FILL, size=300)))
        except PICTURE_ERRORS as error:
            # A cover that cannot be read costs its picture, not the page.
            log.warning("overview: a cover cannot be read: %s", error)

    if reader is not None:
        sheet.saveState()
        path = sheet.beginPath()
        path.circle(x, y, radius)
        sheet.clipPath(path, stroke=0)
        sheet.drawImage(reader, x - radius, y - radius, 2 * radius, 2 * radius)
        sheet.restoreState()

    sheet.setStrokeGray(CUT_LINE_GREY)
    sheet.setLineWidth(0.4)
    sheet.circle(x, y, radius, stroke=1, fill=0)
