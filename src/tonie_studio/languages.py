"""Which language a tonie speaks, grouped the way a person picks one.

teddyCloud's catalogue names a tonie's language with its region, as the shop
it came from does: de-de, de-ch, en-us, en-gb. Someone choosing what to play
asks for German or English, not for the shop, so the lists group by the
language alone. Nothing known about a tonie is a group of its own rather than
a guess.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Counter, List, Optional

# Neither the catalogue nor a model of this app says what the tonie speaks.
NONE = "none"
# A language the app has no name for; its chip would otherwise read as a key.
OTHER = "other"

# Offered first, in this order: the languages tonies are mostly sold in here.
FIRST = ("de", "en", "fr")
# Every language with a name in the translations, as "language.<code>".
NAMED = FIRST + ("da", "es", "it", "nl", "pl", "sv")


def spoken(code: Optional[str]) -> str:
    """The language part of a catalogue language such as "de-ch".

    NONE when nothing is said, OTHER for a language without a name here.
    """
    language = (code or "").strip().lower().split("-", 1)[0]
    if not language:
        return NONE
    return language if language in NAMED else OTHER


# How a language picked for an own model is stored: the way the catalogue
# writes it, with the region the shop of that language mostly uses.
STORED = {"de": "de-de", "en": "en-us", "fr": "fr-fr", "da": "da-dk", "es": "es-es",
          "it": "it-it", "nl": "nl-nl", "pl": "pl-pl", "sv": "sv-se"}
DEFAULT = "de-de"


def chosen(language: str, before: str = "") -> str:
    """What to store for ``language`` picked from NAMED, where ``before`` was stored so far.

    The same language as before keeps its region: en-gb picked as English
    stays en-gb. Anything not offered keeps what was there, or is German.
    """
    if language not in STORED:
        return before or DEFAULT
    if before and spoken(before) == language:
        return before
    return STORED[language]


# Folder names that say which language a collection of dumps speaks: each
# language of NAMED in English, in German and in itself, without accents, as
# _folded leaves them, umlauts both ways.
FOLDER_NAMES = {
    "de": ("german", "deutsch"),
    "en": ("english", "englisch"),
    "fr": ("french", "franzosisch", "franzoesisch", "francais"),
    "da": ("danish", "danisch", "daenisch", "dansk"),
    "es": ("spanish", "spanisch", "espanol"),
    "it": ("italian", "italienisch", "italiano"),
    "nl": ("dutch", "niederlandisch", "niederlaendisch", "nederlands"),
    "pl": ("polish", "polnisch", "polski"),
    "sv": ("swedish", "schwedisch", "svenska"),
}
# A folder named by a code, with or without its region: en, en-gb, de_CH.
_FOLDER_CODE = re.compile(r"([a-z]{2})(?:[-_]([a-z]{2}))?")


def from_path(path: str) -> Optional[str]:
    """The language the folders of ``path`` name, as stored; None when none does.

    Only a whole folder name counts, never the file name: "English/Peppa/x.nfc"
    is English, "Englisch lernen/x.nfc" and "Comics/English.nfc" are nothing.
    The folder nearest the top decides, since collections are sorted by
    language first. A code keeps its region (en-gb), a name takes the region
    STORED gives it.
    """
    for folder in PurePosixPath(path).parent.parts:
        language = _folder_language(folder)
        if language:
            return language
    return None


def _folder_language(folder: str) -> Optional[str]:
    text = folder.strip().lower()
    code = _FOLDER_CODE.fullmatch(text)
    if code and code.group(1) in STORED:
        return f"{code.group(1)}-{code.group(2)}" if code.group(2) else STORED[code.group(1)]
    folded = _folded(text)
    for language, names in FOLDER_NAMES.items():
        if folded in names:
            return STORED[language]
    return None


def _folded(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def offered(counts: Counter) -> List[str]:
    """The languages worth a chip, in the order they are shown.

    Nothing to choose from - a single language, or none at all - offers none:
    a chip that changes nothing helps nobody.
    """
    present = [language for language, count in counts.items() if count]
    if len(present) < 2:
        return []
    rest = sorted(language for language in present if language not in FIRST + (OTHER, NONE))
    tail = [language for language in (OTHER, NONE) if language in present]
    return [language for language in FIRST if language in present] + rest + tail
