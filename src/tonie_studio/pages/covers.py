"""Pictures passed through from teddyCloud, which the browser cannot reach itself."""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ..models import CACHE, COVER_TYPES, cache_name, fetch_cover, is_cover_name
from ..pictures import MAX_SIZE, shrink
from ..sheet import FILL, FIT, PIXELS, render_square
from .common import Context, Refused

log = logging.getLogger(__name__)

# A picture is served as the type its name says and nothing else; a browser
# that guessed from the content could run a file as a page.
NO_SNIFFING = {"X-Content-Type-Options": "nosniff"}

# How long a browser may keep a cover without asking, in seconds. A picture
# in teddyCloud's cache is named after the address it came from, which a shop
# does not reuse for another picture; a cover of this app's own changes when
# someone picks a new one. After that the browser asks with the ETag and gets
# a bodiless 304 for a cover that is unchanged.
KEEP_CACHED = 7 * 86400
KEEP_CUSTOM = 3600

# Rendered sizes and sticker previews, by the cover's ETag and what was asked:
# hovering over a row asks for the same rendering again and again. A few
# hundred small JPEGs, some ten megabytes.
RENDERED_ENTRIES = 400
_rendered: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()
_rendered_lock = Lock()


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    client = ctx.client

    @app.get("/cover/cache/{name}")
    def cached_picture(request: Request, name: str, mode: Optional[str] = None, size: int = 0):
        """Pass a picture through from teddyCloud's image cache, optionally as the sticker.

        teddyCloud fetches a picture into its cache the first time it is asked
        for one, and answers a cover of this app's own with a redirect to
        custom_img, which the request follows.
        """
        if cache_name(f"{CACHE}{name}") != name:
            raise Refused(400, "error.not_an_image")
        data = client().fetch(f"{CACHE}{name}")
        return _picture_response(request, data, name, mode, size, KEEP_CACHED)

    @app.get("/cover/custom/{name}")
    def custom_cover(request: Request, name: str, mode: Optional[str] = None, size: int = 0):
        """Pass a cover through from teddyCloud, optionally as the sticker.

        The browser can reach this app but not teddyCloud's own address, which
        sits behind the gateway under a different name, so linking straight to
        the image would give a broken picture.

        With a ``mode`` the picture comes back the way the sheet would print it
        in that mode, so a preview on screen and the sticker in hand are the
        same rendering rather than two that merely resemble each other.
        """
        if "/" in name or ".." in name or not is_cover_name(name):
            raise Refused(400, "error.not_an_image")
        data = fetch_cover(client(), name)
        return _picture_response(request, data, name, mode, size, KEEP_CUSTOM)


def _picture_response(request: Request, data: bytes, name: str, mode: Optional[str],
                      size: int, keep: int) -> Response:
    """The picture as it is, made smaller with a ``size``, or rendered as the sheet prints it.

    The ETag names the picture's bytes and what was asked of them, so a
    browser that has it already gets a 304 without anything being rendered.
    """
    mode = mode if mode in (FILL, FIT) else ""
    size = max(64, min(size or PIXELS, PIXELS)) if mode else max(0, min(size, MAX_SIZE))
    etag = f'"{hashlib.sha256(data).hexdigest()[:20]}-{mode}-{size}"'
    headers = {**NO_SNIFFING, "ETag": etag, "Cache-Control": f"private, max-age={keep}"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    body, media_type = _render(data, name, mode, size, etag)
    return Response(body, media_type=media_type, headers=headers)


def _render(data: bytes, name: str, mode: str, size: int, etag: str) -> Tuple[bytes, str]:
    """What _picture_response sends: kept by ETag, so a second ask renders nothing."""
    as_is = (data, COVER_TYPES.get(Path(name).suffix.lower(), "image/jpeg"))
    if not mode and not size:
        return as_is
    with _rendered_lock:
        if etag in _rendered:
            _rendered.move_to_end(etag)
            return _rendered[etag]
    try:
        made = ((render_square(data, mode, size=size), "image/jpeg") if mode
                else shrink(data, size))
    except (OSError, ValueError) as error:
        # A cover teddyCloud holds but Pillow cannot read still has to
        # reach the page; the untouched bytes may well render there.
        log.debug("cannot render %s: %s", name, error)
        return as_is
    with _rendered_lock:
        _rendered[etag] = made
        while len(_rendered) > RENDERED_ENTRIES:
            _rendered.popitem(last=False)
    return made
