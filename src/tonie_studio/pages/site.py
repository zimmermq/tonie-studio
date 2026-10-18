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

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# What Sec-Fetch-Site says for a request a page outside this app started. A
# neighbouring subdomain is another app, so "same-site" counts as outside too.
OTHER_SITES = frozenset({"cross-site", "same-site"})


def register(app: FastAPI, ctx: Context) -> None:
    """Add the app-wide routes and error answers to ``app``."""
    templates = ctx.templates

    @app.exception_handler(TeddyCloudError)
    def teddycloud_unreachable(request: Request, error: TeddyCloudError):
        """Answer a page, not a stack trace, when teddyCloud is down.

        Half the app keeps working without it, so an outage should read as a
        service being away rather than as this one being broken.
        """
        if error.refused:
            # teddyCloud answered and said no: it is there, so calling this an
            # outage would send the reader looking for the wrong problem.
            log.warning("teddyCloud refused while serving %s: %s", request.url.path, error)
            message = translator(request)("error.teddycloud_refused", detail=str(error))
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": message}, status_code=502)
            return templates.TemplateResponse(
                request, "refused.html", {"message": message}, status_code=502,
            )
        log.warning("teddyCloud unreachable while serving %s: %s", request.url.path, error)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(error)}, status_code=503)
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

    @app.middleware("http")
    async def same_site_only(request: Request, call_next):
        """Refuse a change asked for by a page on another site.

        A page anywhere can make a visitor's browser post a form here, and the
        login in front of the app does not tell those posts apart. The browser
        does: it names where a request came from in Sec-Fetch-Site. A browser
        that does not send it, or a client without one, is let through.
        """
        if request.method not in SAFE_METHODS and \
                request.headers.get("sec-fetch-site") in OTHER_SITES:
            log.warning("refused a %s to %s from another site", request.method, request.url.path)
            return refused(request, Refused(403, "error.cross_site"))
        return await call_next(request)

    @app.get("/lang/{code}")
    def choose_language(code: str, next: str = "/"):
        """Remember the language chosen and go back to where it was chosen."""
        if code not in catalogs():
            raise Refused(404, "error.no_language")
        # Only a path on this site: an address from elsewhere in ``next``
        # would make this an open redirect. Browsers read a backslash as a
        # slash, so "/\evil.example" is as much another site as "//evil.example".
        local = next.startswith("/") and not next.startswith("//") and "\\" not in next
        target = next if local else "/"
        response = RedirectResponse(target, 303)
        response.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
        return response

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}
