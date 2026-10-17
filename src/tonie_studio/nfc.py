"""Reading Flipper Zero dumps of tonies.

A tonie is an ISO 15693 tag. Two things in a dump made with a Flipper Zero
matter to teddyCloud: the tag's UID, and its eight data blocks, which hold the
32-byte token the Tonies cloud asks for before it hands out the audio. The
token belongs to that one figure, not to the title; two copies of the same
tonie have different UIDs and different tokens.

teddyCloud names a tonie after its UID read backwards (the "rUID"): the first
eight hex digits are the content folder, the last eight the file in it. The
dump writes the UID the other way round, so it is reversed here once.

Only the two lines that matter are read. Everything else in a dump (version,
block count, passwords, the comments the Flipper writes) differs between
firmware versions and is ignored, so a dump from an older Flipper still reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .i18n import Problem

_HEX_BYTE = re.compile(r"^[0-9a-fA-F]{2}$")
_HEX = re.compile(r"^[0-9a-fA-F]*$")

# The token is the whole of the eight four-byte blocks.
AUTH_BYTES = 32


class DumpError(Problem, ValueError):
    """A file that is not a usable dump of a tonie."""


@dataclass(frozen=True)
class Dump:
    """One tonie as read from its dump."""

    ruid: str  # lower case, the form teddyCloud's content API takes
    auth: str  # upper-case hex, as teddyCloud writes it into the content JSON

    @property
    def folder(self) -> str:
        return self.ruid[:8].upper()

    @property
    def file(self) -> str:
        return self.ruid[8:].upper()


def parse_dump(text: str) -> Dump:
    """The tonie in a dump's ``text``; raises :class:`DumpError` if it has none."""
    values = {}
    for line in text.lstrip("\ufeff").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in ("UID", "Data Content"):
            values[key.strip()] = value.strip()

    uid, data = values.get("UID"), values.get("Data Content")
    if not uid or not data:
        raise DumpError("nfc.missing_fields")

    uid_bytes = uid.split()
    if len(uid_bytes) != 8 or not all(_HEX_BYTE.match(byte) for byte in uid_bytes):
        raise DumpError("nfc.bad_uid")

    auth = "".join(data.split())
    if len(auth) != 2 * AUTH_BYTES or not _HEX.match(auth):
        raise DumpError("nfc.bad_auth", size=AUTH_BYTES)

    return Dump(ruid="".join(reversed(uid_bytes)).lower(), auth=auth.upper())
