"""The files a browser upload brings: spooled to disk, and cleared away later.

Every upload is spooled into its own folder so the album reader can treat it
like any other album. A FLAC album is several hundred megabytes, so left alone
those folders fill the volume one evening of uploads at a time.

Folders are removed by age rather than after a successful encode: an upload is
still worth having when the encode failed, when the tracks were ordered wrongly,
or when the same album is about to be cut a second way.
"""

from __future__ import annotations

import errno
import logging
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple

from .album import is_audio_name, name_from_file
from .batch import episode_order
from .i18n import Problem
from .models import is_cover_name

log = logging.getLogger(__name__)


class UploadError(Problem, OSError):
    """An upload that could not be kept."""


def spool(upload_dir: Path, files: Iterable[Tuple[Optional[str], BinaryIO]]) -> List[Path]:
    """Write an upload into a folder of its own; returns its album folders in episode order.

    ``files`` are (name as the browser sent it, content) pairs. Only audio and
    pictures are kept. A picked folder that holds one folder per episode
    arrives as one upload, and each of those becomes an album of its own.
    Nothing is written, and nothing returned, when there is no audio at all.
    """
    by_folder = _upload_folders(list(files))
    if not by_folder:
        return []
    root = upload_dir / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    destinations = _subfolders(root, by_folder)
    try:
        for key, members in by_folder.items():
            destinations[key].mkdir(parents=True, exist_ok=True)
            for name, data in members:
                with (destinations[key] / _base_name(name)).open("wb") as target:
                    shutil.copyfileobj(data, target)
    except OSError as error:
        # Half an album is no album, and would hold its space for days.
        shutil.rmtree(root, ignore_errors=True)
        if error.errno == errno.ENOSPC:
            raise UploadError("upload.no_space") from error
        raise
    return sorted(set(destinations.values()), key=lambda path: episode_order(path.name))


def _upload_folders(files: Sequence[Tuple[Optional[str], BinaryIO]]) -> Dict[str, List[Tuple[str, BinaryIO]]]:
    """The uploaded files that belong to an album, by the folder they came from.

    A folder upload names every file by its path below the folder picked.
    Images come along so a cover can be uploaded with the tracks; a folder
    with no audio at all, such as a series folder holding only a picture,
    is not an album and is dropped.
    """
    folders: Dict[str, List[Tuple[str, BinaryIO]]] = {}
    for name, data in files:
        if not (is_audio_name(name) or is_cover_name(name)):
            continue
        folder = str(PurePosixPath((name or "").replace("\\", "/")).parent)
        folders.setdefault(folder, []).append((name, data))
    return {folder: members for folder, members in folders.items()
            if any(is_audio_name(name) for name, _data in members)}


def _subfolders(root: Path, folders: Dict[str, List[Tuple[str, BinaryIO]]]) -> Dict[str, Path]:
    """One folder below ``root`` per uploaded folder, named like it and its parent.

    The names matter: an album without tags is called after its folder, and
    a series without an artist tag after the folder holding its episodes.
    ``root`` itself has a random name, which is never what anyone picked.
    Only those two levels are kept; anything deeper would sit below what the
    album scan reaches. Files picked one by one come with no folder and are
    named after the first of them. Two folders that end alike get a number.
    """
    taken: set = set()
    destinations = {}
    for folder, members in folders.items():
        parts = [part for part in map(_folder_name, PurePosixPath(folder).parts) if part][-2:]
        parts = parts or [_folder_name(name_from_file(_first_track(members))) or "album"]
        name, number = parts[-1], 2
        while str(PurePosixPath(*parts)).casefold() in taken:
            parts[-1], number = f"{name} ({number})", number + 1
        taken.add(str(PurePosixPath(*parts)).casefold())
        destinations[folder] = root.joinpath(*parts)
    return destinations


def _first_track(members: Sequence[Tuple[str, BinaryIO]]) -> str:
    """The file name of the first audio file among ``members``, in episode order."""
    return min((_base_name(name) for name, _data in members if is_audio_name(name)),
               key=episode_order)


def _folder_name(name: str) -> str:
    """``name`` made fit to name a folder the album scan still finds.

    The scan passes over hidden folders, so a leading dot would hide the album.
    """
    return _base_name(name).lstrip(".")


def _base_name(name: Optional[str]) -> str:
    """The last part of a path a browser sent, safe to use as a file name.

    Browsers send folder uploads with "/" and some Windows ones with "\\";
    either would put a file somewhere other than the folder it is written to.
    """
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return "" if base in (".", "..") else base


def remove_old_uploads(upload_dir: Path, keep_days: int) -> Tuple[int, int]:
    """Delete spooled uploads older than ``keep_days``.

    Returns how many folders went and how many bytes that freed. A folder that
    cannot be read or removed is logged and left alone; housekeeping must never
    be the reason the app fails to start.
    """
    if keep_days <= 0 or not upload_dir.is_dir():
        return 0, 0

    cutoff = time.time() - keep_days * 86400
    removed = freed = 0

    for folder in sorted(upload_dir.iterdir()):
        try:
            if not folder.is_dir() or folder.stat().st_mtime >= cutoff:
                continue
            size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
            shutil.rmtree(folder)
        except OSError as error:
            log.warning("cannot clear the upload %s: %s", folder, error)
            continue
        removed += 1
        freed += size

    if removed:
        log.info("cleared %d upload(s), %.1f MB", removed, freed / 1048576)
    return removed, freed
