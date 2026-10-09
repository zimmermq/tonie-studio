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
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from tonie_studio.teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

# Where uploads wait for the encoder. One folder, visible in teddyCloud's own
# file browser, so leftovers are findable by a person and not only by this app.
STAGING_ROOT = "/_tonie-studio-staging"

TAF_SUFFIX = ".taf"

# Long enough for the album titles in the archive, short enough to stay well
# inside the 255 byte limit a file name has once it is UTF-8 encoded.
MAX_NAME = 120

# Characters that would split a path or hide a file.
UNSAFE = re.compile(r"[/\\\x00-\x1f]")

# What a progress callback is handed: the step, how many of it are done, and of
# how many.
Progress = Callable[[str, int, int], None]

# The steps an encode reports progress in; texts in job.step.*.
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


class EncodeError(RuntimeError):
    """Raised when a request cannot be encoded as asked."""


@dataclass
class EncodeResult:
    """What the caller needs to point a tag and a model at the new taf."""

    target: str
    source: str
    tracks: int
    audio_id: Optional[int] = None
    sha1: Optional[str] = None


def library_path(series: str, episode: str) -> str:
    """Build the library path a taf goes to, from the names a person typed.

    Umlauts stay: the library already holds them and teddyCloud serves them
    back unchanged. Only what would split a path or hide a file is replaced.
    """
    name = _safe_name(episode)
    if not name:
        raise EncodeError("the taf needs a name")

    folder = _safe_name(series)
    return f"/{folder}/{name}{TAF_SUFFIX}" if folder else f"/{name}{TAF_SUFFIX}"


def list_tafs(client: TeddyCloudClient, path: str = "/", strict: bool = False) -> List[dict]:
    """Every taf in the library, with its path, size and audio identity.

    Walks the tree because the library is organised by series, and a flat list
    is what a person cleaning up wants to look at.

    By default a folder that cannot be listed is skipped, which suits a page
    showing what is there. Anything that concludes from the list what is *not*
    there has to pass ``strict``: a skipped folder would make every taf in it
    look missing, and the obvious next step is deleting the tag or the model.
    """
    found: List[dict] = []
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
            found.append({
                "path": full,
                "source": f"lib://{full.lstrip('/')}",
                "name": name[:-len(TAF_SUFFIX)],
                "size": entry.get("size") or 0,
                "audio_id": header.get("audioId"),
                "chapters": len(header.get("trackSeconds") or []),
                "valid": bool(header.get("valid")),
            })
    return sorted(found, key=lambda taf: taf["path"].lower())


def delete_taf(client: TeddyCloudClient, path: str) -> None:
    """Remove one taf from the library.

    Whether a tag still points at it is the caller's question to ask; this only
    does what it is told, because sometimes replacing the audio under a tag is
    exactly the intent.
    """
    if not path.endswith(TAF_SUFFIX):
        raise EncodeError(f"that is not a {TAF_SUFFIX} file: {path}")
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

    ``replace`` deletes an existing taf at the target first. Any tag pointing at
    it plays nothing until the new one is written, so this is only ever done
    because someone asked for it.
    """
    sources = list(sources)
    _check(sources, target)

    _ensure_parents(client, target)
    if _find_entry(client, target):
        if not replace:
            raise EncodeError(f"a taf already exists at {target}; rename or delete it first")
        log.info("replacing the taf at %s", target)
        client.delete(target, special="library")

    job = uuid.uuid4().hex[:12]
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

        # teddyCloud blocks for the whole encode and says nothing. Watching
        # the target grow is the only way to show that something is happening;
        # for a sixty track album that is minutes of otherwise dead screen.
        stop = threading.Event()
        if progress and total_seconds:
            threading.Thread(
                target=_watch_growth,
                args=(client, target, float(total_seconds), progress, stop),
                name="encode-progress", daemon=True,
            ).start()
        try:
            client.encode(target, staged, special="library")
        finally:
            stop.set()
        _report(progress, ENCODE, int(total_seconds or len(sources)),
                int(total_seconds or len(sources)))
    finally:
        _remove_tree(client, staging)

    header = (_find_entry(client, target) or {}).get("tafHeader") or {}
    return EncodeResult(
        target=target,
        source=f"lib://{target.lstrip('/')}",
        tracks=len(sources),
        audio_id=header.get("audioId"),
        sha1=header.get("sha1Hash"),
    )


def existing_taf(client: TeddyCloudClient, target: str) -> Optional[EncodeResult]:
    """A taf already in the library at ``target``, described as its encode would be.

    For a taf that was encoded but never put on a tag, so it can be put on
    one without encoding it again. None when there is no valid taf there.
    """
    header = (_find_entry(client, target) or {}).get("tafHeader") or {}
    if not header.get("valid"):
        return None
    return EncodeResult(
        target=target,
        source=f"lib://{target.lstrip('/')}",
        tracks=len(header.get("trackSeconds") or []),
        audio_id=header.get("audioId"),
        sha1=header.get("sha1Hash"),
    )


def remove_orphaned_staging(client: TeddyCloudClient) -> int:
    """Delete every staging folder left over, and report how many there were.

    Jobs do not outlive the process, so at startup every folder under the
    staging root belongs to a run that was interrupted.
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
        raise EncodeError("select at least one track")
    if not target.endswith(TAF_SUFFIX):
        raise EncodeError(f"the target has to be a {TAF_SUFFIX} file, not {target}")

    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise EncodeError(f"cannot read: {', '.join(missing)}")


def _safe_name(text: str) -> str:
    name = UNSAFE.sub("-", text or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:MAX_NAME].strip()


def _ensure_parents(client: TeddyCloudClient, target: str) -> None:
    """Create every directory above the target that is not there yet."""
    parts = target.strip("/").split("/")[:-1]
    path = ""
    for part in parts:
        path = f"{path}/{part}"
        _ensure_dir(client, path)


def _ensure_dir(client: TeddyCloudClient, path: str) -> None:
    parent = path.rsplit("/", 1)[0] or "/"
    name = path.rsplit("/", 1)[1]
    for entry in client.list_dir(parent, special="library"):
        if entry.get("isDir") and _same_name(entry.get("name"), name):
            return
    client.create_dir(path, special="library")


def _has_entry(
    client: TeddyCloudClient, parent: str, name: str, directory: bool = False,
) -> bool:
    """Whether ``parent`` holds an entry of that name, without listing it."""
    try:
        entries = client.list_dir(parent, special="library")
    except TeddyCloudError:
        return False
    return any(bool(entry.get("isDir")) == directory and _same_name(entry.get("name"), name)
               for entry in entries)


def _find_entry(client: TeddyCloudClient, path: str) -> Optional[dict]:
    """The listing entry for one path, or None when it is not there."""
    parent = path.rsplit("/", 1)[0] or "/"
    name = path.rsplit("/", 1)[1]
    try:
        entries = client.list_dir(parent, special="library")
    except TeddyCloudError:
        return None

    for entry in entries:
        if not entry.get("isDir") and _same_name(entry.get("name"), name):
            return entry
    return None


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
    if left is None:
        return False
    return unicodedata.normalize("NFC", left) == unicodedata.normalize("NFC", right)


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
    while not stop.wait(GROWTH_POLL):
        entry = _find_entry(client, target)
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
