"""Builds the printable sheet of round cover stickers.

A traveltonie is an NFC tag with the album cover stuck on it. The sheet carries
nothing but the pictures and what to cut along, because everything printed
inside the circle ends up on the tag. How big the covers are and how they sit
on the page is a Layout: one for cutting with scissors, one for a circle punch.

Three decisions are worth knowing before changing anything here:

- The picture is printed larger than the circle is cut. Scissors wander and a
  punch sits a little off, and a cut that misses must still land on the cover
  rather than on white paper. The bleed has to stay smaller than half the gap,
  or neighbouring covers print into each other.
- A cover is never scaled non-uniformly. A stretched face is unmissable on a
  46 mm circle, so a picture that is not square is cropped to a centred square
  first and loses its edges instead.
- A square cover does not fit inside a circle: filling the circle cuts whatever
  runs along the edges of the artwork, which on an album cover is usually the
  lettering. ``FILL`` accepts that and prints edge to edge; ``FIT`` shrinks the
  cover until all of it is inside the cut and hides the ring under a blurred
  copy. Both
  are honest answers to the same geometry and the choice is a matter of taste.
"""

from __future__ import annotations

import io
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

from PIL import Image, UnidentifiedImageError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from .i18n import DEFAULT, Translator
from .pictures import centre_square, inset, on_white

log = logging.getLogger(__name__)

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

# What a layout prints to cut along: the circle itself, for scissors, or the
# squares around each cover, cut apart first and then put into a punch.
CUT_CIRCLE = "circle"
CUT_SQUARES = "squares"

# What a printer resolves; more pixels only inflate the file.
DPI = 300


@dataclass(frozen=True)
class Layout:
    """How covers are cut and arranged on an A4 page.

    Every step that draws, previews or lays out a cover reads its sizes from
    the layout it is given, so a second way of cutting is a second Layout and
    not a second copy of the code.
    """

    key: str
    # The finished cover: what scissors or a punch cut out.
    diameter_mm: float
    # How far the picture runs past the cut, per side. Smaller than half the
    # space between two covers, or neighbours print into each other.
    bleed_mm: float
    columns: int
    rows: int
    # From the centre of one cover to the centre of the next.
    pitch_mm: float
    cut: str

    @property
    def per_page(self) -> int:
        return self.columns * self.rows

    @property
    def picture_mm(self) -> float:
        """The printed circle: the cover and its bleed."""
        return self.diameter_mm + 2 * self.bleed_mm

    @property
    def pixels(self) -> int:
        return round(self.picture_mm / 25.4 * DPI)

    @property
    def fit_factor(self) -> float:
        """How far FIT shrinks the picture so all of it lands inside the cut, not the bleed."""
        return FIT_FACTOR * self.diameter_mm / self.picture_mm


# Cut out with scissors along the printed circle; 46 mm covers a tag. The
# 3 mm between two covers is what the scissors need.
SHEET = Layout("sheet", diameter_mm=46, bleed_mm=1, columns=4, rows=5, pitch_mm=49,
               cut=CUT_CIRCLE)

# For a 44.5 mm circle punch, which reaches only a little way in from the
# edge of the paper: each cover is cut out as a square first. The frame of
# 5.75 mm around the circle is what the punch holds while it cuts, and what a
# hand holds while centring the square by eye; a narrower one tears or bends.
# Squares share their edges, so one cut parts two of them. 3 by 5 is what
# fits on A4 with that frame.
PUNCH = Layout("punch", diameter_mm=44.5, bleed_mm=1, columns=3, rows=5, pitch_mm=56,
               cut=CUT_SQUARES)

LAYOUTS = {layout.key: layout for layout in (SHEET, PUNCH)}

# What a cover is kept and rendered at: enough for the largest printed circle.
PIXELS = max(layout.pixels for layout in LAYOUTS.values())


@dataclass
class Back:
    """What the text side of a tonie says: its name, short notes and its tracks.

    The text arrives already written; this module lays it out and adds only
    the count of tracks that did not fit.
    """

    title: str
    series: str = ""
    # Short facts under the name, one per line: the year and playing time, then
    # the model number that finds the tonie again in the app.
    notes: Sequence[str] = ()
    tracks: Sequence[str] = ()
    # The language of whatever this module adds to the text, such as the count
    # of tracks left off; the rest arrives already written.
    language: str = DEFAULT


@dataclass(frozen=True)
class TextLine:
    """One laid-out line, as both the sheet and the screen have to show it."""

    text: str
    size: float
    bold: bool = False


@dataclass
class Sticker:
    """One motif and how many copies of it to print."""

    # The picture, or what fetches it. A fetcher is called only when the
    # sheet gets to this motif, so a sheet of hundreds holds one original at
    # a time rather than all of them; it answers None when there is none.
    image: Union[bytes, Path, Callable[[], Optional[bytes]]]
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


def sticker_positions(count: int, shift_mm: Tuple[float, float] = (0.0, 0.0),
                      layout: Layout = SHEET) -> List[Position]:
    """Centres for ``count`` stickers, left to right and top to bottom.

    The grid is centred on the page, so the margins come out the same on every
    side and a printer's unprintable edge cannot clip a sticker. ``shift_mm``
    moves the whole grid right and down by that many millimetres, to cancel a
    printer that puts everything slightly off where it was asked to.
    """
    shift_x, shift_down = shift_mm
    page_width, page_height = A4
    pitch = layout.pitch_mm * mm
    # From the first centre to the last, in each direction.
    span_x = (layout.columns - 1) * pitch
    span_y = (layout.rows - 1) * pitch

    left = (page_width - span_x) / 2
    # PDF coordinates grow upwards, so the first row is the highest one.
    top = (page_height + span_y) / 2

    positions = []
    for index in range(count):
        slot = index % layout.per_page
        positions.append(Position(
            page=index // layout.per_page,
            x=left + (slot % layout.columns) * pitch + shift_x * mm,
            # PDF coordinates grow upwards, so down on paper is a smaller y.
            y=top - (slot // layout.columns) * pitch - shift_down * mm,
        ))
    return positions


def build_sheet(stickers: Sequence[Sticker], target: Path,
                shift_mm: Tuple[float, float] = (0.0, 0.0),
                progress: Optional[Callable[[int, int], None]] = None,
                layout: Layout = SHEET) -> List[str]:
    """Write the sheet to ``target`` and return the motifs that had to be skipped.

    A cover that cannot be decoded is left out rather than failing the sheet:
    the other stickers are still worth printing, and the caller is told which
    one to look at. ``progress`` hears how many motifs of how many are ready.
    """
    prepared: List[Tuple[str, List[TextLine]]] = []
    skipped: List[str] = []
    wanted = [sticker for sticker in stickers if sticker.count >= 1]

    # Each rendered square waits here as a JPEG file. Handed a file, reportlab
    # puts the JPEG into the PDF as it is; handed the picture in memory, it
    # decodes it to raw pixels and holds those until the PDF is saved, about
    # 1 MB a motif, which a sheet of every tonie turns into most of a gigabyte.
    with tempfile.TemporaryDirectory(prefix="sheet-") as staging:
        for done, sticker in enumerate(wanted, 1):
            try:
                source = sticker.image() if callable(sticker.image) else sticker.image
                if source is None:
                    raise ValueError("no picture")
                square = Path(staging) / f"{done}.jpg"
                square.write_bytes(render_square(source, sticker.mode,
                                                 fade=BACK_FADE if sticker.back else 0.0,
                                                 layout=layout))
            except (OSError, UnidentifiedImageError, ValueError) as error:
                log.warning("skipping cover %r: %s", sticker.label, error)
                skipped.append(sticker.label or "unnamed")
                continue
            finally:
                if progress:
                    progress(done, len(wanted))
            lines = back_lines(sticker.back, layout=layout) if sticker.back else []
            prepared.extend([(str(square), lines)] * sticker.count)

        if not prepared:
            raise ValueError("there is no cover to print")

        _draw(prepared, target, shift_mm, layout)
    return skipped


def text_width_mm(layout: Layout = SHEET) -> float:
    """How wide a line on the text side may be.

    The largest square that fits in a circle has a side of 1/sqrt(2) of its
    diameter, which is the same arithmetic the FIT mode uses for the cover. A
    rectangle wastes a little of the circle and never needs the line length to
    depend on how far up the page the line happens to land.
    """
    return 2 * (layout.diameter_mm / 2 - TEXT_SAFE_MM) * FIT_FACTOR


def back_lines(back: Back, with_tracks: bool = True, layout: Layout = SHEET) -> List[TextLine]:
    """Lay the text side out, deciding here what fits and what is dropped.

    The sheet and the page on screen both call this, so the words a preview
    shows are the words that get printed. A track list longer than the circle
    ends in a count of what was left off rather than being cut mid-word, which
    would read as a rendering fault instead of a deliberate omission.
    """
    width = text_width_mm(layout) * mm
    height = width

    head: List[TextLine] = []
    head += [TextLine(line, TITLE_PT, bold=True)
             for line in wrap_text(back.title, TITLE_PT, True, width)]
    if back.series:
        head += [TextLine(line, SERIES_PT)
                 for line in wrap_text(back.series, SERIES_PT, False, width)]
    for note in back.notes:
        head += [TextLine(line, NOTE_PT) for line in wrap_text(note, NOTE_PT, False, width)]

    if not with_tracks or not back.tracks:
        return head

    # One line per track, each shortened to the width rather than wrapped: a
    # track running over two lines makes the list unreadable as a list.
    numbered = [f"{index}. {track}" for index, track in enumerate(back.tracks, 1)]
    spacer = TextLine("", NOTE_PT * 0.5)

    for keep in range(len(numbered), -1, -1):
        lines = list(head) + [spacer]
        lines += [TextLine(shorten_text(text, TRACK_PT, width), TRACK_PT)
                  for text in numbered[:keep]]
        left = len(numbered) - keep
        if left:
            lines.append(TextLine(Translator(back.language)("pdf.more_tracks", count=left),
                                  TRACK_PT))
        if _stack_height(lines) <= height:
            return lines if keep else head

    return head


def wrap_text(text: str, size: float, bold: bool, width: float) -> List[str]:
    """Break ``text`` into lines no wider than ``width``."""
    words = (text or "").split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(candidate, size, bold) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    # A single word longer than the circle still has to stop somewhere.
    return [line if text_width(line, size, bold) <= width
            else shorten_text(line, size, width, bold) for line in lines]


def shorten_text(text: str, size: float, width: float, bold: bool = False) -> str:
    """Cut ``text`` to ``width`` and mark the cut, measured in the face it is set in."""
    if text_width(text, size, bold) <= width:
        return text
    while text and text_width(text + "...", size, bold) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def text_width(text: str, size: float, bold: bool = False) -> float:
    return pdfmetrics.stringWidth(text, font_name(bold), size)


def font_name(bold: bool) -> str:
    return "Helvetica-Bold" if bold else "Helvetica"


def _stack_height(lines: Sequence[TextLine]) -> float:
    return sum(line.size * LEADING for line in lines)


def render_square(source: Union[bytes, Path], mode: str = FILL, size: Optional[int] = None,
                  fade: float = 0.0, layout: Layout = SHEET) -> bytes:
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
        picture = on_white(image)

    picture = centre_square(picture, size or layout.pixels)
    if mode == FIT:
        picture = inset(picture, layout.fit_factor)

    if fade:
        picture = Image.blend(picture, Image.new("RGB", picture.size, (255, 255, 255)),
                              min(max(fade, 0.0), 1.0))

    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _draw(images: Sequence[Tuple[str, Sequence[TextLine]]], target: Path,
          shift_mm: Tuple[float, float] = (0.0, 0.0), layout: Layout = SHEET) -> None:
    sheet = canvas.Canvas(str(target), pagesize=A4)
    sheet.setTitle("Traveltonies")

    radius = layout.diameter_mm * mm / 2
    bleed_radius = radius + layout.bleed_mm * mm
    positions = sticker_positions(len(images), shift_mm, layout)
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
        if layout.cut == CUT_SQUARES:
            # The punch makes the edge of the cover; a printed circle would
            # only show as a ring wherever the punch sits a little off.
            half = layout.pitch_mm * mm / 2
            sheet.rect(position.x - half, position.y - half, 2 * half, 2 * half, stroke=1, fill=0)
        else:
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
            sheet.setFont(font_name(line.bold), line.size)
            sheet.drawCentredString(position.x, cursor, line.text)
