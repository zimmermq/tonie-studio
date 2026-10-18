"""How a taf in teddyCloud's library is named, and which sticker plays it.

A taf has two names. Its path in the library, ``/Lichterkinder/Halloween.taf``,
is what the file browser and the encoder use; the source a tag points at,
``lib://Lichterkinder/Halloween.taf``, is the same file as a tag names it.

The same path can also arrive in two spellings. The archive share hands back
decomposed umlauts and teddyCloud stores whatever it was given, so a tag can
name a taf in a form that looks identical and compares unequal. Everything here
that compares paths or sources compares them through ``same``.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, Set

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


def played(sources: Iterable[str]) -> Set[str]:
    """The sources of the given tags, ready to look a taf's ``same(source)`` up in."""
    return {same(source) for source in sources if source}
