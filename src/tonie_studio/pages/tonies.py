"""The tonies: the list, a tonie's page, and what can be done with one."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ..albumnotes import made
from ..encode import EncodeError, taf_exists
from ..library import is_library, path_of
from ..listing import Column, filters
from ..models import (CATEGORIES, DEFAULT_CATEGORY, cover_url,
                      find_model, images_cached, list_models,
                      picture_suffix, release_year, update_model)
from ..naming import LIBRARY_FOLDER, LIBRARY_PREFIX
from ..podcast import LARGEST_IMAGE, FeedError, unsubscribe
from ..shop import LANGUAGE as SHOP_LANGUAGE
from ..shop import Shop
from ..tags import TonieError, TonieTag, list_tags
from ..teddycloud import TeddyCloudClient, TeddyCloudError
from ..times import from_seconds
from ..tonie import copy_tag, move_tag, plan_removal, remove_tonie
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
        rows = _listed(connection, tags, ctx.namer.official().by_model, ctx.shop)
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
        # looks like any other and plays nothing.
        silent = is_library(tag.source) and not taf_exists(connection, path_of(tag.source))
        return templates.TemplateResponse(request, "tonie.html", {
            "tag": tag,
            "model": model,
            "silent": silent,
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
                move_tag(client(), ruid, target)
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

# What kind of tonie a row of the list is; the filters of the page go by it.
OFFICIAL, OWN, NO_MODEL, FREE = "official", "own", "none", "free"
# Not a kind but filtered the same way: a tonie with no picture to show.
NO_PICTURE = "nopicture"


@dataclass
class Listed:
    """A tonie as the list shows it: its tag and what is known about it besides."""

    tag: TonieTag
    kind: str
    category: str = ""
    added: Optional[datetime] = None  # when its taf was written into the library
    age: Optional[int] = None  # the youngest age tonies.com recommends it for

    @property
    def tags(self) -> List[str]:
        """What the filters match: the kind, and whether there is no picture to show."""
        return [self.kind] if cover_url(self.tag.picture, False) else [self.kind, NO_PICTURE]

    @property
    def name(self) -> str:
        """What the list orders by: the episode, which tells tonies of one series apart."""
        return (self.tag.episode or self.tag.label).casefold()


def _listed(connection: TeddyCloudClient, tags: List[TonieTag],
            catalogue: Dict[str, Dict], shop: Shop) -> List[Listed]:
    """Every tag with its kind, category, recommended age and when its audio came in.

    Two requests to teddyCloud besides the tag index, for the custom models
    and the library, and the shop's answers in batches, however many tonies
    there are.
    """
    own = {model.id: model for model in list_models(connection)}
    added = _library_dates(connection)
    rows = []
    for tag in tags:
        if not tag.assigned:
            rows.append(Listed(tag, FREE))
        elif tag.model in own:
            rows.append(Listed(tag, OWN, own[tag.model].category, added.get(tag.source)))
        elif tag.model:
            category = (catalogue.get(tag.model) or {}).get("category") or ""
            rows.append(Listed(tag, OFFICIAL, category, added.get(tag.source)))
        else:
            rows.append(Listed(tag, NO_MODEL, "", added.get(tag.source)))

    # What the shop sells each as: an official tonie under its article
    # number, if the catalogue entry is from the German shop that is asked;
    # an own model as the product it took its picture from.
    sold_as = {}
    for row in rows:
        if row.kind == OFFICIAL:
            if (catalogue.get(row.tag.model) or {}).get("language") == SHOP_LANGUAGE:
                sold_as[row.tag.ruid] = row.tag.model
        elif row.kind == OWN and own[row.tag.model].shop_link:
            sold_as[row.tag.ruid] = own[row.tag.model].shop_link
    known = shop.lookup_many(sold_as.values())
    for row in rows:
        info = known.get(sold_as.get(row.tag.ruid, ""))
        row.age = info.age_min if info else None
    return rows


def _album_of(data_dir: Path, model_id: str) -> Optional[str]:
    """The album the model was made from, if this app made it from one."""
    return next((album for album, models in made(data_dir).items()
                 if model_id and model_id in models), None)


def _library_dates(connection: TeddyCloudClient) -> Dict[str, datetime]:
    """When each taf of the library was written, by the source a tag names it with.

    A tonie teddyCloud keeps in its content tree has no source and so no date;
    the list then leaves the date empty rather than guess.
    """
    try:
        files = connection.list_dir(LIBRARY_FOLDER, special="library")
    except TeddyCloudError:
        return {}
    return {LIBRARY_PREFIX + entry["name"]: from_seconds(entry["date"])
            for entry in files if not entry.get("isDir") and entry.get("date")}
