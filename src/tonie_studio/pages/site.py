"""What every page shares: answers for errors, the language switch and the health check."""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..i18n import COOKIE as LANG_COOKIE
from ..i18n import catalogs
from ..teddycloud import TeddyCloudError
from .common import Context, Refused, translator

log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Context) -> None:
    """Add the app-wide routes and error answers to ``app``."""
    templates = ctx.templates

    @app.exception_handler(TeddyCloudError)
    def teddycloud_unreachable(request: Request, error: TeddyCloudError):
        """Answer a page, not a stack trace, when teddyCloud is down.

        Half the app keeps working without it, so an outage should read as a
        service being away rather than as this one being broken.
        """
        log.warning("teddyCloud unreachable while serving %s: %s", request.url.path, error)
        return templates.TemplateResponse(
            request, "error.html", {"message": str(error)}, status_code=503,
        )

    @app.exception_handler(Refused)
    def refused(request: Request, error: Refused):
        """Say why a request was turned down, in the reader's language."""
        message = error.say(translator(request))
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=error.status)
        return templates.TemplateResponse(
            request, "refused.html", {"message": message}, status_code=error.status,
        )

    @app.get("/lang/{code}")
    def choose_language(code: str, next: str = "/"):
        """Remember the language chosen and go back to where it was chosen."""
        if code not in catalogs():
            raise Refused(404, "error.no_language")
        # Only a path on this site: an address from elsewhere in ``next``
        # would make this an open redirect.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, 303)
        response.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
        return response

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}
