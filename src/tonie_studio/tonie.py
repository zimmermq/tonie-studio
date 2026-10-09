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
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Sequence, Union

from .encode import EncodeResult, taf_exists
from .library import is_library
from .models import (DEFAULT_CATEGORY, DEFAULT_LANGUAGE, find_model, new_model, next_model_id,
                     release_from_year, remove_model, upload_cover)
from .tags import TonieError, find_tag, list_tags, require_tag
from .teddycloud import TeddyCloudClient

log = logging.getLogger(__name__)

# Held from picking a model id to writing the model and pointing the tag at
# it. The id is the next free one, so two publishes running at once - a batch
# encode finishing while a sticker is placed by hand - would otherwise pick
# the same id and the second would overwrite the first tonie's name and cover.
_WRITING = threading.Lock()


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
    require_free: bool = False,
) -> str:
    """Give the tag its audio and its identity, and return the model id used.

    With ``require_free`` a tag that plays something is refused rather than
    replaced. That is checked under the same lock as the write, so two
    writers that both saw it free cannot both have it.
    """
    if not encoded.source:
        raise TonieError("tonie.no_source")
    # A tag pointing at a path that is not there plays nothing, and nothing
    # says so: teddyCloud stores whatever it is handed. This has happened once
    # already, from a source string that had been read with the wrong encoding
    # and written straight back.
    if is_library(encoded.source) and not taf_exists(client, encoded.target):
        raise TonieError("tonie.no_taf", target=encoded.target)
    require_tag(client, ruid)

    with _WRITING:
        if require_free and require_tag(client, ruid).assigned:
            raise TonieError("tonie.sticker_taken", ruid=ruid)
        model_id = model_id or next_model_id(client)
        model = new_model(model_id, series, episode, tracks, language, category)
        model.release = release_from_year(year) or ""
        if encoded.audio_id is not None and encoded.sha1:
            model.audio_ids, model.hashes = [encoded.audio_id], [encoded.sha1.upper()]

        if cover is not None:
            model.picture = upload_cover(client, model_id, cover, cover_name)
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
        raise TonieError("tonie.not_a_url", url=url)
    if not url.lower().startswith(("http://", "https://")):
        raise TonieError("tonie.only_http")
    require_tag(client, ruid)

    with _WRITING:
        model_id = model_id or next_model_id(client)
        model = new_model(model_id, series, episode, (), language, category)
        if cover is not None:
            model.picture = upload_cover(client, model_id, cover, cover_name)

        client.upsert_custom_tonie(model.to_json())
        client.set_tag(ruid, source=url, tonie_model=model_id, live=True, nocloud=True)
    log.info("tag %s now streams %s as %s", ruid, url, model_id)
    return model_id


def copy_tag(client: TeddyCloudClient, from_ruid: str, to_ruid: str) -> None:
    """Make a second sticker play what the first one plays.

    Both point at the same taf and name the same model. A second model for
    the same audio would carry the same audio identity and fight the first
    over which name teddyCloud shows; one model on two stickers has nothing to
    fight about. Whatever the second sticker played before is replaced.
    """
    if from_ruid == to_ruid:
        raise TonieError("tonie.copy_onto_itself")
    original = find_tag(client, from_ruid)
    if original is None or not original.source:
        raise TonieError("tonie.nothing_to_copy", ruid=from_ruid)
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


def drop_model(client: TeddyCloudClient, ruid: str) -> Optional[str]:
    """Take the model off a sticker; delete it only if no other sticker names it.

    A copied sticker shares its model with the original, so deleting it from
    the copy would leave the original nameless. Returns the id of the model
    that was deleted, or None when it was only unlinked from this sticker.
    """
    tag = find_tag(client, ruid)
    if tag is None or not tag.model:
        raise TonieError("tonie.model_missing", model="")
    # Only a model of this app's own can be deleted; unlinking an official
    # one first would leave the sticker nameless for nothing.
    if find_model(client, tag.model) is None:
        raise TonieError("tonie.model_missing", model=tag.model)
    with _WRITING:
        others = [other for other in list_tags(client)
                  if other.ruid != ruid and other.model.lower() == tag.model.lower()]
        if others:
            client.set_tag(ruid, tonie_model="")
            log.info("tag %s no longer names %s; other stickers still do", ruid, tag.model)
            return None
        remove_model(client, tag.model, ruid=ruid)
        return tag.model
