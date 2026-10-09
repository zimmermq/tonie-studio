"""Square pictures made from covers: previews for the picker, the crop the
sticker prints, and the blurred ring a whole cover is set into.

The covers in the archive are around 130 KB each. Sixteen of them on one page
is two megabytes of JPEG to show pictures the size of a stamp, so the list gets
thumbnails instead.

They are cached in memory and keyed by the file's modification time, so a cover
replaced on disk produces a new thumbnail without anyone having to clear
anything. The cache is small on purpose: it holds an archive's worth of
stamp-sized pictures, not a copy of the artwork.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple

from PIL import Image, ImageFilter, UnidentifiedImageError

log = logging.getLogger(__name__)

# Rendered at twice the display size so the list stays sharp on a retina screen.
DEFAULT_SIZE = 128

MAX_SIZE = 512

# How far out the cover may be zoomed. Below this the cover is a stamp in the
# middle of a blurred disc, which is no longer a picture of the album.
MIN_ZOOM = 0.5

# Roughly four archives' worth. Each entry is a few kilobytes.
CACHE_ENTRIES = 200

_cache: "OrderedDict[Tuple[str, float, int], bytes]" = OrderedDict()
_lock = Lock()


def thumbnail(path: Path, size: int = DEFAULT_SIZE) -> Optional[bytes]:
    """A square JPEG preview of ``path``, or None when it cannot be read.

    Cropped to a centred square rather than letterboxed, so a row of covers
    lines up. A cover that cannot be decoded is not an error worth showing: the
    row simply has no picture.
    """
    size = max(16, min(size, MAX_SIZE))
    try:
        stamp = path.stat().st_mtime
    except OSError as error:
        log.debug("cannot stat %s: %s", path, error)
        return None

    key = (str(path), stamp, size)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached

    try:
        rendered = _render(path, size)
    except (OSError, UnidentifiedImageError, ValueError) as error:
        log.debug("cannot read cover %s: %s", path, error)
        return None

    with _lock:
        _cache[key] = rendered
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
    return rendered


def crop(
    path: Path,
    size: int,
    zoom: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> Optional[bytes]:
    """Render a cover as the square that goes under the round sticker.

    A sticker is a circle and a cover is a square, so filling the circle always
    cuts something - usually the lettering along the top edge. Shifting decides
    what the circle keeps, and zooming out below 1.0 keeps all of it by
    shrinking the cover inside the square, with the gap filled by a blurred,
    enlarged copy of itself rather than a hard edge.

    ``zoom`` is 1.0 for the whole square; ``offset_x``/``offset_y`` are
    fractions of the visible window, clamped so the crop cannot leave the
    picture, and have nothing to move below 1.0 because the whole cover is
    already inside.
    """
    zoom = max(MIN_ZOOM, min(float(zoom), 3.0))
    try:
        with Image.open(path) as image:
            image.load()
            picture = image.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError) as error:
        log.debug("cannot read cover %s: %s", path, error)
        return None

    edge = min(picture.size)
    # Below 1.0 the whole square is kept and the shrinking happens afterwards,
    # so only zooming in narrows the window.
    window = edge / max(zoom, 1.0)
    slack = (edge - window) / 2
    centre_x = (picture.size[0] - edge) / 2 + slack + _clamp(offset_x) * slack
    centre_y = (picture.size[1] - edge) / 2 + slack + _clamp(offset_y) * slack

    picture = picture.crop((
        round(centre_x), round(centre_y),
        round(centre_x + window), round(centre_y + window),
    )).resize((size, size), Image.LANCZOS)

    if zoom < 1.0:
        picture = inset(picture, zoom)

    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def inset(square: Image.Image, share: float) -> Image.Image:
    """Shrink a square picture to ``share`` of its edge, over a blurred copy of itself.

    A cover placed whole inside the circle leaves a ring, and a flat colour
    there reads as a mistake. The blurred copy carries the cover's own colours
    to the edge, so the ring looks intended and the print bleeds cleanly.
    """
    size = square.size[0]
    background = square.filter(ImageFilter.GaussianBlur(max(1, size // 25)))
    shrunk = max(1, round(size * share))
    foreground = square.resize((shrunk, shrunk), Image.LANCZOS)
    background.paste(foreground, ((size - shrunk) // 2, (size - shrunk) // 2))
    return background


def _clamp(value: float) -> float:
    return max(-1.0, min(float(value), 1.0))


def centre_square(picture: Image.Image, size: int) -> Image.Image:
    """The largest centred square of ``picture``, scaled to ``size`` pixels."""
    edge = min(picture.size)
    width, height = picture.size
    left, top = (width - edge) // 2, (height - edge) // 2
    return picture.crop((left, top, left + edge, top + edge)).resize((size, size), Image.LANCZOS)


def _render(path: Path, size: int) -> bytes:
    with Image.open(path) as image:
        image.load()
        picture = image.convert("RGB")

    buffer = io.BytesIO()
    centre_square(picture, size).save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()
