"""The tonies, the box, radio tonies and the library of tafs."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..albumnotes import forget_model
from ..encode import EncodeError, delete_taf, list_tafs
from ..library import same
from ..models import (CACHE, CATEGORIES, COVER_TYPES, DEFAULT_CATEGORY, cache_name, fetch_cover,
                      find_model, images_cached, is_cover_name, update_model)
from ..podcast import load_subscriptions, remove_subscription
from ..runtimes import forget
from ..sheet import FILL, FIT, PIXELS, render_square
from ..tags import TonieError, list_tags
from ..teddycloud import TeddyCloudError
from ..tonie import copy_tag, plan_removal, publish_stream, remove_tonie
from .common import Context, Refused

log = logging.getLogger(__name__)

# A picture is served as the type its name says and nothing else; a browser
# that guessed from the content could run a file as a page.
NO_SNIFFING = {"X-Content-Type-Options": "nosniff"}

# How long the name of the tonie on the box is reused, in seconds. A tonie
# renamed or given a new sticker shows its new name this much later in the header.
LABEL_TTL = 10.0

# Offered as a starting point, not as an endorsement: a stream URL is hard to
# find and easy to get wrong, and a wrong one fails silently at the box.
RADIO_SUGGESTIONS = [
    ("KiRaKa (WDR)", "https://wdrhoerenlive-lh.akamaihd.net/i/wdrhoerenlive_kiraka@2334/master.m3u8"),
    ("Radio TEDDY", "https://stream.radioteddy.de/radioteddy/mp3-192/radioteddy"),
    ("Kinderradio Bayern 2 Klaro", "https://dispatcher.rndfnk.com/br/br2/live/mp3/mid"),
]


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    box = ctx.box
    client = ctx.client

    @app.get("/tonies", response_class=HTMLResponse)
    def tonies(request: Request):
        connection = client()
        return templates.TemplateResponse(request, "tonies.html", {
            "tags": list_tags(connection),
            "box": box.state,
            "images_cached": images_cached(connection),
        })


    @app.get("/tonies/{ruid}", response_class=HTMLResponse)
    def tonie_page(request: Request, ruid: str):
        connection = client()
        tags = list_tags(connection)
        tag = next((candidate for candidate in tags if candidate.ruid == ruid), None)
        if tag is None:
            raise Refused(404, "error.no_tag")
        return templates.TemplateResponse(request, "tonie.html", {
            "tag": tag,
            "model": find_model(connection, tag.model) if tag.model else None,
            "categories": CATEGORIES,
            # Where a copy could go, the empty stickers first.
            "others": [other for other in tags if other.ruid != ruid],
            # What the delete button would take with it, shown before it is pressed.
            "removal": plan_removal(connection, ruid) if tag.assigned or tag.model else None,
        })


    @app.post("/tonies/{ruid}/copy")
    def copy_tonie(ruid: str, target: str = Form(...)):
        try:
            copy_tag(client(), ruid, target)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)


    @app.post("/tonies/{ruid}")
    def edit_tonie(
        request: Request,
        ruid: str,
        series: str = Form(""),
        episode: str = Form(""),
        category: str = Form(DEFAULT_CATEGORY),
        cover: Optional[UploadFile] = File(None),
    ):
        connection = client()
        tag = next((candidate for candidate in list_tags(connection)
                    if candidate.ruid == ruid), None)
        if tag is None or not tag.model:
            raise Refused(404, "error.no_model")

        picture, suffix = _uploaded_cover(cover)

        try:
            update_model(connection, tag.model, series=series, episode=episode,
                         category=category, cover=picture, cover_name=suffix)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)


    @app.post("/tonies/{ruid}/delete")
    def delete_tonie(ruid: str):
        """Free the sticker and delete what only it used: model, taf, its podcast."""
        try:
            removal = remove_tonie(client(), ruid)
        except (TonieError, EncodeError) as error:
            raise Refused(400, "error.failed", detail=error) from error
        if removal.model:
            forget(settings.data_dir, removal.model)
            forget_model(settings.data_dir, removal.model)
        # A subscription would only fail on the emptied sticker from now on.
        for subscription in load_subscriptions(settings.data_dir):
            if subscription.ruid == ruid:
                remove_subscription(settings.data_dir, subscription.id)
        return RedirectResponse("/tonies", 303)


    labels: Dict[str, Tuple[float, str]] = {}

    def box_label(ruid: str) -> str:
        """The name of the tonie on the box, read from the tag index at most every LABEL_TTL.

        Every open page polls the box every few seconds; reading the whole tag
        index for each poll would put teddyCloud under constant load for a
        name that rarely changes.
        """
        now = time.monotonic()
        cached = labels.get(ruid)
        if cached and now - cached[0] < LABEL_TTL:
            return cached[1]
        try:
            label = next((tag.label for tag in list_tags(client()) if tag.ruid == ruid), "")
        except TeddyCloudError:
            return ""
        labels.clear()
        labels[ruid] = (now, label)
        return label

    @app.get("/api/box")
    def box_state():
        """What is on the box, for the page to poll.

        Carries the tonie's name as well as its ruid: the header says what is
        playing, and looking that up per page would mean every page knowing how
        to read the tag index.
        """
        state = box.state
        return JSONResponse({
            "label": box_label(state.ruid) if state.known and state.on_box else "",
            "ruid": state.ruid if state.known else None,
            "on_box": state.on_box and state.known,
            "playing": state.playing and state.known,
            "since": state.since.isoformat() if state.since and state.known else None,
            "stale": state.stale,
            "connected": state.connected,
            # Why it is not: teddyCloud is up, but its event stream went quiet.
            "silent": state.silent and not state.connected,
            # A tag teddyCloud does not know: a fresh sticker, just held
            # against the box. The assignment page waits for this.
            "unknown_ruid": state.fresh_unknown,
            "unknown_since": state.unknown_since.isoformat() if state.unknown_since else None,
        })


    @app.get("/cover/cache/{name}")
    def cached_picture(name: str, mode: Optional[str] = None, size: int = 0):
        """Pass a picture through from teddyCloud's image cache, optionally as the sticker.

        teddyCloud fetches a picture into its cache the first time it is asked
        for one, and answers a cover of this app's own with a redirect to
        custom_img, which the request follows.
        """
        if cache_name(f"{CACHE}{name}") != name:
            raise Refused(400, "error.not_an_image")
        try:
            data = client().fetch(f"{CACHE}{name}")
        except TeddyCloudError as error:
            raise Refused(502, "error.failed", detail=error) from error
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
        try:
            data = fetch_cover(client(), name)
        except TeddyCloudError as error:
            raise Refused(502, "error.failed", detail=error) from error

        return _picture_response(data, name, mode, size)


    @app.get("/radio", response_class=HTMLResponse)
    def radio_page(request: Request):
        connection = client()
        tags = list_tags(connection)
        return templates.TemplateResponse(request, "radio.html", {
            "tags": tags,
            # Not "://" in source: a library path is lib:// and would match.
            "streams": [tag for tag in tags
                        if tag.source.lower().startswith(("http://", "https://"))],
            "categories": CATEGORIES,
            "stations": RADIO_SUGGESTIONS,
        })


    @app.post("/radio")
    def make_radio(
        ruid: str = Form(...),
        url: str = Form(...),
        series: str = Form(""),
        episode: str = Form(""),
        category: str = Form(DEFAULT_CATEGORY),
        cover: Optional[UploadFile] = File(None),
    ):
        picture, suffix = _uploaded_cover(cover)
        try:
            publish_stream(client(), ruid, url, series=series, episode=episode,
                           cover=picture, cover_name=suffix, category=category)
        except TonieError as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/tonies", 303)


    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request):
        connection = client()
        tafs = list_tafs(connection)
        # Which tag plays which taf, so deleting one can say what it breaks.
        users: Dict[str, List] = {}
        for tag in list_tags(connection):
            if tag.source:
                users.setdefault(same(tag.source), []).append(tag)
        return templates.TemplateResponse(request, "library.html", {
            "tafs": [(taf, users.get(same(taf.source), [])) for taf in tafs],
            "total": sum(taf.size for taf in tafs),
        })


    @app.post("/library/delete")
    def library_delete(path: str = Form(...)):
        try:
            delete_taf(client(), path)
        except (EncodeError, TeddyCloudError) as error:
            raise Refused(400, "error.failed", detail=error) from error
        return RedirectResponse("/library", 303)


def _uploaded_cover(cover: Optional[UploadFile]) -> Tuple[Optional[bytes], str]:
    """The picture a form sent along, and the extension to store it under.

    Anything that is not an image by its name is ignored rather than refused:
    the cover is optional, and the rest of the form is still worth saving.
    """
    if cover is None or not is_cover_name(cover.filename):
        return None, "jpg"
    return cover.file.read(), Path(cover.filename).suffix.lstrip(".") or "jpg"


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
