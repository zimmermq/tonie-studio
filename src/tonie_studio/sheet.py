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
from typing import List, Optional, Sequence, Union

from PIL import Image, ImageFilter, UnidentifiedImageError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
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

# Barely visible, but enough to cut along.
CUT_LINE_GREY = 0.75
CUT_LINE_WIDTH = 0.25


@dataclass
class Sticker:
    """One motif and how many copies of it to print."""

    image: Union[bytes, Path]
    count: int = 1
    mode: str = FILL
    # Only used to name the motif in a message about a picture that failed.
    label: str = ""


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
    prepared: List[ImageReader] = []
    skipped: List[str] = []

    for sticker in stickers:
        if sticker.count < 1:
            continue
        try:
            square = _square(sticker.image, sticker.mode)
        except (OSError, UnidentifiedImageError, ValueError) as error:
            log.warning("skipping cover %r: %s", sticker.label, error)
            skipped.append(sticker.label or "unnamed")
            continue
        prepared.extend([square] * sticker.count)

    if not prepared:
        raise ValueError("there is no cover to print")

    _draw(prepared, target)
    return skipped


def _square(source: Union[bytes, Path], mode: str = FILL) -> ImageReader:
    """Render a cover into the square the circle is drawn over."""
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
    )).resize((PIXELS, PIXELS), Image.LANCZOS)

    if mode == FIT:
        picture = _inset(picture)

    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=92)
    buffer.seek(0)
    return ImageReader(buffer)


def _inset(picture: Image.Image) -> Image.Image:
    """Shrink the cover until all of it is inside the circle, on a blurred copy.

    Filling the circle cuts whatever sits along the edges of the artwork, which
    on an album cover is usually the lettering. This keeps every corner of the
    cover and hides the ring it leaves behind under an enlarged, blurred copy,
    so the sticker still reads as one picture rather than a badge.
    """
    background = picture.filter(ImageFilter.GaussianBlur(PIXELS // 25))
    inset = round(PIXELS * FIT_FACTOR)
    foreground = picture.resize((inset, inset), Image.LANCZOS)
    offset = (PIXELS - inset) // 2
    background.paste(foreground, (offset, offset))
    return background


def _draw(images: Sequence[ImageReader], target: Path) -> None:
    sheet = canvas.Canvas(str(target), pagesize=A4)
    sheet.setTitle("Traveltonies")

    radius = DIAMETER_MM * mm / 2
    bleed_radius = radius + BLEED_MM * mm
    positions = sticker_positions(len(images))
    page = 0

    for image, position in zip(images, positions):
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

        sheet.setStrokeGray(CUT_LINE_GREY)
        sheet.setLineWidth(CUT_LINE_WIDTH)
        sheet.circle(position.x, position.y, radius, stroke=1, fill=0)

    sheet.save()
