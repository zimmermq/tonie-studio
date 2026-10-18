"""teddyCloud's custom models: what a tonie is called, shows and plays.

The model ids live in their own namespace. Reusing an id out of the shipped
tonies.json would shadow a real Tonie, so every id made here starts with a
prefix nothing else uses.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Union
from urllib.parse import quote, urlparse

from .cache import BackgroundCache
from .tags import TonieError, label
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .times import from_seconds

log = logging.getLogger(__name__)


# Namespace for the models this app creates.
MODEL_PREFIX = "TS-"


# Where teddyCloud serves the covers of custom models; a model's ``pic`` names
# its cover by this path.
CUSTOM_IMG = "/custom_img/"

# Where teddyCloud serves the pictures it keeps itself with the setting
# tonie_json.cache_images on: the catalogue's figure pictures and every model
# picture hosted elsewhere, fetched on first use. It then hands out these
# addresses instead of the original ones, for this app's covers too, which it
# answers with a redirect to custom_img.
CACHE = "/cache/"
CACHE_SETTING = "tonie_json.cache_images"
_CACHE_NAME = re.compile(r"^[0-9A-Fa-f]{64}\.[A-Za-z0-9]{1,5}$")


DEFAULT_LANGUAGE = "de-de"


DEFAULT_CATEGORY = "music"


# The pictures a cover may be, with the type they are served as. Anything
# else in custom_img - a restored backup can bring any name - is not served
# or written back, because a browser would take an .html "cover" for a page.
COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}


def is_cover_name(name: Optional[str]) -> bool:
    """Whether ``name`` is a picture file a cover may be."""
    return bool(name) and Path(name).suffix.lower() in COVER_TYPES


# What tonies.json uses, offered so a model is not filed under an invented one.
CATEGORIES = ("music", "audio-book", "audio-play", "audio-play-songs", "audio-book-songs")



def cache_name(picture: Optional[str]) -> Optional[str]:
    """The name ``picture`` has in teddyCloud's image cache, or None.

    teddyCloud names a cached picture after the upper-case SHA-256 of its
    original address, with the extension after its last dot (query left out)
    or "jpg" when there is none. A /cache/ address carries its name already.
    """
    if not picture:
        return None
    if picture.startswith(CACHE):
        name = picture[len(CACHE):]
        return name if _CACHE_NAME.match(name) else None
    if not picture.startswith(("http://", "https://")):
        return None
    digest = hashlib.sha256(picture.encode("utf-8")).hexdigest().upper()
    dot = picture.rfind(".")
    extension = picture[dot + 1:].split("?", 1)[0] if dot >= 0 and "/" not in picture[dot:] else ""
    return f"{digest}.{extension or 'jpg'}"


def cover_url(picture: Optional[str], cached: bool = True, size: int = 0) -> Optional[str]:
    """Where a page asks for ``picture``, or None when there is no picture to show.

    Through this app wherever it can: the browser reaches it but not
    teddyCloud. A picture hosted elsewhere goes through teddyCloud's cache
    when that keeps pictures (``cached``), and straight to its host otherwise.
    ``size`` asks this app for the picture scaled to fit that many pixels; a
    picture fetched from its host comes as the host sends it.
    """
    name = cover_name(picture)
    if name:
        url = f"/cover/custom/{quote(name)}"
    elif picture and picture.startswith(CACHE):
        name = cache_name(picture)
        url = f"/cover/cache/{name}" if name else None
    elif picture and picture.startswith(("http://", "https://")):
        if not cached:
            return picture
        url = f"/cover/cache/{cache_name(picture)}"
    else:
        url = None
    return f"{url}?size={size}" if url and size else url


def images_cached(client: TeddyCloudClient) -> Optional[bool]:
    """Whether teddyCloud keeps the pictures itself, or None when it cannot say."""
    try:
        return client.setting(CACHE_SETTING) == "true"
    except TeddyCloudError:
        return None


def fetch_picture(client: TeddyCloudClient, picture: Optional[str]) -> bytes:
    """The bytes of a model's picture, from custom_img or through teddyCloud's cache.

    Raises TeddyCloudError when teddyCloud cannot hand it out, such as for a
    picture hosted elsewhere while its cache keeps no pictures.
    """
    name = cover_name(picture)
    if name:
        return fetch_cover(client, name)
    name = cache_name(picture)
    if not name:
        raise TeddyCloudError(f"no picture to fetch: {picture!r}")
    return client.fetch(f"{CACHE}{name}")


def cover_name(picture: Optional[str]) -> Optional[str]:
    """The file name a model's picture has in custom_img, or None.

    None as well for a picture hosted elsewhere, or a name with a path in
    it: neither is a file this app can fetch, back up or print.
    """
    if not picture or not picture.startswith(CUSTOM_IMG):
        return None
    name = picture[len(CUSTOM_IMG):]
    return name if name and "/" not in name else None


# Held by everything that writes models or tags in teddyCloud: from picking a
# model id to writing the model and pointing the tag at it, and around every
# read-change-write of a model. The id is the next free one and a model is
# written whole, so two writers at once - a batch encode finishing while a
# sticker is placed by hand, the naming after an import, a title being saved
# while a podcast refresh publishes new audio - would otherwise take the same
# id, or write back what the other just changed. Re-entrant, so a writer
# holding it can call update_model.
WRITING = threading.RLock()

# Where a model of this app keeps the article number of the shop product it
# took its picture and details from. teddyCloud stores a custom model whole,
# so the field travels with the model, into its backups too.
SHOP_LINK = "shop_sales_id"


@dataclass
class Model:
    """One custom model in teddyCloud: what a tonie is called and shows.

    teddyCloud replaces a model whole on every write. A model read from it
    therefore keeps every field it came with: the ones this app has no use
    for travel in ``other`` and are written back unchanged.
    """

    id: str
    series: str = ""
    episode: str = ""
    tracks: List[str] = field(default_factory=list)
    language: str = DEFAULT_LANGUAGE
    category: str = DEFAULT_CATEGORY
    # The cover as teddyCloud serves it, such as /custom_img/TS-0001.jpg.
    picture: str = ""
    # The release year as seconds since 1970, the catalogue's form; see release_from_year.
    release: str = ""
    # What lets teddyCloud recognise a taf as this model's audio on its own:
    # it matches a taf's header against these in every known model.
    audio_ids: List[Any] = field(default_factory=list)
    hashes: List[str] = field(default_factory=list)
    other: Dict[str, Any] = field(default_factory=dict)

    _FIELDS = ("model", "series", "episodes", "tracks", "language", "category", "pic", "release",
               "audio_id", "hash")

    @property
    def shop_link(self) -> Optional[str]:
        """The article number of the shop product this model was linked to, if any."""
        value = self.other.get(SHOP_LINK)
        return str(value) if value else None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Model":
        return cls(
            id=str(data.get("model") or ""),
            series=str(data.get("series") or ""),
            episode=str(data.get("episodes") or ""),
            tracks=[str(track) for track in data.get("tracks") or []],
            language=str(data.get("language") or ""),
            category=str(data.get("category") or ""),
            picture=str(data.get("pic") or ""),
            release=str(data.get("release") or ""),
            audio_ids=list(data.get("audio_id") or []),
            hashes=[str(entry) for entry in data.get("hash") or []],
            other={key: value for key, value in data.items() if key not in cls._FIELDS},
        )

    def to_json(self) -> Dict[str, Any]:
        """The model as teddyCloud's tonies.custom.json holds it."""
        data = dict(self.other)
        data.update({"model": self.id, "series": self.series, "episodes": self.episode,
                     "tracks": list(self.tracks)})
        for key, value in (("language", self.language), ("category", self.category),
                           ("pic", self.picture), ("release", self.release),
                           ("audio_id", self.audio_ids), ("hash", self.hashes)):
            if value:
                data[key] = value if isinstance(value, str) else list(value)
        return data

    @property
    def label(self) -> str:
        """How the model is named in a list."""
        return label(self.series, self.episode)

    @property
    def cover(self) -> Optional[str]:
        """The file name of the cover in custom_img, or None; see ``cover_name``."""
        return cover_name(self.picture)

    @property
    def year(self) -> Optional[int]:
        """The release year, or None when the model carries no date."""
        return release_year(self.release)


def list_models(client: TeddyCloudClient) -> List[Model]:
    """Every custom model teddyCloud holds."""
    return [Model.from_json(entry) for entry in client.custom_tonies()]


def find_model(client: TeddyCloudClient, model_id: str) -> Optional[Model]:
    """One custom model by its id, or None."""
    return next((model for model in list_models(client)
                 if model.id.lower() == model_id.lower()), None)


class ModelCache(BackgroundCache[List[Model]]):
    """The models teddyCloud holds, as a copy that is never waited for.

    For pages that only decorate with the models, such as the album list.
    """

    def __init__(self, client: Callable[[], TeddyCloudClient], ttl: float = 60.0):
        super().__init__(lambda: list_models(client()), name="models", ttl=ttl)


def next_model_id(client: TeddyCloudClient) -> str:
    """The lowest unused id in this app's namespace."""
    used = {int(model.id[len(MODEL_PREFIX):]) for model in list_models(client)
            if model.id.startswith(MODEL_PREFIX) and model.id[len(MODEL_PREFIX):].isdigit()}
    return f"{MODEL_PREFIX}{max(used, default=0) + 1:04d}"


def find_model_by_audio(
    client: TeddyCloudClient,
    audio_id: Optional[int],
    sha1: Optional[str],
) -> Optional[Model]:
    """The model that already carries this exact audio, if there is one.

    teddyCloud identifies a taf by `audio_id` plus `hash`, so two models
    holding the same pair describe the same recording. Encoding an album twice
    produces two tafs with different audio_ids, but re-using one taf for a
    second tag does not - and then the second model is a duplicate that will
    fight the first for teddyCloud's automatic matching.
    """
    if audio_id is None or not sha1:
        return None
    wanted = sha1.upper()
    for model in list_models(client):
        if (str(audio_id) in [str(entry) for entry in model.audio_ids]
                and wanted in [entry.upper() for entry in model.hashes]):
            return model
    return None


def update_model(
    client: TeddyCloudClient,
    model_id: str,
    series: Optional[str] = None,
    episode: Optional[str] = None,
    category: Optional[str] = None,
    tracks: Optional[Sequence[str]] = None,
    cover: Optional[Union[Path, bytes]] = None,
    cover_name: str = "jpg",
    shop_link: Optional[str] = None,
) -> Model:
    """Change a model's description without touching the audio it points at.

    `audio_id` and `hash` are carried over untouched: they are what lets
    teddyCloud recognise the taf, and rewriting a title must not cost that.
    What is not named stays as it is.
    """
    with WRITING:
        return _update_model(client, model_id, series, episode, category, tracks, cover,
                             cover_name, shop_link)


def _update_model(client: TeddyCloudClient, model_id: str, series: Optional[str],
                  episode: Optional[str], category: Optional[str],
                  tracks: Optional[Sequence[str]], cover: Optional[Union[Path, bytes]],
                  cover_name: str, shop_link: Optional[str]) -> Model:
    model = find_model(client, model_id)
    if model is None:
        raise TonieError("tonie.model_missing", model=model_id)

    if series is not None:
        model.series = _series(series, model.id)
    if episode is not None:
        model.episode = episode.strip()
    if tracks is not None:
        model.tracks = _tracks(tracks)
    if category is not None:
        model.category = _category(category)
    if cover is not None:
        model.picture = upload_cover(client, model.id, cover, cover_name)
    if shop_link is not None:
        model.other[SHOP_LINK] = shop_link

    client.upsert_custom_tonie(model.to_json())
    log.info("model %s updated", model.id)
    return model


def remove_model(client: TeddyCloudClient, model_id: str, ruid: Optional[str] = None) -> None:
    """Delete a model, and unlink the tag that named it.

    The tag keeps its audio: only the cover and the track names go. Leaving
    `tonie_model` pointing at a deleted model would show the tonie as unknown.
    """
    if ruid:
        client.set_tag(ruid, tonie_model="")
    client.delete_custom_tonies([model_id])
    log.info("model %s removed", model_id)


def new_model(model_id: str, series: str, episode: str, tracks: Sequence[str],
               language: str, category: str) -> Model:
    """A model as this app makes one, whether it plays a taf or a stream."""
    return Model(id=model_id, series=_series(series, model_id), episode=episode.strip(),
                 tracks=_tracks(tracks), language=language, category=_category(category))


def _series(series: str, model_id: str) -> str:
    # teddyCloud's model format needs a series (the client refuses a model
    # without one), so a model nobody named is listed under its own id.
    return series.strip() or model_id


def _tracks(tracks: Sequence[str]) -> List[str]:
    # One name per chapter, by position: a track without a name keeps its
    # place as an empty one, or every name after it would label the wrong chapter.
    return [track or "" for track in tracks]


def _category(category: str) -> str:
    return category if category in CATEGORIES else DEFAULT_CATEGORY


def release_from_year(year: Optional[Union[int, str]]) -> Optional[str]:
    """A release year as the seconds-since-1970 string the catalogue uses.

    The year arrives from an audio tag or a folder name, so it can be "2017",
    "2017-03-01" or something that is not a year at all; anything but a plausible
    four digit year is dropped rather than turned into a wrong date.

    Only the year is ever known, so the day is the first of January. The
    official catalogue carries real release dates in this same field, which is
    why nothing may read more than a year back out of it.
    """
    match = re.match(r"\s*(\d{4})", str(year or ""))
    if not match:
        return None
    found = int(match.group(1))
    if not 1900 <= found <= 2200:
        return None
    return str(int(datetime(found, 1, 1, tzinfo=timezone.utc).timestamp()))


def upload_cover(
    client: TeddyCloudClient,
    model_id: str,
    cover: Union[Path, bytes],
    suffix: str = "jpg",
) -> str:
    """Put the cover in custom_img under the model's name and return its url.

    Takes a file or the bytes of one already cropped for the round sticker.
    Naming it after the model keeps one image per model: publishing again
    replaces the picture instead of leaving the old one behind with nothing
    pointing at it.
    """
    if isinstance(cover, bytes):
        name = f"{model_id}.{suffix.lstrip('.').lower()}"
        stream: BinaryIO = io.BytesIO(cover)
        client.upload(stream, name, path="/", special="custom_img")
        return f"{CUSTOM_IMG}{name}"

    name = f"{model_id}{cover.suffix.lower()}"
    with cover.open("rb") as stream:
        client.upload(stream, name, path="/", special="custom_img")
    return f"{CUSTOM_IMG}{name}"


def fetch_cover(client: TeddyCloudClient, name: str) -> bytes:
    """The bytes of one cover in custom_img; raises TeddyCloudError when it cannot be read."""
    return client.fetch(f"{CUSTOM_IMG}{name}")


def release_year(release: Optional[str]) -> Optional[int]:
    """The year of a catalogue release, which the catalogue gives as seconds since 1970."""
    moment = from_seconds((release or "").strip())
    return moment.year if moment else None


def picture_suffix(url: str) -> str:
    """The cover type of a picture's address, as teddyCloud keeps covers.

    The address's own suffix where it is a cover type; jpg otherwise, which
    is what most pictures without one turn out to be.
    """
    suffix = PurePosixPath(urlparse(url or "").path).suffix.lower()
    return suffix.lstrip(".") if suffix in COVER_TYPES else "jpg"
