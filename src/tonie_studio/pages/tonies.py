"""The tonies: the list, a tonie's page, and what can be done with one."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ..albumnotes import made
from ..encode import EncodeError
from ..library import is_library, listed_path, path_of
from ..listing import Column, filters
from ..models import (CATEGORIES, DEFAULT_CATEGORY, WRITING, find_model, images_cached,
                      picture_suffix, release_year, update_model)
from ..podcast import LARGEST_IMAGE, FeedError, move_subscriptions, unsubscribe
from ..tags import TonieError, list_tags
from ..tonie import copy_tag, move_tag, plan_removal, remove_tonie
from ..tonielist import FREE, NO_MODEL, NO_PICTURE, OFFICIAL, OWN, list_tonies
from .common import Context, Refused, refusing, tag_or_404, uploaded_cover

log = logging.getLogger(__name__)

def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    box = ctx.box
    client = ctx.client

    @app.get("/tonies", response_class=HTMLResponse)
    def tonies(request: Request):
        connection = client()
        tags = list_tags(connection)
        rows = list_tonies(connection, tags, ctx.namer.official().by_model, ctx.shop)
        tonies = sorted((row for row in rows if row.kind != FREE), key=lambda row: row.name)
        counts, offered = filters((row.tags for row in tonies), (OFFICIAL, OWN, NO_MODEL, NO_PICTURE))
        return templates.TemplateResponse(request, "tonies.html", {
            "tags": tags,
            "tonies": tonies,
            "free": [row for row in rows if row.kind == FREE],
            "counts": counts,
            "filters": offered,
            "columns": TONIE_COLUMNS,
            "box": box.state,
            "images_cached": images_cached(connection),
        })


    @app.get("/model/{model_id}")
    def tonie_of_model(model_id: str):
        """The tonie a model is on, or, on no sticker yet, its cover to print.

        Where a link knows only the model - an album row - it comes here
        rather than reading every sticker up front for every row.
        """
        for tag in list_tags(client()):
            if tag.model == model_id:
                return RedirectResponse(f"/tonies/{quote(tag.ruid)}", 303)
        return RedirectResponse("/sheet?" + urlencode({"model": model_id}), 303)

    @app.get("/tonies/{ruid}", response_class=HTMLResponse)
    def tonie_page(request: Request, ruid: str):
        connection = client()
        tags = list_tags(connection)
        tag = tag_or_404(tags, ruid)
        model = find_model(connection, tag.model) if tag.model else None
        # What the catalogue and the shop say about it: for a tonie the
        # catalogue names by its article number, for a model of this app by
        # the shop product it was linked to. The first view asks the shop.
        entry, about = None, None
        if tag.model and model is None:
            entry = ctx.namer.official().by_model.get(tag.model)
            if entry:
                about = ctx.shop.lookup(tag.model, entry.get("language") or "")
        elif model is not None and model.shop_link:
            about = ctx.shop.lookup(model.shop_link)
        # Audio the sticker points at but the library no longer has: the tonie
        # looks like any other and plays nothing. The same goes for a file
        # the library has under another spelling of its umlauts, which
        # teddyCloud's file system tells apart; the checkup repairs that.
        listed = listed_path(connection, path_of(tag.source)) if is_library(tag.source) else None
        silent = is_library(tag.source) and listed != path_of(tag.source)
        return templates.TemplateResponse(request, "tonie.html", {
            "tag": tag,
            "model": model,
            "silent": silent,
            "other_spelling": silent and listed is not None,
            "remake": _album_of(settings.data_dir, tag.model) if silent else None,
            "entry": entry,
            "about": about,
            "year": release_year(entry.get("release")) if entry else None,
            "categories": CATEGORIES,
            # Where a copy could go, the empty stickers first.
            "others": [other for other in tags if other.ruid != ruid],
            # What the delete button would take with it, shown before it is pressed.
            "removal": plan_removal(connection, ruid) if tag.assigned or tag.model else None,
        })


    @app.get("/tonies/{ruid}/shop", response_class=HTMLResponse)
    def shop_page(request: Request, ruid: str):
        """What the shop has that could be this tonie, for a person to pick from."""
        connection = client()
        tag, model = _own_model(connection, ruid)
        return templates.TemplateResponse(request, "shop.html", {
            "tag": tag,
            "model": model,
            "found": ctx.shop.search(model.series, model.episode),
            "chapters": tag.chapters or None,
        })

    @app.post("/tonies/{ruid}/shop")
    def take_from_shop(ruid: str, sales_id: str = Form(...)):
        """Take a shop product's picture, category and, where they fit, chapter titles.

        The name stays the one given here. The chapter titles are only taken
        when the product has as many as this tonie's audio has chapters: they
        are named by position, and titles of another edition would name the
        wrong ones. The product's article number is kept in the model, so the
        page shows what the shop says about it from then on.
        """
        connection = client()
        tag, model = _own_model(connection, ruid)
        info = ctx.shop.lookup(sales_id)
        if info is None:
            raise Refused(404, "error.shop_unknown", product=sales_id)
        picture = None
        if info.picture:
            with refusing(FeedError, requests.RequestException, status=502):
                picture = ctx.fetch(info.picture, LARGEST_IMAGE)
        tracks = info.tracks if info.tracks and len(info.tracks) == tag.chapters else None
        update_model(
            connection, model.id, tracks=tracks,
            category=info.genre_key if info.genre_key in CATEGORIES else None,
            cover=picture, cover_name=picture_suffix(info.picture), shop_link=info.sales_id)
        log.info("model %s takes picture and details of shop product %s", model.id, info.sales_id)
        return RedirectResponse(f"/tonies/{ruid}", 303)

    def _own_model(connection, ruid):
        tag = tag_or_404(list_tags(connection), ruid)
        model = find_model(connection, tag.model) if tag.model else None
        if model is None:
            raise Refused(404, "error.no_model")
        return tag, model

    @app.post("/tonies/{ruid}/copy")
    def copy_tonie(ruid: str, target: str = Form(...), move: str = Form("")):
        """Make another sticker play this tonie; with ``move``, this sticker is then forgotten."""
        with refusing(TonieError):
            if move:
                # A podcast on it keeps refreshing the sticker it is on now. In
                # one step with the move: a refresh publishing in between would
                # find its sticker gone.
                with WRITING:
                    move_tag(client(), ruid, target)
                    move_subscriptions(settings.data_dir, ruid, target)
            else:
                copy_tag(client(), ruid, target)
        return RedirectResponse(f"/tonies/{target}" if move else "/tonies", 303)


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
        tag = tag_or_404(list_tags(connection), ruid)
        if not tag.model:
            raise Refused(404, "error.no_model")

        picture, suffix = uploaded_cover(cover)

        with refusing(TonieError):
            update_model(connection, tag.model, series=series, episode=episode,
                         category=category, cover=picture, cover_name=suffix)
        return RedirectResponse("/tonies", 303)


    @app.post("/tonies/{ruid}/delete")
    def delete_tonie(ruid: str):
        """Free the sticker and delete what only it used: model, taf, its podcast."""
        with refusing(TonieError, EncodeError):
            remove_tonie(client(), ruid, settings.data_dir)
        # A subscription would only fail on the emptied sticker from now on.
        unsubscribe(settings.data_dir, ruid)
        return RedirectResponse("/tonies", 303)


# The tonie list's columns, in the order the rows show them.
TONIE_COLUMNS = (
    Column("name", "col.name"),
    Column("series", "tonies.col_series"),
    Column("kind", "tonies.col_kind"),
    Column("chapters", "col.chapters", "tonies.sort_chapters", number=True, desc=True),
    Column("category", "tonies.col_category"),
    Column("age", "tonies.col_age", "tonies.sort_age", number=True),
    Column("added", "col.added", "tonies.sort_added", number=True, desc=True),
)

def _album_of(data_dir: Path, model_id: str) -> Optional[str]:
    """The album the model was made from, if this app made it from one."""
    return next((album for album, models in made(data_dir).items()
                 if model_id and model_id in models), None)
