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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Union

from .encode import EncodeResult
from .teddycloud import TeddyCloudClient

log = logging.getLogger(__name__)

# Namespace for the models this app creates.
MODEL_PREFIX = "TS-"

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

    @property
    def assigned(self) -> bool:
        return bool(self.source)

    @property
    def label(self) -> str:
        """What to call this tag in a list."""
        if self.series and self.episode:
            return f"{self.series} - {self.episode}"
        return self.series or self.source or self.uid


def list_tags(client: TeddyCloudClient) -> List[TonieTag]:
    """Every tag the box has read, the empty ones first.

    A tag only appears here once a Toniebox has held it against the reader, so
    a fresh sticker is invisible until then. The empty ones lead because that
    is what a new traveltonie is looking for.
    """
    tags = []
    for entry in client.tags():
        if entry.get("ruid") == SYSTEM_RUID or entry.get("type") == "system":
            continue
        info = entry.get("tonieInfo") or {}
        tags.append(TonieTag(
            ruid=entry.get("ruid", ""),
            uid=entry.get("uid", ""),
            source=entry.get("source") or "",
            model=info.get("model") or "",
            series=info.get("series") or "",
            episode=info.get("episode") or "",
            picture=info.get("picture") or "",
            claimed=bool(entry.get("claimed")),
        ))
    return sorted(tags, key=lambda tag: (tag.assigned, tag.label.lower()))


def next_model_id(client: TeddyCloudClient) -> str:
    """The lowest unused id in this app's namespace."""
    used = set()
    for model in client.custom_tonies():
        name = str(model.get("model", ""))
        if name.startswith(MODEL_PREFIX) and name[len(MODEL_PREFIX):].isdigit():
            used.add(int(name[len(MODEL_PREFIX):]))
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
) -> str:
    """Give the tag its audio and its identity, and return the model id used."""
    if not encoded.source:
        raise TonieError("the encode produced no library path to point the tag at")
    if not any(tag.ruid == ruid for tag in list_tags(client)):
        raise TonieError(
            f"teddyCloud does not know the tag {ruid}; hold it against the box once first"
        )

    model_id = model_id or next_model_id(client)
    model = _model(model_id, series, episode, tracks, encoded, language, category)

    if cover is not None:
        model["pic"] = _upload_cover(client, model_id, cover, cover_name)

    client.upsert_custom_tonie(model)
    client.set_tag(ruid, source=encoded.source, tonie_model=model_id, nocloud=True)
    log.info("tag %s now plays %s as %s", ruid, encoded.source, model_id)
    return model_id


def find_model(client: TeddyCloudClient, model_id: str) -> Optional[Dict[str, Any]]:
    """One custom model by its id, or None."""
    for model in client.custom_tonies():
        if str(model.get("model", "")).lower() == model_id.lower():
            return model
    return None


def find_model_by_audio(
    client: TeddyCloudClient,
    audio_id: Optional[int],
    sha1: Optional[str],
) -> Optional[Dict[str, Any]]:
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
    for model in client.custom_tonies():
        ids = [str(entry) for entry in (model.get("audio_id") or [])]
        hashes = [str(entry).upper() for entry in (model.get("hash") or [])]
        if str(audio_id) in ids and wanted in hashes:
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
) -> Dict[str, Any]:
    """Change a model's description without touching the audio it points at.

    `audio_id` and `hash` are carried over untouched: they are what lets
    teddyCloud recognise the taf, and rewriting a title must not cost that.
    """
    model = find_model(client, model_id)
    if model is None:
        raise TonieError(f"there is no model {model_id}")

    if series is not None:
        model["series"] = series.strip() or model_id
    if episode is not None:
        model["episodes"] = episode.strip()
    if tracks is not None:
        model["tracks"] = [track for track in tracks if track]
    if category is not None:
        model["category"] = category if category in CATEGORIES else DEFAULT_CATEGORY
    if cover is not None:
        model["pic"] = _upload_cover(client, model["model"], cover, cover_name)

    client.upsert_custom_tonie(model)
    log.info("model %s updated", model["model"])
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


def _model(
    model_id: str,
    series: str,
    episode: str,
    tracks: Sequence[str],
    encoded: EncodeResult,
    language: str,
    category: str,
) -> Dict[str, Any]:
    """The tonies.custom.json entry for this taf."""
    model: Dict[str, Any] = {
        "model": model_id,
        "series": series.strip() or model_id,
        "episodes": episode.strip(),
        "tracks": [track for track in tracks if track],
        "language": language,
        "category": category if category in CATEGORIES else DEFAULT_CATEGORY,
    }

    # These two are what lets teddyCloud recognise the audio on its own: it
    # matches a taf's header against every known model, so a tag playing this
    # taf picks the model up even if nobody set tonie_model on it.
    if encoded.audio_id is not None and encoded.sha1:
        model["audio_id"] = [encoded.audio_id]
        model["hash"] = [encoded.sha1.upper()]

    return model


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
        return f"/custom_img/{name}"

    name = f"{model_id}{cover.suffix.lower()}"
    with cover.open("rb") as stream:
        client.upload(stream, name, path="/", special="custom_img")
    return f"/custom_img/{name}"
