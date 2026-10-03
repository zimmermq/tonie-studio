"""Builds the printable sheet of round cover stickers.

A traveltonie is an NFC tag with the album cover stuck on it, and 46 mm is the
diameter that covers one. The sheet carries nothing but the pictures: no titles,
no crop marks beyond the cut line itself, because everything printed inside the
circle ends up on the tag.

Three decisions are worth knowing before changing anything here:

- The picture is printed larger than the circle is cut. Scissors wander, and a
  cut that lands outside the line must still land on the cover rather than on
  white paper. The bleed has to stay smaller than half the gap, or neighbouring
  stickers print into each other.
- A cover is never scaled non-uniformly. A stretched face is unmissable on a
  46 mm circle, so a picture that is not square is cropped to a centred square
  first and loses its edges instead.
- A square cover does not fit inside a circle: filling the circle cuts whatever
  runs along the edges of the artwork, which on an album cover is usually the
  lettering. ``FILL`` accepts that and prints edge to edge; ``FIT`` shrinks the
  cover until all of it is inside and hides the ring under a blurred copy. Both
  are honest answers to the same geometry and the choice is a matter of taste.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageFilter, UnidentifiedImageError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

log = logging.getLogger(__name__)

# What fits on a tag.
DIAMETER_MM = 46

# Space between two stickers, which is also what the scissors need.
GAP_MM = 3

# How far the picture is printed beyond the cut line, per side.
BLEED_MM = 1

COLUMNS = 4
ROWS = 5
PER_PAGE = COLUMNS * ROWS

# Printing a 46 mm circle at 300 dpi needs 543 px; more only inflates the file.
PIXELS = round((DIAMETER_MM + 2 * BLEED_MM) / 25.4 * 300)

# How the cover meets the circle. "fill" lets the circle cut into the artwork;
# "fit" keeps the whole cover visible and fills the ring around it with a
# blurred copy of itself.
FILL = "fill"
FIT = "fit"

# A square fits inside a circle at 1/sqrt(2) of its diameter.
FIT_FACTOR = 0.707

# How far text stays clear of the cut line. Scissors wander by about a
# millimetre and a clipped letter is worse than a shorter line.
TEXT_SAFE_MM = 3

# The text side, in points. The title carries the weight; the track list is
# small on purpose, because its job is to be checked, not read across a room.
TITLE_PT = 8.0
SERIES_PT = 6.5
NOTE_PT = 6.0
TRACK_PT = 5.5
LEADING = 1.22

# How much white is mixed into the cover behind the text. Below this the
# artwork swallows the letters; above it the pair stops looking related.
BACK_FADE = 0.78

# Barely visible, but enough to cut along.
CUT_LINE_GREY = 0.75
CUT_LINE_WIDTH = 0.25


@dataclass
class Back:
    """What the text side of a tonie says.

    Only what a published model actually holds: it knows its series, its
    episode and its track names, and nothing about the year the album came out
    or how long it runs.
    """

    title: str
    series: str = ""
    # Short facts under the name, one per line: the year and playing time, then
    # the model number that finds the tonie again in the app.
    notes: Sequence[str] = ()
    tracks: Sequence[str] = ()


@dataclass(frozen=True)
class TextLine:
    """One laid-out line, as both the sheet and the screen have to show it."""

    text: str
    size: float
    bold: bool = False


@dataclass
class Sticker:
    """One motif and how many copies of it to print."""

    image: Union[bytes, Path]
    count: int = 1
    mode: str = FILL
    # Only used to name the motif in a message about a picture that failed.
    label: str = ""
    # Set for the text side: the cover is then faded to a watermark and these
    # words are printed over it.
    back: Optional[Back] = None


@dataclass(frozen=True)
class Position:
    """Where one sticker goes: its centre, in PDF points, on which page."""

    page: int
    x: float
    y: float


def sticker_positions(count: int) -> List[Position]:
    """Centres for ``count`` stickers, left to right and top to bottom.

    The grid is centred on the page, so the margins come out the same on every
    side and a printer's unprintable edge cannot clip a sticker.
    """
    page_width, page_height = A4
    pitch = (DIAMETER_MM + GAP_MM) * mm
    used_width = COLUMNS * DIAMETER_MM * mm + (COLUMNS - 1) * GAP_MM * mm
    used_height = ROWS * DIAMETER_MM * mm + (ROWS - 1) * GAP_MM * mm

    left = (page_width - used_width) / 2 + DIAMETER_MM * mm / 2
    # PDF coordinates grow upwards, so the first row is the highest one.
    top = (page_height + used_height) / 2 - DIAMETER_MM * mm / 2

    positions = []
    for index in range(count):
        slot = index % PER_PAGE
        positions.append(Position(
            page=index // PER_PAGE,
            x=left + (slot % COLUMNS) * pitch,
            y=top - (slot // COLUMNS) * pitch,
        ))
    return positions


def build_sheet(stickers: Sequence[Sticker], target: Path) -> List[str]:
    """Write the sheet to ``target`` and return the motifs that had to be skipped.

    A cover that cannot be decoded is left out rather than failing the sheet:
    the other stickers are still worth printing, and the caller is told which
    one to look at.
    """
    prepared: List[Tuple[ImageReader, List[TextLine]]] = []
    skipped: List[str] = []

    for sticker in stickers:
        if sticker.count < 1:
            continue
        try:
            square = _square(sticker.image, sticker.mode,
                             fade=BACK_FADE if sticker.back else 0.0)
        except (OSError, UnidentifiedImageError, ValueError) as error:
            log.warning("skipping cover %r: %s", sticker.label, error)
            skipped.append(sticker.label or "unnamed")
            continue
        lines = back_lines(sticker.back) if sticker.back else []
        prepared.extend([(square, lines)] * sticker.count)

    if not prepared:
        raise ValueError("there is no cover to print")

    _draw(prepared, target)
    return skipped


def text_width_mm() -> float:
    """How wide a line on the text side may be.

    The largest square that fits in a circle has a side of 1/sqrt(2) of its
    diameter, which is the same arithmetic the FIT mode uses for the cover. A
    rectangle wastes a little of the circle and never needs the line length to
    depend on how far up the page the line happens to land.
    """
    return 2 * (DIAMETER_MM / 2 - TEXT_SAFE_MM) * FIT_FACTOR


def back_lines(back: Back, with_tracks: bool = True) -> List[TextLine]:
    """Lay the text side out, deciding here what fits and what is dropped.

    The sheet and the page on screen both call this, so the words a preview
    shows are the words that get printed. A track list longer than the circle
    ends in a count of what was left off rather than being cut mid-word, which
    would read as a rendering fault instead of a deliberate omission.
    """
    width = text_width_mm() * mm
    height = width

    head: List[TextLine] = []
    head += [TextLine(line, TITLE_PT, bold=True)
             for line in _wrap(back.title, TITLE_PT, True, width)]
    if back.series:
        head += [TextLine(line, SERIES_PT)
                 for line in _wrap(back.series, SERIES_PT, False, width)]
    for note in back.notes:
        head += [TextLine(line, NOTE_PT) for line in _wrap(note, NOTE_PT, False, width)]

    if not with_tracks or not back.tracks:
        return head

    # One line per track, each shortened to the width rather than wrapped: a
    # track running over two lines makes the list unreadable as a list.
    numbered = [f"{index}. {track}" for index, track in enumerate(back.tracks, 1)]
    spacer = TextLine("", NOTE_PT * 0.5)

    for keep in range(len(numbered), -1, -1):
        lines = list(head) + [spacer]
        lines += [TextLine(_shorten(text, TRACK_PT, width), TRACK_PT)
                  for text in numbered[:keep]]
        left = len(numbered) - keep
        if left:
            lines.append(TextLine(f"und {left} weitere", TRACK_PT))
        if _stack_height(lines) <= height:
            return lines if keep else head

    return head


def _wrap(text: str, size: float, bold: bool, width: float) -> List[str]:
    """Break ``text`` into lines no wider than ``width``."""
    words = (text or "").split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _width_of(candidate, size, bold) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    # A single word longer than the circle still has to stop somewhere.
    return [line if _width_of(line, size, bold) <= width
            else _shorten(line, size, width) for line in lines]


def _shorten(text: str, size: float, width: float) -> str:
    if _width_of(text, size, False) <= width:
        return text
    while text and _width_of(text + "...", size, False) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _width_of(text: str, size: float, bold: bool = False) -> float:
    return pdfmetrics.stringWidth(text, _font(bold), size)


def _font(bold: bool) -> str:
    return "Helvetica-Bold" if bold else "Helvetica"


def _stack_height(lines: Sequence[TextLine]) -> float:
    return sum(line.size * LEADING for line in lines)


def render_square(source: Union[bytes, Path], mode: str = FILL,
                  size: int = PIXELS, fade: float = 0.0) -> bytes:
    """One sticker's picture as JPEG, exactly as the sheet prints it.

    The screen shows this too, so that choosing between the modes is a matter
    of looking at what will come out of the printer rather than at a separate
    approximation of it.

    ``fade`` mixes that share of white into the result, which is how the text
    side keeps the cover as a watermark without the letters disappearing into
    the artwork.
    """
    handle = io.BytesIO(source) if isinstance(source, bytes) else source
    with Image.open(handle) as image:
        image.load()
        picture = image.convert("RGB")

    edge = min(picture.size)
    width, height = picture.size
    picture = picture.crop((
        (width - edge) // 2,
        (height - edge) // 2,
        (width - edge) // 2 + edge,
        (height - edge) // 2 + edge,
    )).resize((size, size), Image.LANCZOS)

    if mode == FIT:
        picture = _inset(picture)

    if fade:
        picture = Image.blend(picture, Image.new("RGB", picture.size, (255, 255, 255)),
                              min(max(fade, 0.0), 1.0))

    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _square(source: Union[bytes, Path], mode: str = FILL, fade: float = 0.0) -> ImageReader:
    """Render a cover into the square the circle is drawn over."""
    return ImageReader(io.BytesIO(render_square(source, mode, fade=fade)))


def _inset(picture: Image.Image) -> Image.Image:
    """Shrink the cover until all of it is inside the circle, on a blurred copy.

    Filling the circle cuts whatever sits along the edges of the artwork, which
    on an album cover is usually the lettering. This keeps every corner of the
    cover and hides the ring it leaves behind under an enlarged, blurred copy,
    so the sticker still reads as one picture rather than a badge.
    """
    edge = picture.size[0]
    background = picture.filter(ImageFilter.GaussianBlur(max(1, edge // 25)))
    inset = round(edge * FIT_FACTOR)
    foreground = picture.resize((inset, inset), Image.LANCZOS)
    offset = (edge - inset) // 2
    background.paste(foreground, (offset, offset))
    return background


def _draw(images: Sequence[Tuple[ImageReader, Sequence[TextLine]]], target: Path) -> None:
    sheet = canvas.Canvas(str(target), pagesize=A4)
    sheet.setTitle("Traveltonies")

    radius = DIAMETER_MM * mm / 2
    bleed_radius = radius + BLEED_MM * mm
    positions = sticker_positions(len(images))
    page = 0

    for (image, lines), position in zip(images, positions):
        if position.page != page:
            sheet.showPage()
            page = position.page

        # Clipping to the bled circle is what makes the picture round; drawing
        # a square that exactly covers it means no corner can show.
        sheet.saveState()
        path = sheet.beginPath()
        path.circle(position.x, position.y, bleed_radius)
        sheet.clipPath(path, stroke=0)
        sheet.drawImage(
            image,
            position.x - bleed_radius,
            position.y - bleed_radius,
            2 * bleed_radius,
            2 * bleed_radius,
        )
        sheet.restoreState()

        if lines:
            _draw_text(sheet, lines, position)

        sheet.setStrokeGray(CUT_LINE_GREY)
        sheet.setLineWidth(CUT_LINE_WIDTH)
        sheet.circle(position.x, position.y, radius, stroke=1, fill=0)

    sheet.save()


def _draw_text(sheet: canvas.Canvas, lines: Sequence[TextLine], position: Position) -> None:
    """Centre the laid-out lines on the sticker.

    Drawn as PDF text rather than rendered into the picture, so 5.5 pt stays
    sharp at whatever resolution the printer works at.
    """
    sheet.setFillGray(0.1)
    # The stack is centred on the circle, so the first baseline sits half the
    # stack above it, less the ascent of that first line.
    cursor = position.y + _stack_height(lines) / 2
    for line in lines:
        cursor -= line.size * LEADING
        if line.text:
            sheet.setFont(_font(line.bold), line.size)
            sheet.drawCentredString(position.x, cursor, line.text)
