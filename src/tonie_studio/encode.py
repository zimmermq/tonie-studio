"""Turns a list of audio files into one taf in the teddyCloud library.

teddyCloud encodes from its own disk, not from the request: ``fileEncode`` is
handed paths and reads them itself. So the audio has to be there first, and the
route it takes is an upload into a staging directory, an encode, and a delete.

The staging names are ``001.flac``, ``002.mp3`` and so on rather than the
original ones. A name would otherwise travel from a NAS through a multipart
header, a urlencoded form and a second filesystem, and the archive is full of
names that do not survive that intact: composed and decomposed umlauts, commas,
brackets, exclamation marks. The extension is kept because ffmpeg selects its
decoder by it, and the number keeps the order visible while a job is running.

A failed encode still cleans up. Half a FLAC album is several hundred megabytes,
and an abandoned job would otherwise sit in the library until someone noticed.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .i18n import Problem
from .library import is_library, path_of, same, source_of
from .models import WRITING, list_models
from .tags import list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

# Where uploads wait for the encoder. One folder, visible in teddyCloud's own
# file browser, so leftovers are findable by a person and not only by this app.
STAGING_ROOT = "/_tonie-studio-staging"

TAF_SUFFIX = ".taf"

# Long enough for the album titles in the archive. A name is cut at both: the
# characters keep every name made so far as it was, the bytes keep a name of
# wide characters (four bytes for an emoji) inside the 255 bytes a file name
# may have, with room for ".taf" and the copy a replace writes beside it.
MAX_NAME = 120
MAX_NAME_BYTES = 200

# Characters that would split a path or hide a file.
UNSAFE = re.compile(r"[/\\\x00-\x1f]")

# What a progress callback is handed: the step, how many of it are done, and of
# how many.
Progress = Callable[[str, int, int], None]

# The steps an encode reports progress in; ``describe_progress`` words each.
UPLOAD, ENCODE = "upload", "encode"
STEPS = (UPLOAD, ENCODE)

# teddyCloud encodes Opus at `encode.bitrate`, which defaults to 96 kbit/s.
# Measured across four tafs already in the library the real rate came out at
# 92.9 to 97.4 kbit/s, so this is close enough to turn a growing file into a
# position. It is an estimate and nothing depends on it being right.
ENCODE_BYTES_PER_SECOND = 96_000 / 8

# How often to look at the growing taf. teddyCloud reports nothing at all while
# it encodes, so this is the only sign of life there is.
GROWTH_POLL = 3.0


class EncodeError(Problem, RuntimeError):
    """Raised when a request cannot be encoded as asked; a person reads it."""


@dataclass
class EncodeResult:
    """What the caller needs to point a tag and a model at the new taf."""

    target: str
    source: str
    tracks: int
    audio_id: Optional[int] = None
    sha1: Optional[str] = None


@dataclass
class Taf:
    """One taf in the library, as its folder listing describes it."""

    path: str
    size: int = 0
    audio_id: Optional[int] = None
    chapters: int = 0
    # False for a file teddyCloud could not read a taf header from.
    valid: bool = False
    # What teddyCloud matched the audio to, from the catalogue or the custom
    # models: its model, name and picture; empty where it knows none.
    model: str = ""
    series: str = ""
    episode: str = ""
    picture: str = ""
    # When the file was written, in seconds since 1970; 0 when not known.
    date: int = 0

    @property
    def name(self) -> str:
        """The file name without its folder and extension: what a person calls it."""
        return self.path.rsplit("/", 1)[-1][:-len(TAF_SUFFIX)]

    @property
    def source(self) -> str:
        """The source a tag uses to play this taf."""
        return source_of(self.path)


def library_path(series: str, episode: str) -> str:
    """Build the library path a taf goes to, from the names a person typed.

    Umlauts stay: the library already holds them and teddyCloud serves them
    back unchanged. Only what would split a path or hide a file is replaced.
    """
    name = _safe_name(episode)
    if not name:
        raise EncodeError("encode.no_name")

    folder = _safe_name(series)
    return f"/{folder}/{name}{TAF_SUFFIX}" if folder else f"/{name}{TAF_SUFFIX}"


def list_tafs(client: TeddyCloudClient, path: str = "/", strict: bool = False) -> List[Taf]:
    """Every taf in the library, with its path, size and audio identity.

    Walks the tree because the library is organised by series, and a flat list
    is what a person cleaning up wants to look at.

    By default a folder that cannot be listed is skipped, which suits a page
    showing what is there. Anything that concludes from the list what is *not*
    there has to pass ``strict``: a skipped folder would make every taf in it
    look missing, and the obvious next step is deleting the tag or the model.
    """
    found: List[Taf] = []
    try:
        entries = client.list_dir(path, special="library")
    except TeddyCloudError as error:
        if strict:
            raise
        log.warning("cannot list %s: %s", path, error)
        return found

    for entry in entries:
        name = entry.get("name")
        if _is_parent_link(name) or name == STAGING_ROOT.lstrip("/"):
            continue
        full = f"{path.rstrip('/')}/{name}"
        if entry.get("isDir"):
            found.extend(list_tafs(client, full, strict=strict))
        elif name.lower().endswith(TAF_SUFFIX):
            header = entry.get("tafHeader") or {}
            info = entry.get("tonieInfo") or {}
            found.append(Taf(
                path=full,
                size=entry.get("size") or 0,
                audio_id=header.get("audioId"),
                chapters=len(header.get("trackSeconds") or []),
                valid=bool(header.get("valid")),
                model=info.get("model") or "",
                series=info.get("series") or "",
                episode=info.get("episode") or "",
                picture=info.get("picture") or "",
                date=entry.get("date") or 0,
            ))
    return sorted(found, key=lambda taf: taf.path.lower())


def delete_taf(client: TeddyCloudClient, path: str) -> None:
    """Remove one taf from the library.

    Whether a tag still points at it is the caller's question to ask; this only
    does what it is told, because sometimes replacing the audio under a tag is
    exactly the intent.
    """
    if not path.endswith(TAF_SUFFIX):
        raise EncodeError("encode.not_a_taf", path=path)
    client.delete(path, special="library")
    log.info("deleted %s", path)


def taf_exists(client: TeddyCloudClient, target: str) -> bool:
    """Whether a taf already sits at ``target``.

    Asked before a job starts, so an occupied name is a question on the form
    rather than a failure minutes later on the progress page.
    """
    return _find_entry(client, target) is not None


def encode_files(
    client: TeddyCloudClient,
    sources: Sequence[Path],
    target: str,
    progress: Optional[Progress] = None,
    replace: bool = False,
    total_seconds: Optional[float] = None,
) -> EncodeResult:
    """Upload the sources, encode them into ``target``, and clear the staging area.

    The sources are encoded in the order given, one chapter each. Anything that
    can be refused is refused before the first byte is uploaded.

    ``total_seconds`` is the playing time of the sources. Given it, the encode
    reports a position by watching the taf grow; without it the bar simply
    waits.

    ``replace`` puts the new audio where a taf already is, and the old one is
    only given up once the new one exists; see ``_replace``. Without it an
    existing taf is refused.
    """
    sources = list(sources)
    _check(sources, target)

    _ensure_parents(client, target)
    replacing = _find_entry(client, target) is not None
    if replacing and not replace:
        raise EncodeError("encode.exists", target=target)
    # Written, replaced and pointed at in the spelling the library already
    # has: teddyCloud's filesystem holds a composed and a decomposed umlaut as
    # two different names, so the requested spelling could name nothing.
    target = _as_listed(client, target)

    job = uuid.uuid4().hex[:12]
    with _OURS_LOCK:
        _OURS.add(job)
    staging = f"{STAGING_ROOT}/{job}"
    _ensure_dir(client, STAGING_ROOT)
    client.create_dir(staging, special="library")

    try:
        staged = []
        for index, source in enumerate(sources, start=1):
            name = f"{index:03d}{source.suffix.lower()}"
            with source.open("rb") as stream:
                client.upload(stream, name, path=staging, special="library")
            staged.append(f"{staging}/{name}")
            _report(progress, UPLOAD, index, len(sources))

        if replacing:
            _replace(client, staged, target, progress, total_seconds)
        else:
            _encode(client, staged, target, progress, total_seconds)
    finally:
        _remove_tree(client, staging)

    header = (_find_entry(client, target) or {}).get("tafHeader") or {}
    return _result(target, header, tracks=len(sources))


def _encode(client: TeddyCloudClient, staged: Sequence[str], target: str,
            progress: Optional[Progress], total_seconds: Optional[float]) -> None:
    # teddyCloud blocks for the whole encode and says nothing. Watching the
    # target grow is the only way to show that something is happening; for a
    # sixty track album that is minutes of otherwise dead screen. The watcher
    # asks over its own session while this one waits for the encode.
    stop = threading.Event()
    if progress and total_seconds:
        threading.Thread(
            target=_watch_growth,
            args=(client.twin(), target, float(total_seconds), progress, stop),
            name="encode-progress", daemon=True,
        ).start()
    try:
        client.encode(target, staged, special="library")
    finally:
        stop.set()
    _report(progress, ENCODE, int(total_seconds or len(staged)), int(total_seconds or len(staged)))


# What a replace puts between a target's name and its suffix for the copy it
# encodes beside it; aside_of reads it back.
ASIDE_MARK = ".new-"
_ASIDE = re.compile(r"^(.*)" + re.escape(ASIDE_MARK) + r"[0-9a-f]{8}" + re.escape(TAF_SUFFIX) + "$")


def aside_of(path: str) -> Optional[str]:
    """The target a replace encodes ``path`` beside, or None for any other taf."""
    match = _ASIDE.match(path)
    return match.group(1) + TAF_SUFFIX if match else None


def _replace(client: TeddyCloudClient, staged: Sequence[str], target: str,
             progress: Optional[Progress], total_seconds: Optional[float]) -> None:
    """Put new audio where a taf is, never leaving a moment with neither.

    teddyCloud cannot rename a file, so the new audio is first encoded beside
    the old taf, and every tag that plays the old one is pointed at it before
    the old one is deleted - the price is a second encode, paid only when
    replacing. Only then is it encoded again under the old name and the tags
    pointed back. Whenever the process is stopped, every tag plays audio that
    exists; should the second encode fail, they keep the copy beside it and
    the error says where it is.

    Stickers still on the copy an earlier, interrupted replace of the same
    target left beside it are taken over as well, and that copy deleted: the
    new audio supersedes it.
    """
    # A name of its own each time: a copy an earlier failed replace left
    # behind may be what the stickers play now, and is not this one's to delete
    # until nothing plays it.
    aside = f"{target[:-len(TAF_SUFFIX)]}{ASIDE_MARK}{uuid.uuid4().hex[:8]}{TAF_SUFFIX}"
    old_header = (_find_entry(client, target) or {}).get("tafHeader") or {}
    with _OURS_LOCK:
        _REPLACING.add(same(aside))
    try:
        _encode(client, staged, aside, progress, total_seconds)

        with WRITING:
            for tag in list_tags(client):
                if same(tag.source) == same(source_of(target)) or _plays_aside_of(tag.source, target):
                    client.set_tag(tag.ruid, source=source_of(aside))
            # Deleted under the lock, so no sticker is given the old taf
            # between the stickers moving off it and it going.
            log.info("replacing the taf at %s", target)
            client.delete(target, special="library")

        try:
            client.encode(target, staged, special="library")
        except TeddyCloudError as error:
            raise EncodeError("encode.replace_kept", target=target, kept=aside) from error

        with WRITING:
            # Which stickers play the copy is read again here, not carried
            # over from before the encode: a sticker copied from one of them
            # meanwhile plays it too, one moved away no longer exists and
            # would be brought back by a write, and one given other audio
            # keeps that.
            _settle(client, target, [aside] + _earlier_asides(client, target))
            _carry_models(client, old_header,
                          (_find_entry(client, target) or {}).get("tafHeader") or {})
    finally:
        with _OURS_LOCK:
            _REPLACING.discard(same(aside))


def _plays_aside_of(source: str, target: str) -> bool:
    """Whether ``source`` names a copy an earlier replace of ``target`` left beside it."""
    if not is_library(source) or same(aside_of(path_of(source)) or "") != same(target):
        return False
    with _OURS_LOCK:
        return same(path_of(source)) not in _REPLACING


def _earlier_asides(client: TeddyCloudClient, target: str) -> List[str]:
    """The copies beside ``target`` that no replace of this process is using."""
    folder = _split(target)[0]
    entries = client.list_dir(folder, special="library") if folder == "/" else list_folder(client, folder)
    found = []
    for entry in entries:
        path = _join(folder, entry.get("name") or "")
        if entry.get("isDir") or same(aside_of(path) or "") != same(target):
            continue
        with _OURS_LOCK:
            if same(path) in _REPLACING:
                continue
        found.append(path)
    return found


def _settle(client: TeddyCloudClient, target: str, asides: Sequence[str]) -> None:
    """Point every sticker on one of ``asides`` at ``target``, then delete them.

    Under WRITING, with the stickers read here: once they are pointed away
    nothing plays the copies, and nothing can be given one before they go.
    Only stickers that exist are written; teddyCloud would make a sticker it
    has forgotten up again from a write.
    """
    sources = {same(source_of(aside)) for aside in asides}
    for tag in list_tags(client):
        if same(tag.source) in sources:
            client.set_tag(tag.ruid, source=source_of(target))
            log.info("tag %s plays %s again", tag.ruid, target)
    for aside in asides:
        _delete(client, aside)


def recover_asides(client: TeddyCloudClient, tafs: Optional[Sequence[Taf]] = None) -> int:
    """Finish what interrupted replaces left undone; returns how many copies went.

    A replace stopped during its final encode - the app killed, teddyCloud
    restarted - leaves its stickers on the copy beside the target for good,
    and the target looks unused. Wherever the target is complete, the
    stickers are pointed back at it and the copy is deleted; a copy nothing
    plays goes as soon as a valid target is there.

    A copy whose target is missing, or smaller than the copy, stays with its
    stickers: teddyCloud lists a taf it is still writing, or stopped writing,
    with a valid header, and the copy may be the only whole audio there is.
    Both encodes read the same files with the same settings, so a complete
    target is never smaller than its copy. Copies of a replace this process
    is running are left to it. Safe to run at any time; ``tafs`` spares the
    walk through the library to a caller that has just listed it.
    """
    recovered = 0
    for taf in (list_tafs(client) if tafs is None else tafs):
        target = aside_of(taf.path)
        if target is None:
            continue
        try:
            with WRITING:
                if _recover(client, taf.path, target):
                    recovered += 1
        except TeddyCloudError as error:
            log.warning("cannot recover %s: %s", taf.path, error)
    if recovered:
        log.info("recovered %d copies left beside their taf by a replace", recovered)
    return recovered


def _recover(client: TeddyCloudClient, aside: str, target: str) -> bool:
    """Settle one copy beside its target if that is safe; under WRITING."""
    with _OURS_LOCK:
        if same(aside) in _REPLACING:
            return False
    aside_entry = _find_entry(client, aside)
    found = _locate(client, *_split(target))
    if aside_entry is None or found is None:
        return False
    target, target_entry = found
    header = target_entry.get("tafHeader") or {}
    if not header.get("valid"):
        return False
    playing = same(source_of(aside)) in {same(tag.source) for tag in list_tags(client)}
    if playing and not _complete(target_entry, aside_entry):
        log.warning("%s is not complete; its stickers stay on %s", target, aside)
        return False
    _settle(client, target, [aside])
    return True


def _complete(target: dict, aside: dict) -> bool:
    """Whether the taf listed as ``target`` holds all the audio of its copy ``aside``."""
    target_header = target.get("tafHeader") or {}
    aside_header = aside.get("tafHeader") or {}
    return (len(target_header.get("trackSeconds") or []) == len(aside_header.get("trackSeconds") or [])
            and (target.get("size") or 0) >= (aside.get("size") or 0))


def _carry_models(client: TeddyCloudClient, old: dict, new: dict) -> None:
    """Have every model that named the old audio name the new one instead.

    A model names its audio by audio_id and hash, and new audio has new
    ones. Left on the old pair, the model still plays through the stickers
    that name it, but teddyCloud's library and find_model_by_audio no longer
    match the new taf to it. Only the replaced pair changes; a model listing
    the parts of a set keeps the other parts. Under WRITING.
    """
    if any(header.get("audioId") is None or not header.get("sha1Hash") for header in (old, new)):
        return
    old_pair = (str(old["audioId"]), old["sha1Hash"].upper())
    for model in list_models(client):
        pairs = list(zip(model.audio_ids, model.hashes))
        if old_pair not in [(str(audio), str(sha).upper()) for audio, sha in pairs]:
            continue
        for index, (audio, sha) in enumerate(pairs):
            if (str(audio), str(sha).upper()) == old_pair:
                model.audio_ids[index] = new["audioId"]
                model.hashes[index] = new["sha1Hash"].upper()
        client.upsert_custom_tonie(model.to_json())
        log.info("model %s now names the replaced audio %s", model.id, new["audioId"])


def existing_taf(client: TeddyCloudClient, target: str) -> Optional[EncodeResult]:
    """A taf already in the library at ``target``, described as its encode would be.

    For a taf that was encoded but never put on a tag, so it can be put on
    one without encoding it again. None when there is no valid taf there.
    """
    found = _locate(client, *_split(target))
    header = (found[1].get("tafHeader") or {}) if found else {}
    if not header.get("valid"):
        return None
    # The listed name, not the requested one: the two may differ in how an
    # umlaut is composed, and a sticker given the requested one plays nothing.
    return _result(found[0], header, tracks=len(header.get("trackSeconds") or []))


def _result(target: str, header: dict, tracks: int) -> EncodeResult:
    """An encode's result from the header teddyCloud lists for the taf it wrote."""
    return EncodeResult(
        target=target,
        source=source_of(target),
        tracks=tracks,
        audio_id=header.get("audioId"),
        sha1=header.get("sha1Hash"),
    )


# The copies beside their target that a replace of this process is encoding
# or about to point stickers away from, in same() spelling. A recovery or
# another replace deleting one would leave those stickers silent.
_REPLACING: set = set()

# The staging folders this process made. Jobs do not outlive the process, so
# any other folder under the staging root belongs to a run that was
# interrupted; one named here may be in use right now. Never shrinks: a
# dozen characters per encode for as long as the process runs.
_OURS: set = set()
_OURS_LOCK = threading.Lock()


def remove_orphaned_staging(client: TeddyCloudClient) -> int:
    """Delete every staging folder an earlier run left over; returns how many.

    Run at startup, which can be after the first encode of this run began
    when teddyCloud answers late; that encode's folder is spared.
    """
    # Asking teddyCloud to list a directory that does not exist does not get a
    # 404: it closes the connection, the client retries twice, and the log
    # fills with warnings about a server that is perfectly fine. The staging
    # root is usually absent, so look for it in its parent instead.
    if not _has_entry(client, "/", STAGING_ROOT.lstrip("/"), directory=True):
        return 0

    try:
        entries = client.list_dir(STAGING_ROOT, special="library")
    except TeddyCloudError:
        return 0

    removed = 0
    for entry in entries:
        name = entry.get("name")
        if _is_parent_link(name):
            continue
        path = f"{STAGING_ROOT}/{name}"
        with _OURS_LOCK:
            if name in _OURS:
                continue
        if entry.get("isDir"):
            _remove_tree(client, path)
            removed += 1
        else:
            _delete(client, path)

    if removed:
        log.info("removed %d abandoned staging folder(s)", removed)
    return removed


# --- plumbing -------------------------------------------------------------


def _check(sources: Sequence[Path], target: str) -> None:
    if not sources:
        raise EncodeError("encode.no_tracks")
    if not target.endswith(TAF_SUFFIX):
        raise EncodeError("encode.target_not_taf", target=target)

    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise EncodeError("encode.unreadable", files=", ".join(missing))


def _safe_name(text: str) -> str:
    name = UNSAFE.sub("-", text or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return cut_to_bytes(name[:MAX_NAME], MAX_NAME_BYTES).strip()


def cut_to_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` bytes of UTF-8, never inside a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _ensure_parents(client: TeddyCloudClient, target: str) -> None:
    """Create every directory above the target that is not there yet."""
    parts = target.strip("/").split("/")[:-1]
    path = ""
    for part in parts:
        path = f"{path}/{part}"
        _ensure_dir(client, path)


def _ensure_dir(client: TeddyCloudClient, path: str) -> None:
    # Lists without _entry on purpose: a folder that cannot be listed has to
    # fail the encode here, not be taken for missing and created again.
    parent, name = _split(path)
    for entry in client.list_dir(parent, special="library"):
        if entry.get("isDir") and _same_name(entry.get("name"), name):
            return
    client.create_dir(path, special="library")


def list_folder(client: TeddyCloudClient, path: str) -> List[dict]:
    """The listing of a library folder; empty when it does not exist.

    Looked for in its parent first, for the reason _entry gives: a folder
    teddyCloud does not have is answered by a dropped connection.
    """
    parent, name = _split(path)
    if _entry(client, parent, name, directory=True) is None:
        return []
    return client.list_dir(path, special="library")


def _has_entry(
    client: TeddyCloudClient, parent: str, name: str, directory: bool = False,
) -> bool:
    """Whether ``parent`` holds an entry of that name, without listing it."""
    return _entry(client, parent, name, directory) is not None


def _find_entry(client: TeddyCloudClient, path: str) -> Optional[dict]:
    """The listing entry for the file at ``path``, or None when it is not there."""
    return _entry(client, *_split(path))


def _entry(client: TeddyCloudClient, parent: str, name: str,
           directory: bool = False) -> Optional[dict]:
    """The entry ``name`` in the listing of ``parent``; None if it is not there."""
    found = _locate(client, parent, name, directory)
    return found[1] if found else None


def _locate(client: TeddyCloudClient, parent: str, name: str,
            directory: bool = False) -> Optional[Tuple[str, dict]]:
    """The path ``parent``/``name`` as the library lists it, and its entry.

    Names are compared across the two spellings an umlaut has, and every
    folder on the way is listed under the name the library gave it.

    ``parent`` is only listed once its own parent shows it, up to the root:
    teddyCloud answers a listing of a folder that is not there by closing the
    connection, which costs retries and leaves an error in its log for every
    taf looked for in a series folder not made yet. A listing that fails for
    a folder that is there raises TeddyCloudError: read as "not there", a
    moment of teddyCloud being away would say audio is missing that is not.
    """
    if parent != "/":
        folder = _locate(client, *_split(parent), directory=True)
        if folder is None:
            return None
        parent = folder[0]
    entries = client.list_dir(parent, special="library")
    entry = next((entry for entry in entries
                  if bool(entry.get("isDir")) == directory and _same_name(entry.get("name"), name)),
                 None)
    return (_join(parent, entry["name"]), entry) if entry is not None else None


def _as_listed(client: TeddyCloudClient, path: str, directory: bool = False) -> str:
    """``path`` in the spelling the library lists it in, as far as it exists."""
    parent, name = _split(path)
    found = _locate(client, parent, name, directory)
    if found is not None:
        return found[0]
    if parent == "/":
        return path
    return _join(_as_listed(client, parent, directory=True), name)


def _join(folder: str, name: str) -> str:
    return f"{folder.rstrip('/')}/{name}"


def _split(path: str) -> Tuple[str, str]:
    """A library path as its folder and its last name."""
    parent, _, name = path.rpartition("/")
    return parent or "/", name


def _remove_tree(client: TeddyCloudClient, path: str) -> None:
    """Delete a directory and everything in it, never failing the caller.

    This runs in a finally block after an encode that may itself have failed.
    Reporting a cleanup problem instead of the real error would hide the thing
    worth knowing, so it is logged and swallowed.
    """
    try:
        entries = client.list_dir(path, special="library")
    except TeddyCloudError as error:
        log.warning("cannot list %s for cleanup: %s", path, error)
        return

    for entry in entries:
        name = entry.get("name")
        if _is_parent_link(name):
            continue
        child = f"{path}/{name}"
        if entry.get("isDir"):
            _remove_tree(client, child)
        else:
            _delete(client, child)

    _delete(client, path)


def _delete(client: TeddyCloudClient, path: str) -> None:
    try:
        client.delete(path, special="library")
    except TeddyCloudError as error:
        log.warning("cannot delete %s: %s", path, error)


def _is_parent_link(name: Optional[str]) -> bool:
    """teddyCloud lists "." and ".." as ordinary entries; following them would
    walk out of the directory being cleaned and into the library."""
    return name in (None, ".", "..")


def _same_name(left: Optional[str], right: str) -> bool:
    """Compare names across the two spellings an umlaut has on a share."""
    return left is not None and same(left) == same(right)


def _watch_growth(
    client: TeddyCloudClient,
    target: str,
    total_seconds: float,
    progress: Progress,
    stop: threading.Event,
) -> None:
    """Turn the growing taf into a position, until the encode finishes.

    Never reports completion: the encode says when it is done. Stopping one
    second short is honest, a bar that sits at 100 percent while ffmpeg is
    still running is not.
    """
    # A moment of teddyCloud not answering, while it is busy encoding, is no
    # reason to stop: the bar would freeze for the rest of the encode. Said
    # once, not every few seconds for as long as it lasts.
    said = False
    while not stop.wait(GROWTH_POLL):
        try:
            entry = _find_entry(client, target)
        except TeddyCloudError as error:
            if not said:
                log.warning("cannot see how far the encode of %s is: %s", target, error)
                said = True
            continue
        if entry is None:
            continue
        written = entry.get("size") or 0
        done = min(written / ENCODE_BYTES_PER_SECOND, total_seconds - 1)
        _report(progress, ENCODE, int(max(done, 0)), int(total_seconds))


def _report(progress: Optional[Progress], step: str, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(step, done, total)
    except Exception:  # a broken progress display must not kill the encode
        log.exception("progress callback failed")
