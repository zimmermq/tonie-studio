"""Puts a finished taf onto an NFC tag and gives it a cover and track names.

Three things have to agree before a Toniebox plays a homemade tonie the way a
bought one behaves:

- the tag's ``source`` names the taf, and ``nocloud`` is set. Without nocloud
  the box falls back to the Tonies cloud for a tag it believes it owns, and
  plays the original content instead of the new one.
- a custom model carries the cover, the series, the episode and the track
  names. It is metadata for teddyCloud's own screens; the box plays perfectly
  well without it.
- the tag's ``tonie_model`` names that model.

The model is written before the tag points at it, because a tag naming a model
that does not exist yet reads as unknown until something reloads the index.

The model ids live in their own namespace. Reusing an id out of the shipped
tonies.json would shadow a real Tonie, so every id made here starts with a
prefix nothing else uses.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Union

from .encode import EncodeResult, taf_exists
from .library import is_library
from .teddycloud import TeddyCloudClient

log = logging.getLogger(__name__)

# Namespace for the models this app creates.
MODEL_PREFIX = "TS-"

# Where teddyCloud serves the covers of custom models; a model's ``pic`` names
# its cover by this path.
CUSTOM_IMG = "/custom_img/"

# teddyCloud's placeholder for a tag whose slot in the index is not a real tag.
SYSTEM_RUID = "0000000000000000"

DEFAULT_LANGUAGE = "de-de"
DEFAULT_CATEGORY = "music"

# What tonies.json uses, offered so a model is not filed under an invented one.
CATEGORIES = ("music", "audio-book", "audio-play", "audio-play-songs", "audio-book-songs")


class TonieError(RuntimeError):
    """Raised when a tag or a model cannot be written as asked."""


@dataclass
class TonieTag:
    """One NFC tag as the assignment page needs to show it."""

    ruid: str
    uid: str
    source: str
    model: str
    series: str
    episode: str
    picture: str
    claimed: bool
    nocloud: bool = False
    live: bool = False

    @property
    def assigned(self) -> bool:
        return bool(self.source)

    @property
    def label(self) -> str:
        """What to call this tag in a list."""
        return label(self.series, self.episode) or self.source or self.uid or self.ruid


def label(series: Optional[str], episode: Optional[str]) -> str:
    """How a tonie is named in a list: series and episode, or whichever is there."""
    return " - ".join(part for part in (series or "", episode or "") if part)


def is_real_tag(entry: Dict[str, Any]) -> bool:
    """False for teddyCloud's placeholder entries in the tag index, which no sticker has."""
    return entry.get("ruid") != SYSTEM_RUID and entry.get("type") != "system"


def list_tags(client: TeddyCloudClient) -> List[TonieTag]:
    """Every tag the box has read, the empty ones first.

    A tag only appears here once a Toniebox has held it against the reader, so
    a fresh sticker is invisible until then. The empty ones lead because that
    is what a new traveltonie is looking for.
    """
    tags = [_tag(entry) for entry in client.tags() if is_real_tag(entry)]
    return sorted(tags, key=lambda tag: (tag.assigned, tag.label.lower()))


def find_tag(client: TeddyCloudClient, ruid: str) -> Optional[TonieTag]:
    """One tag by its ruid, or None when teddyCloud does not know it."""
    return next((tag for tag in list_tags(client) if tag.ruid == ruid), None)


def require_tag(client: TeddyCloudClient, ruid: str) -> TonieTag:
    """The tag ``ruid``, or a TonieError saying how a sticker becomes known."""
    tag = find_tag(client, ruid)
    if tag is None:
        raise TonieError(
            f"teddyCloud does not know the tag {ruid}; hold it against the box once first"
        )
    return tag


def _tag(entry: Dict[str, Any]) -> TonieTag:
    info = entry.get("tonieInfo") or {}
    return TonieTag(
        ruid=entry.get("ruid", ""),
        uid=entry.get("uid", ""),
        source=entry.get("source") or "",
        model=info.get("model") or "",
        series=info.get("series") or "",
        episode=info.get("episode") or "",
        picture=info.get("picture") or "",
        claimed=bool(entry.get("claimed")),
        nocloud=bool(entry.get("nocloud")),
        live=bool(entry.get("live")),
    )


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
    # The release year as seconds since 1970, the catalogue's form; see _released.
    release: str = ""
    # What lets teddyCloud recognise a taf as this model's audio on its own:
    # it matches a taf's header against these in every known model.
    audio_ids: List[Any] = field(default_factory=list)
    hashes: List[str] = field(default_factory=list)
    other: Dict[str, Any] = field(default_factory=dict)

    _FIELDS = ("model", "series", "episodes", "tracks", "language", "category", "pic", "release",
               "audio_id", "hash")

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
        """The file name of the cover in custom_img, or None.

        None as well for a picture hosted elsewhere, or a name with a path in
        it: neither is a file this app can fetch, back up or print.
        """
        if not self.picture.startswith(CUSTOM_IMG):
            return None
        name = self.picture[len(CUSTOM_IMG):]
        return name if name and "/" not in name else None

    @property
    def year(self) -> Optional[int]:
        """The release year, or None when the model carries no date."""
        try:
            return datetime.fromtimestamp(int(self.release.strip()), timezone.utc).year
        except (ValueError, OverflowError, OSError):
            return None


def list_models(client: TeddyCloudClient) -> List[Model]:
    """Every custom model teddyCloud holds."""
    return [Model.from_json(entry) for entry in client.custom_tonies()]


def find_model(client: TeddyCloudClient, model_id: str) -> Optional[Model]:
    """One custom model by its id, or None."""
    return next((model for model in list_models(client)
                 if model.id.lower() == model_id.lower()), None)


class ModelCache:
    """The models teddyCloud holds, as a copy that is never waited for.

    For pages that only decorate with the models, such as the album list: a
    teddyCloud that does not answer holds a request for its whole timeout,
    three times with retries. A stale copy starts one refresh in the
    background and is handed out as it is; the page after next shows the
    newer one. A failed refresh is not retried for ``ttl`` either, so an
    absent teddyCloud is asked once a minute rather than on every page.
    """

    def __init__(self, client: Callable[[], TeddyCloudClient], ttl: float = 60.0):
        self.client = client
        self.ttl = ttl
        self._models: Optional[List[Model]] = None
        self._at = float("-inf")
        self._loading = False
        self._lock = threading.Lock()

    def get(self) -> Optional[List[Model]]:
        """The last models seen, or None before the first answer; never blocks."""
        with self._lock:
            start = time.monotonic() - self._at > self.ttl and not self._loading
            if start:
                self._loading = True
            models = self._models
        if start:
            threading.Thread(target=self._load, name="models", daemon=True).start()
        return models

    def _load(self) -> None:
        models = None
        try:
            models = list_models(self.client())
        except Exception as error:  # whatever it is, the page simply goes without
            log.info("the models are not to be had this time: %s", error)
        finally:
            with self._lock:
                if models is not None:
                    self._models = models
                self._at = time.monotonic()
                self._loading = False


def next_model_id(client: TeddyCloudClient) -> str:
    """The lowest unused id in this app's namespace."""
    used = {int(model.id[len(MODEL_PREFIX):]) for model in list_models(client)
            if model.id.startswith(MODEL_PREFIX) and model.id[len(MODEL_PREFIX):].isdigit()}
    return f"{MODEL_PREFIX}{max(used, default=0) + 1:04d}"


def publish(
    client: TeddyCloudClient,
    ruid: str,
    encoded: EncodeResult,
    series: str,
    episode: str,
    tracks: Sequence[str],
    cover: Optional[Union[Path, bytes]] = None,
    cover_name: str = "jpg",
    model_id: Optional[str] = None,
    language: str = DEFAULT_LANGUAGE,
    category: str = DEFAULT_CATEGORY,
    year: Optional[Union[int, str]] = None,
) -> str:
    """Give the tag its audio and its identity, and return the model id used."""
    if not encoded.source:
        raise TonieError("the encode produced no library path to point the tag at")
    # A tag pointing at a path that is not there plays nothing, and nothing
    # says so: teddyCloud stores whatever it is handed. This has happened once
    # already, from a source string that had been read with the wrong encoding
    # and written straight back.
    if is_library(encoded.source) and not taf_exists(client, encoded.target):
        raise TonieError(
            f"there is no taf at {encoded.target}; the tag would play nothing"
        )
    require_tag(client, ruid)

    model_id = model_id or next_model_id(client)
    model = _new_model(model_id, series, episode, tracks, language, category)
    model.release = _released(year) or ""
    if encoded.audio_id is not None and encoded.sha1:
        model.audio_ids, model.hashes = [encoded.audio_id], [encoded.sha1.upper()]

    if cover is not None:
        model.picture = _upload_cover(client, model_id, cover, cover_name)
    else:
        # The model is replaced whole on every write, so a picture not carried
        # over is a picture lost: publishing new audio under the same model
        # must not strip its cover.
        existing = find_model(client, model_id)
        if existing:
            model.picture = existing.picture

    client.upsert_custom_tonie(model.to_json())
    client.set_tag(ruid, source=encoded.source, tonie_model=model_id, nocloud=True)
    log.info("tag %s now plays %s as %s", ruid, encoded.source, model_id)
    return model_id


def publish_stream(
    client: TeddyCloudClient,
    ruid: str,
    url: str,
    series: str,
    episode: str = "",
    cover: Optional[Union[Path, bytes]] = None,
    cover_name: str = "jpg",
    model_id: Optional[str] = None,
    language: str = DEFAULT_LANGUAGE,
    category: str = DEFAULT_CATEGORY,
) -> str:
    """Point a tag at a radio stream instead of at a taf.

    teddyCloud decides this by the source alone: a source containing ``://`` is
    a stream, and it then sets ``live`` and ``nocloud`` itself and persists
    that (`contentJson.c`, where `_source_type` becomes `CT_SOURCE_STREAM`).
    Both are set here anyway so the tag is right the moment it is written
    rather than the first time teddyCloud reads it.

    The model carries no ``audio_id`` or ``hash``: there is no taf to identify,
    so nothing can be recognised automatically and the tag names the model
    outright.

    Worth knowing at the box: it reserves a file of ``encode.stream_max_size``
    per stream, 240 MB by default, and stops when that is full - roughly six
    hours. The tonie then has to be lifted and put back.
    """
    url = (url or "").strip()
    if "://" not in url:
        raise TonieError(f"a stream needs a URL, not {url!r}")
    if not url.lower().startswith(("http://", "https://")):
        raise TonieError("only http and https streams are supported")
    require_tag(client, ruid)

    model_id = model_id or next_model_id(client)
    model = _new_model(model_id, series, episode, (), language, category)
    if cover is not None:
        model.picture = _upload_cover(client, model_id, cover, cover_name)

    client.upsert_custom_tonie(model.to_json())
    client.set_tag(ruid, source=url, tonie_model=model_id, live=True, nocloud=True)
    log.info("tag %s now streams %s as %s", ruid, url, model_id)
    return model_id


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
) -> Model:
    """Change a model's description without touching the audio it points at.

    `audio_id` and `hash` are carried over untouched: they are what lets
    teddyCloud recognise the taf, and rewriting a title must not cost that.
    """
    model = find_model(client, model_id)
    if model is None:
        raise TonieError(f"there is no model {model_id}")

    if series is not None:
        model.series = _series(series, model.id)
    if episode is not None:
        model.episode = episode.strip()
    if tracks is not None:
        model.tracks = _tracks(tracks)
    if category is not None:
        model.category = _category(category)
    if cover is not None:
        model.picture = _upload_cover(client, model.id, cover, cover_name)

    client.upsert_custom_tonie(model.to_json())
    log.info("model %s updated", model.id)
    return model


def copy_tag(client: TeddyCloudClient, from_ruid: str, to_ruid: str) -> None:
    """Make a second sticker play what the first one plays.

    Both point at the same taf and name the same model. A second model for
    the same audio would carry the same audio identity and fight the first
    over which name teddyCloud shows; one model on two stickers has nothing to
    fight about. Whatever the second sticker played before is replaced.
    """
    if from_ruid == to_ruid:
        raise TonieError("a sticker cannot be copied onto itself")
    original = find_tag(client, from_ruid)
    if original is None or not original.source:
        raise TonieError(f"the tag {from_ruid} plays nothing to copy")
    require_tag(client, to_ruid)

    client.set_tag(
        to_ruid,
        source=original.source,
        tonie_model=original.model,
        live=original.live,
        # A library tag asking the cloud for content it will never have is
        # the one thing a copy must not inherit.
        nocloud=True if not original.live else original.nocloud,
    )
    log.info("tag %s now plays what %s plays", to_ruid, from_ruid)


def remove_model(client: TeddyCloudClient, model_id: str, ruid: Optional[str] = None) -> None:
    """Delete a model, and unlink the tag that named it.

    The tag keeps its audio: only the cover and the track names go. Leaving
    `tonie_model` pointing at a deleted model would show the tonie as unknown.
    """
    if ruid:
        client.set_tag(ruid, tonie_model="")
    client.delete_custom_tonies([model_id])
    log.info("model %s removed", model_id)


def _new_model(model_id: str, series: str, episode: str, tracks: Sequence[str],
               language: str, category: str) -> Model:
    """A model as this app makes one, whether it plays a taf or a stream."""
    return Model(id=model_id, series=_series(series, model_id), episode=episode.strip(),
                 tracks=_tracks(tracks), language=language, category=_category(category))


def _series(series: str, model_id: str) -> str:
    # teddyCloud's model format needs a series (the client refuses a model
    # without one), so a model nobody named is listed under its own id.
    return series.strip() or model_id


def _tracks(tracks: Sequence[str]) -> List[str]:
    return [track for track in tracks if track]


def _category(category: str) -> str:
    return category if category in CATEGORIES else DEFAULT_CATEGORY


def _released(year: Optional[Union[int, str]]) -> Optional[str]:
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


def _upload_cover(
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
