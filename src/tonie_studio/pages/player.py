"""The library's audio played in the browser, passed through from teddyCloud."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ..encode import TAF_SUFFIX
from ..library import safe_library_path
from ..teddycloud import TeddyCloudError
from .common import Context, Refused


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""

    @app.get("/audio/{path:path}")
    def audio(request: Request, path: str):
        """A library taf as Ogg audio, for the player.

        The browser cannot reach teddyCloud itself. Ranges are passed on, as
        a browser seeks in Ogg by asking for a part of the file.
        """
        path = "/" + path
        if not path.lower().endswith(TAF_SUFFIX) or not safe_library_path(path):
            raise Refused(400, "player.not_a_taf")
        try:
            played = ctx.client().audio(path, request.headers.get("range"))
        except TeddyCloudError as error:
            if error.status == 404:
                raise Refused(404, "player.not_found") from error
            raise
        headers = {"Accept-Ranges": "bytes"}
        if played.length is not None:
            headers["Content-Length"] = str(played.length)
        if played.content_range:
            headers["Content-Range"] = played.content_range

        def chunks():
            try:
                yield from played.chunks
            finally:
                played.close()
        return StreamingResponse(chunks(), status_code=played.status, media_type="audio/ogg",
                                 headers=headers)
