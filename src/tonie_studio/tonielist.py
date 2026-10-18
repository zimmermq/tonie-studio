"""The tonie list: every sticker with its kind and what else is known about it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .encode import list_folder
from .models import cover_url, list_models
from .naming import LIBRARY_FOLDER, LIBRARY_PREFIX
from .shop import LANGUAGE as SHOP_LANGUAGE
from .shop import Shop
from .tags import TonieTag
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .times import from_seconds

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


def list_tonies(connection: TeddyCloudClient, tags: List[TonieTag],
            catalogue: Dict[str, Dict], shop: Shop) -> List[Listed]:
    """Every tag with its kind, category, recommended age and when its audio came in.

    Two requests to teddyCloud besides the tag index, for the custom models
    and the library, and the shop's answers in batches, however many tonies
    there are.
    """
    own = {model.id: model for model in list_models(connection)}
    added = library_dates(connection)
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


def library_dates(connection: TeddyCloudClient) -> Dict[str, datetime]:
    """When each taf of the library was written, by the source a tag names it with.

    A tonie teddyCloud keeps in its content tree has no source and so no date;
    the list then leaves the date empty rather than guess.
    """
    try:
        files = list_folder(connection, LIBRARY_FOLDER)
    except TeddyCloudError:
        return {}
    return {LIBRARY_PREFIX + entry["name"]: from_seconds(entry["date"])
            for entry in files if not entry.get("isDir") and entry.get("date")}
