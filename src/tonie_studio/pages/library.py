"""The library: every taf teddyCloud keeps to play."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..checkup import BOX_SOUND_PREFIX
from ..encode import TAF_SUFFIX, EncodeError, Taf, delete_taf, existing_taf, list_tafs
from ..jobs import JobState
from ..languages import NONE, chosen, offered as offered_languages, spoken
from ..library import safe_library_path, same
from ..listing import Column, filters
from ..naming import LIBRARY_FOLDER
from ..podcast_archive import episode_at, picture_of
from ..models import CATEGORIES, DEFAULT_CATEGORY, WRITING, cover_url, list_models
from ..tags import TonieError, TonieTag, list_tags, require_tag
from ..tags import label as tag_label
from ..times import from_seconds
from ..tonie import publish
from .common import Context, Refused, refusing

def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    templates = ctx.templates
    client = ctx.client

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request):
        connection = client()
        tafs = list_tafs(connection)
        languages = {model: entry.get("language") or ""
                     for model, entry in ctx.namer.official().by_model.items()}
        languages.update({model.id: model.language for model in list_models(connection)})
        shelf = sorted(_shelve(tafs, list_tags(connection), languages),
                       key=lambda item: item.label.casefold())
        counts, offered = filters((item.tags for item in shelf), (RECORDED, IMPORTED, BOX, UNUSED))
        spoken_counts = Counter(item.language for item in shelf)
        return templates.TemplateResponse(request, "library.html", {
            "shelf": shelf,
            "total": sum(taf.size for taf in tafs),
            "counts": counts,
            "filters": offered,
            "spoken_languages": offered_languages(spoken_counts),
            "spoken_counts": spoken_counts,
            "columns": LIBRARY_COLUMNS,
        })


    @app.post("/library/delete")
    def library_delete(path: str = Form(...), players: str = Form("")):
        """Delete a taf, after checking again what the page showed about it.

        ``players`` are the stickers the page said play it, which the person
        was asked about. A page left open must not delete what has since been
        put on another sticker or is being written by an encode right now.
        """
        if not path.endswith(TAF_SUFFIX):
            raise Refused(400, "error.failed", detail=EncodeError("encode.not_a_taf", path=path))
        connection = client()
        item = next((item for item in _shelve(list_tafs(connection), list_tags(connection))
                     if item.taf.path == path), None)
        if item is None:
            raise Refused(404, "library.gone")
        if item.origin == BOX:
            raise Refused(400, "library.box_sound")
        if any(job.kind == "encode" and same(job.label) == same(path)
               and job.state in (JobState.QUEUED, JobState.RUNNING) for job in ctx.jobs.all()):
            raise Refused(409, "library.being_made")
        shown = set(filter(None, players.split(",")))
        added = [tag for tag in item.played_by + item.misspelled_by if tag.ruid not in shown]
        if added:
            raise Refused(409, "library.now_used",
                          used=", ".join(tag.label for tag in added))
        with refusing(EncodeError), WRITING:
            # Once more right before deleting, under the lock every tag write
            # shares: the walk above takes seconds, a copy onto it fewer.
            now = [tag for tag in list_tags(connection) if same(tag.source) == same(item.taf.source)
                   and tag.ruid not in shown]
            if now:
                raise Refused(409, "library.now_used",
                              used=", ".join(tag.label for tag in now))
            delete_taf(connection, path)
        return RedirectResponse("/library", 303)

    @app.get("/library/assign", response_class=HTMLResponse)
    def assign_page(request: Request, path: str):
        """Put audio that is in the library, and played by nothing, on a sticker.

        The encode that made it would have offered this; after a restart that
        job is gone, and the audio would otherwise only be deletable.
        """
        connection = client()
        encoded = (existing_taf(connection, path)
                   if path.endswith(TAF_SUFFIX) and safe_library_path(path) else None)
        if encoded is None:
            raise Refused(404, "library.gone")
        folder, _slash, name = path.strip("/").rpartition("/")
        archived = episode_at(ctx.settings.data_dir, path)
        return templates.TemplateResponse(request, "library_assign.html", {
            "path": path,
            "encoded": encoded,
            "imported": _imported(path),
            "series": folder.rsplit("/", 1)[-1],
            # An archived episode by its title, which the file name may have
            # had to change.
            "episode": archived.title if archived and archived.title else name[:-len(TAF_SUFFIX)],
            "tags": list_tags(connection),
            "categories": CATEGORIES,
        })

    @app.post("/library/assign")
    def assign(path: str = Form(...), ruid: str = Form(...), series: str = Form(""),
               episode: str = Form(""), category: str = Form(DEFAULT_CATEGORY),
               language: str = Form("")):
        connection = client()
        encoded = (existing_taf(connection, path)
                   if path.endswith(TAF_SUFFIX) and safe_library_path(path) else None)
        if encoded is None:
            raise Refused(404, "library.gone")
        with refusing(TonieError):
            if _imported(path):
                # Official audio: teddyCloud names it by itself. A model of our
                # own would carry that audio, and the naming replaces such a
                # model by the official one, deleting the name typed here.
                with WRITING:
                    require_tag(connection, ruid)
                    connection.set_tag(ruid, source=encoded.source, tonie_model="",
                                       nocloud=True)
            else:
                if not episode.strip():
                    raise Refused(400, "encode.no_name")
                archived = episode_at(ctx.settings.data_dir, path)
                cover = picture_of(ctx.settings.data_dir, archived) if archived else None
                publish(connection, ruid, encoded, series=series, episode=episode, tracks=[],
                        cover=cover, cover_name=(archived.picture.rsplit(".", 1)[-1]
                                                 if cover else "jpg"),
                        category=category, language=chosen(language),
                        data_dir=ctx.settings.data_dir)
        return RedirectResponse(f"/tonies/{ruid}", 303)


# The library's columns, in the order the rows show them.
LIBRARY_COLUMNS = (
    Column("name", "col.name"),
    Column("origin", "library.col_origin"),
    Column("chapters", "col.chapters", number=True, desc=True),
    Column("size", "library.col_size", number=True, desc=True),
    Column("added", "col.added", number=True, desc=True),
    Column("played", "library.col_played"),
)

# Where a taf of the library comes from; the library page filters by it.
RECORDED, IMPORTED, BOX = "recorded", "imported", "box"
# Not where it comes from but filtered the same way: no sticker plays it.
UNUSED = "unused"


def _imported(path: str) -> bool:
    """Whether a taf is one teddyCloud fetched from the Tonies cloud: official audio."""
    return same(path).startswith(same(LIBRARY_FOLDER) + "/")


def _shelve(tafs: List[Taf], tags: List[TonieTag],
            languages: Optional[Dict[str, str]] = None) -> List["Shelved"]:
    """Each taf with its origin, the stickers that play it and those that only seem to.

    A sticker plays a taf only when its source names the file exactly;
    teddyCloud's file system tells the two spellings of an umlaut apart.
    ``languages`` gives each model's language, catalogue and own models alike:
    a taf speaks the language of the model teddyCloud matched its audio to,
    else of the model of a sticker naming it.
    """
    languages = languages or {}
    users: Dict[str, List[TonieTag]] = {}
    for tag in tags:
        if tag.source:
            users.setdefault(same(tag.source), []).append(tag)
    shelf = []
    for taf in tafs:
        naming = users.get(same(taf.source), [])
        model = taf.model or next((tag.model for tag in naming if tag.model), "")
        shelf.append(Shelved(taf, _origin(taf),
                             [tag for tag in naming if tag.source == taf.source],
                             [tag for tag in naming if tag.source != taf.source],
                             spoken(languages.get(model))))
    return shelf


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
    """A taf of the library as its page shows it: the file, its origin, who plays it.

    ``misspelled_by`` are the stickers naming it with its umlauts spelled the
    other way: they play nothing, but the checkup's repair points them back
    at this file, so it is neither unused nor deleted without asking.
    """

    taf: Taf
    origin: str
    played_by: List[TonieTag]
    misspelled_by: List[TonieTag] = field(default_factory=list)
    language: str = NONE  # what it speaks, as languages.spoken groups it

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
    def picture(self) -> str:
        """The audio's own picture, else that of the sticker playing it, as with the label.

        Audio teddyCloud cannot match by its audio id has no picture of its
        own, while the official tonie playing it has one.
        """
        if cover_url(self.taf.picture, False):
            return self.taf.picture
        return next((tag.picture for tag in self.played_by if cover_url(tag.picture, False)),
                    self.taf.picture)

    @property
    def added(self) -> Optional[datetime]:
        """When the file was written into the library, or None when teddyCloud does not say."""
        return from_seconds(self.taf.date)

    @property
    def tags(self) -> List[str]:
        """What the filters match. The box's sounds are played by the box, never unused."""
        unused = not self.played_by and not self.misspelled_by and self.origin != BOX
        return [self.origin, UNUSED] if unused else [self.origin]
