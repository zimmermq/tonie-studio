"""What can be printed: which models have a picture, the rows of the print
page, the motifs of a sheet with their text sides, and the overview's entries.

The page asks; this decides. sheet.py and overview.py then only draw.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple


from .i18n import DEFAULT as DEFAULT_LANGUAGE
from .i18n import Translator
from .models import Model, cache_name, fetch_picture, list_models
from .naming import Catalog
from .overview import Entry
from .runtimes import minutes
from .sheet import Back, Sticker
from .tags import list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)


@dataclass
class SheetRow:
    """One cover the sheet can print: a sticker and the model it plays, or a model on none."""

    model: Model
    uid: str = ""  # as teddyCloud writes it; empty for a model on no sticker
    ruid: str = ""
    ticked: bool = False

    @property
    def search(self) -> str:
        """What the page's search looks through: the names, the model and the UID.

        The UID is there without its colons too, the way it is often printed
        on a chip, so either spelling finds it.
        """
        return " ".join((self.model.label, self.model.id, self.uid,
                         self.uid.replace(":", ""))).lower()


def printable(model: Model, cached: Optional[bool]) -> bool:
    """Whether a model has a picture this app can put on a sheet.

    That is a cover in custom_img, or, while teddyCloud keeps the pictures it
    is shown, one hosted elsewhere, which then comes through its cache.
    """
    return bool(model.cover or (cached is not False and cache_name(model.picture)))


def known_models(client: TeddyCloudClient, catalog: Catalog,
                  cached: Optional[bool] = True) -> Dict[str, Model]:
    """Every printable model on a sticker, and every printable model of this app, by id.

    Only catalogue tonies on a sticker: the catalogue lists thousands nobody
    here owns. Their picture comes through teddyCloud's image cache.
    """
    custom = {model.id: model for model in list_models(client)}
    known = {model_id: model for model_id, model in custom.items() if printable(model, cached)}
    for tag in list_tags(client):
        if tag.model and tag.model not in custom and tag.model in catalog.by_model:
            model = Model.from_json(catalog.by_model[tag.model])
            if printable(model, cached):
                known[model.id] = model
    return known


def sheet_rows(client: TeddyCloudClient, catalog: Catalog,
                cached: Optional[bool] = True) -> List[SheetRow]:
    """A row per sticker with a printable model, then the models of this app on none.

    Two stickers that play the same tonie are two rows: each chip in hand
    gets a cover of its own.
    """
    known = known_models(client, catalog, cached)
    rows = [SheetRow(known[tag.model], tag.uid, tag.ruid)
            for tag in list_tags(client) if tag.model in known]
    on_stickers = {row.model.id for row in rows}
    rows += [SheetRow(model) for model in known.values() if model.id not in on_stickers]
    return sorted(rows, key=lambda row: (row.model.label.casefold(), row.uid))


def stickers_for(client: Callable[[], TeddyCloudClient], wanted: Sequence[Tuple[Model, int, str]],
              back: bool, back_tracks: bool, times: Dict[str, int],
              translate: Translator) -> List[Sticker]:
    """The motifs of a sheet, each fetching its picture only when the sheet gets to it.

    The text side follows its cover, and both use one download: the last
    picture fetched is kept until the next motif asks for another.
    """
    last: Dict[str, Optional[bytes]] = {}

    def picture_of(model: Model) -> Callable[[], Optional[bytes]]:
        def fetch() -> Optional[bytes]:
            if model.id not in last:
                last.clear()
                last[model.id] = cover_bytes(client(), model)
            return last[model.id]
        return fetch

    stickers = []
    for model, count, mode in wanted:
        stickers.append(Sticker(image=picture_of(model), count=count, label=model.label,
                                mode=mode))
        # One text side per copy, right after it, so a sheet cut up in order
        # leaves each pair together.
        if back:
            description = back_of(model, times.get(model.id), translate)
            if not back_tracks:
                description.tracks = ()
            stickers.append(Sticker(image=picture_of(model), count=count,
                                    label=f"{model.label} back", back=description))
    return stickers


def overview_entries(client: TeddyCloudClient, times: Dict[str, int],
                      translate: Optional[Translator] = None) -> List[Entry]:
    """One entry per model that sits on a sticker, in shelf order.

    A model no sticker names is not a tonie anyone can pick up, so it does not
    belong on a list of what is in the tin.
    """
    on_stickers = {tag.model for tag in list_tags(client) if tag.model}
    entries = []
    for model in list_models(client):
        if model.id not in on_stickers:
            continue
        image = cover_bytes(client, model)
        back = back_of(model, times.get(model.id), translate)
        entries.append(Entry(image=image, title=back.title, series=back.series,
                             facts=list(back.notes)))
    return sorted(entries, key=lambda entry: (entry.series.casefold(), entry.title.casefold()))


def back_of(model: Model, seconds: Optional[int] = None,
             translate: Optional[Translator] = None) -> Back:
    """What the text side of one model says.

    The episode names the album and the series names who made it, which is the
    order they are looked for in: a shelf of tonies is searched by album.
    Facts the model does not carry are left out rather than guessed at, so a
    tonie recorded before the app stored them simply says less.
    """
    translate = translate or Translator(DEFAULT_LANGUAGE)
    facts = [
        str(model.year or ""),
        translate("pdf.tracks", count=len(model.tracks)) if model.tracks else "",
        translate("pdf.minutes", count=minutes(seconds)) if minutes(seconds) else "",
    ]
    notes = [" - ".join(fact for fact in facts if fact), model.id]
    return Back(
        title=model.episode or model.series or model.id,
        series=model.series,
        notes=[note for note in notes if note],
        tracks=list(model.tracks),
        language=translate.lang,
    )


def cover_bytes(client: TeddyCloudClient, model: Optional[Model]) -> Optional[bytes]:
    """The cover of a printable model, or None when there is none to be read."""
    if model is None or not (model.cover or cache_name(model.picture)):
        return None
    try:
        return fetch_picture(client, model.picture)
    except TeddyCloudError as error:
        log.warning("cannot read the cover of %s: %s", model.id, error)
        return None
