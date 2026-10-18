"""Pictures passed through from teddyCloud, which the browser cannot reach itself."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import Response

from ..models import CACHE, COVER_TYPES, cache_name, fetch_cover, is_cover_name
from ..sheet import FILL, FIT, PIXELS, render_square
from ..teddycloud import TeddyCloudError
from .common import Context, Refused, refusing

log = logging.getLogger(__name__)

# A picture is served as the type its name says and nothing else; a browser
# that guessed from the content could run a file as a page.
NO_SNIFFING = {"X-Content-Type-Options": "nosniff"}

def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    client = ctx.client

    @app.get("/cover/cache/{name}")
    def cached_picture(name: str, mode: Optional[str] = None, size: int = 0):
        """Pass a picture through from teddyCloud's image cache, optionally as the sticker.

        teddyCloud fetches a picture into its cache the first time it is asked
        for one, and answers a cover of this app's own with a redirect to
        custom_img, which the request follows.
        """
        if cache_name(f"{CACHE}{name}") != name:
            raise Refused(400, "error.not_an_image")
        with refusing(TeddyCloudError, status=502):
            data = client().fetch(f"{CACHE}{name}")
        return _picture_response(data, name, mode, size)

    @app.get("/cover/custom/{name}")
    def custom_cover(name: str, mode: Optional[str] = None, size: int = 0):
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
        with refusing(TeddyCloudError, status=502):
            data = fetch_cover(client(), name)

        return _picture_response(data, name, mode, size)


def _picture_response(data: bytes, name: str, mode: Optional[str], size: int) -> Response:
    """A picture as it is, or, with a ``mode``, rendered the way the sheet prints it."""
    if mode in (FILL, FIT):
        try:
            data = render_square(data, mode, size=max(64, min(size or PIXELS, PIXELS)))
        except (OSError, ValueError) as error:
            # A cover teddyCloud holds but Pillow cannot read still has to
            # reach the page; the untouched bytes may well render there.
            log.debug("cannot render %s as a sticker: %s", name, error)
        else:
            return Response(data, media_type="image/jpeg", headers=NO_SNIFFING)
    return Response(data, media_type=COVER_TYPES.get(Path(name).suffix.lower(), "image/jpeg"),
                    headers=NO_SNIFFING)
