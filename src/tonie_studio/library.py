"""How a taf in teddyCloud's library is named, and which sticker plays it.

A taf has two names. Its path in the library, ``/Lichterkinder/Halloween.taf``,
is what the file browser and the encoder use; the source a tag points at,
``lib://Lichterkinder/Halloween.taf``, is the same file as a tag names it.

The same path can also arrive in two spellings. The archive share hands back
decomposed umlauts and teddyCloud stores whatever it was given, so a tag can
name a taf in a form that looks identical and compares unequal. Two questions
treat that differently:

- Whether a sticker *plays* a file compares exactly. teddyCloud's file system
  takes a name byte for byte, so a sticker naming the other spelling plays
  nothing; calling it healthy would hide exactly the silent tonie the
  checkup is there to find. ``listed_path`` answers it.
- Whether two names *mean* the same audio - a taf still wanted by a sticker,
  an album already made, a search - compares through ``same``. Erring towards
  "the same" there keeps a file from being deleted or made twice.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, Optional, Set

from .teddycloud import TeddyCloudClient

SCHEME = "lib://"


def source_of(path: str) -> str:
    """The source a tag uses for the taf at library ``path``."""
    return f"{SCHEME}{path.lstrip('/')}"


def path_of(source: str) -> str:
    """The library path a library ``source`` names."""
    return "/" + source[len(SCHEME):]


def is_library(source: str) -> bool:
    """Whether ``source`` names a taf in the library, rather than a stream or nothing."""
    return (source or "").startswith(SCHEME)


def safe_library_path(path: str) -> bool:
    """Whether ``path`` names a place inside the library and nothing besides.

    For paths that come from outside this app - a backup, a sticker's source,
    a form - before anything is written or encoded there: no "..", ".", empty
    part, backslash or control character, which could lead out of the
    library or name something other than what is shown.
    """
    if not path.startswith("/") or "\\" in path:
        return False
    parts = path[1:].split("/")
    return all(part not in ("", ".", "..") and part.isprintable() for part in parts)


def same(name: str) -> str:
    """A path, source or file name in the one spelling two of them compare in."""
    return unicodedata.normalize("NFC", name or "")


def listed_path(client: TeddyCloudClient, path: str) -> Optional[str]:
    """``path`` as the library spells it, or None when no file there matches it.

    Only the spelling of umlauts may differ between ``path`` and the answer;
    a caller that needs the file a sticker plays compares the two exactly.
    Each folder is listed only once its parent has shown it: teddyCloud
    answers a listing of a folder that is not there by dropping the
    connection. A listing that fails raises TeddyCloudError rather than
    reading as "not there", which would call audio missing that is not.
    """
    parts = path.strip("/").split("/")
    spelled = ""
    for index, part in enumerate(parts):
        wanted_dir = index < len(parts) - 1
        names = [entry.get("name") or "" for entry in
                 client.list_dir(spelled or "/", special="library")
                 if bool(entry.get("isDir")) == wanted_dir and entry.get("name") not in (".", "..")]
        name = part if part in names else next(
            (name for name in names if same(name) == same(part)), None)
        if not part or name is None:
            return None
        spelled = f"{spelled}/{name}"
    return spelled


def played(sources: Iterable[str]) -> Set[str]:
    """The sources of the given tags, ready to look a taf's ``same(source)`` up in."""
    return {same(source) for source in sources if source}
