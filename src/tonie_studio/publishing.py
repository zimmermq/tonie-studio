"""Encoding an album and putting the result on a sticker.

Every page that makes a tonie goes through here: the album page, the batch
and the assignment page. An encode runs as a job (see ``jobs``); its result is
an ``Outcome``, which says what was encoded under which names and, once it is
on a sticker, which model it became.

Putting an outcome on a sticker also records what only this app keeps: the
playing time for the printed text side, and which album the tonie was made
from for the album list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .album import Track
from .albumnotes import remember_made
from .batch import Placing, PlayedOn, Slot
from .encode import EncodeError, EncodeResult, encode_files, existing_taf
from .jobs import Job, JobState
from .models import DEFAULT_CATEGORY
from .pictures import crop
from .runtimes import remember
from .sheet import PIXELS
from .tags import TonieError, find_tag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import publish

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Crop:
    """How the cover is cut for the round sticker: zoom, and the offset from centre."""

    zoom: float = 1.0
    x: float = 0.0
    y: float = 0.0


@dataclass(frozen=True)
class Details:
    """What a person decided about an album before it is encoded.

    ``cover`` is the picture file chosen on the album page, or None. ``year``
    and ``album`` (the album's id) are kept for the printed text side and the
    album list; nothing plays differently because of them.
    """

    series: str
    episode: str
    cover: Optional[str] = None
    crop: Crop = Crop()
    year: Optional[str] = None
    album: str = ""


@dataclass
class Outcome:
    """A finished encode, and what became of it."""

    encoded: EncodeResult
    details: Details
    # The track names a person approved, in the order of the taf's chapters.
    tracks: List[str]
    # The playing time in seconds, when the files said it.
    seconds: Optional[float] = None
    # Set once the outcome is on a sticker.
    model: str = ""
    ruid: str = ""
    # Key of a text saying why it is not on the sticker it was meant for.
    assign_error: str = ""

    @property
    def episode(self) -> str:
        return self.details.episode

    @property
    def series(self) -> str:
        return self.details.series


def encode_job(client: Callable[[], TeddyCloudClient], chosen: Sequence[Tuple[Track, str]],
               target: str, details: Details, replace: bool = False, reuse: bool = False,
               after: Optional[Callable[[Outcome], None]] = None) -> Callable:
    """The work of one encode for the job runner; it returns an Outcome.

    ``client`` makes the teddyCloud client the job uses, on the job's own
    thread: a session is not shared with the request that started it.

    ``chosen`` pairs each track with the name a person gave it. With
    ``reuse`` the taf already at ``target`` is taken as it is, and the job
    fails if that taf has gone or no longer has one chapter per track.
    ``after`` runs once the taf exists and may fill in the outcome; it must
    not raise for a failure the encode itself does not share.
    """
    paths = [track.path for track, _name in chosen]
    names = [name for _track, name in chosen]
    # The playing time turns the encode from a dead bar into a position;
    # teddyCloud itself reports nothing while ffmpeg runs.
    playing_time = sum(track.duration or 0 for track, _name in chosen) or None

    def work(progress):
        connection = client()
        if reuse:
            encoded = existing_taf(connection, target)
            if encoded is None or encoded.tracks != len(paths):
                raise EncodeError("encode.changed", target=target)
        else:
            encoded = encode_files(connection, paths, target, progress=progress,
                                   replace=replace, total_seconds=playing_time)
        outcome = Outcome(encoded=encoded, details=details, tracks=names, seconds=playing_time)
        if after is not None:
            after(outcome)
        return outcome
    return work


def publish_outcome(client: TeddyCloudClient, data_dir: Path, ruid: str, outcome: Outcome,
                    series: Optional[str] = None, episode: Optional[str] = None,
                    category: str = DEFAULT_CATEGORY, require_free: bool = False,
                    model_id: Optional[str] = None) -> str:
    """Put a finished encode on a tag and return the model id it became.

    ``series`` and ``episode`` override the names the encode was planned
    with, for the assignment page where they can still be changed.
    ``require_free`` refuses a tag that plays something, as ``publish`` does.
    ``model_id`` writes the encode into that model rather than a new one.
    """
    model_id = publish(
        client,
        ruid,
        outcome.encoded,
        series=outcome.series if series is None else series,
        episode=outcome.episode if episode is None else episode,
        tracks=outcome.tracks,
        cover=cropped_cover(outcome.details),
        category=category,
        year=outcome.details.year,
        require_free=require_free,
        model_id=model_id,
    )
    outcome.model, outcome.ruid = model_id, ruid
    remember(data_dir, model_id, outcome.seconds)
    remember_made(data_dir, outcome.details.album, model_id)
    return model_id


def put_on_sticker(client: TeddyCloudClient, data_dir: Path, ruid: str, outcome: Outcome) -> bool:
    """Put an outcome on the sticker meant for it, if that sticker is still free.

    Says so in the outcome rather than raising: the encode is fine either way,
    and its page offers the ordinary assignment. A sticker given something
    else since it was chosen is left alone.
    """
    try:
        publish_outcome(client, data_dir, ruid, outcome, require_free=True)
    except TonieError as error:
        if error.key == "tonie.sticker_taken":
            outcome.assign_error = "batch.tag_taken"
            return False
        _note_publish_failed(outcome, ruid, error)
        return False
    except TeddyCloudError as error:
        _note_publish_failed(outcome, ruid, error)
        return False
    return True


def _note_publish_failed(outcome: Outcome, ruid: str, error: Exception) -> None:
    log.warning("could not put %s on %s: %s", outcome.episode, ruid, error)
    outcome.assign_error = "batch.publish_failed"


def free_tags(client: TeddyCloudClient) -> List[str]:
    """Tags the box has read that play nothing yet, in the order they are handed out."""
    return [tag.ruid for tag in list_tags(client) if not tag.assigned]


def cropped_cover(details: Details) -> Optional[bytes]:
    """The album cover, cut the way the album page showed it.

    Cropping here rather than at print time means what teddyCloud displays and
    what comes out of the printer are the same picture.
    """
    if not details.cover:
        return None
    return crop(Path(details.cover), size=PIXELS, zoom=details.crop.zoom,
                offset_x=details.crop.x, offset_y=details.crop.y)


# What putting a sticker on the box during a batch came to.
UNKNOWN = "unknown"      # teddyCloud has not filed the sticker yet; ask again
TAKEN = "taken"          # it plays something else and is left as it is
RESERVED = "reserved"    # it waits for its episode's encode to finish
ASSIGNED = "assigned"    # it plays its episode now
COMPLETE = "complete"    # every episode has a sticker already
NONE_WAITING = "none_waiting"  # nothing left to fill: the remaining encodes failed
FAILED = "failed"        # it could not be written
PLACE_RESULTS = (UNKNOWN, TAKEN, RESERVED, ASSIGNED, COMPLETE, NONE_WAITING, FAILED)


def job_states(job_of: Callable[[str], Optional[Job]]
               ) -> Tuple[Callable[[str], bool], PlayedOn]:
    """How a batch's encodes stand, as ``Placing`` asks: which failed, and where each plays.

    An encode that is gone counts as failed: nothing can be put on a sticker from it.
    """
    def failed(job_id: str) -> bool:
        job = job_of(job_id)
        return job is None or job.state is JobState.FAILED

    def played_on(job_id: str) -> Optional[str]:
        job = job_of(job_id)
        outcome = job.result if job is not None else None
        return outcome.ruid if isinstance(outcome, Outcome) and outcome.model else None

    return failed, played_on


def place(client: TeddyCloudClient, data_dir: Path, placing: Placing, ruid: str,
          job_of: Callable[[str], Optional[Job]]) -> Tuple[str, Optional[Slot]]:
    """Give the sticker lying on the box to the next episode of a batch.

    Returns what happened and the episode slot it happened to. Only a free
    sticker is taken. One placed again keeps its episode, so a sticker left
    lying on the box between two polls is not handed a second one. An
    episode already encoded is put on its sticker at once; one still being
    encoded gets it when its encode finishes.
    """
    failed, played_on = job_states(job_of)
    own = next((slot for slot in placing.slots if slot.ruid == ruid), None)
    tag = find_tag(client, ruid)
    if tag is None:
        return UNKNOWN, None
    if tag.assigned:
        return (ASSIGNED, own) if own is not None else (TAKEN, None)

    slot, settled = placing.reserve(ruid, failed, played_on)
    if slot is None:
        gave_up = any(failed(other.job_id) for other in placing.slots)
        return (NONE_WAITING if gave_up else COMPLETE), None
    if not settled:
        return RESERVED, slot
    job = job_of(slot.job_id)
    if job is None or not job.done.wait(30) or not isinstance(job.result, Outcome):
        return RESERVED, slot
    if put_on_sticker(client, data_dir, ruid, job.result):
        return ASSIGNED, slot
    return FAILED, slot
