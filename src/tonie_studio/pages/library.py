"""The library: every taf teddyCloud keeps to play."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..checkup import BOX_SOUND_PREFIX
from ..encode import EncodeError, Taf, delete_taf, list_tafs
from ..library import same
from ..listing import Column, filters
from ..naming import LIBRARY_FOLDER
from ..tags import TonieTag, list_tags
from ..tags import label as tag_label
from ..teddycloud import TeddyCloudError
from ..times import from_seconds
from .common import Context, refusing

def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    templates = ctx.templates
    client = ctx.client

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request):
        connection = client()
        tafs = list_tafs(connection)
        # Which tag plays which taf, so deleting one can say what it breaks.
        users: Dict[str, List] = {}
        for tag in list_tags(connection):
            if tag.source:
                users.setdefault(same(tag.source), []).append(tag)
        shelf = sorted((Shelved(taf, _origin(taf), users.get(same(taf.source), []))
                        for taf in tafs), key=lambda item: item.label.casefold())
        counts, offered = filters((item.tags for item in shelf), (RECORDED, IMPORTED, BOX, UNUSED))
        return templates.TemplateResponse(request, "library.html", {
            "shelf": shelf,
            "total": sum(taf.size for taf in tafs),
            "counts": counts,
            "filters": offered,
            "columns": LIBRARY_COLUMNS,
        })


    @app.post("/library/delete")
    def library_delete(path: str = Form(...)):
        with refusing(EncodeError, TeddyCloudError):
            delete_taf(client(), path)
        return RedirectResponse("/library", 303)


# The library's columns, in the order the rows show them.
LIBRARY_COLUMNS = (
    Column("name", "library.col_name"),
    Column("origin", "library.col_origin"),
    Column("chapters", "library.col_chapters", number=True, desc=True),
    Column("size", "library.col_size", number=True, desc=True),
    Column("added", "library.col_added", number=True, desc=True),
    Column("played", "library.col_played"),
)

# Where a taf of the library comes from; the library page filters by it.
RECORDED, IMPORTED, BOX = "recorded", "imported", "box"
# Not where it comes from but filtered the same way: no sticker plays it.
UNUSED = "unused"


def _origin(taf: Taf) -> str:
    """Recorded here, fetched from the Tonies cloud, or one of the box's own sounds.

    teddyCloud files what it fetches from the cloud under its audio id in
    one folder, and names the box's own sounds after the box in its
    catalogue; everything else in the library was put there, by this app's
    encodes or by hand.
    """
    if taf.model.lower().startswith(BOX_SOUND_PREFIX):
        return BOX
    if same(taf.path).startswith(same(LIBRARY_FOLDER) + "/"):
        return IMPORTED
    return RECORDED


@dataclass
class Shelved:
    """A taf of the library as its page shows it: the file, its origin, who plays it."""

    taf: Taf
    origin: str
    played_by: List[TonieTag]

    @property
    def label(self) -> str:
        """The tonie teddyCloud knows the audio as, else the sticker that plays it, else the file.

        teddyCloud names a file it fetched after its audio id, a bare number,
        and knows no name for audio no catalogue lists; the sticker playing
        it usually has one.
        """
        return (tag_label(self.taf.series, self.taf.episode)
                or (self.played_by[0].label if self.played_by else "") or self.taf.name)

    @property
    def added(self) -> Optional[datetime]:
        """When the file was written into the library, or None when teddyCloud does not say."""
        return from_seconds(self.taf.date)

    @property
    def tags(self) -> List[str]:
        """What the filters match. The box's sounds are played by the box, never unused."""
        unused = not self.played_by and self.origin != BOX
        return [self.origin, UNUSED] if unused else [self.origin]
