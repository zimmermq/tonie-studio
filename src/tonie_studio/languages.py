"""Which language a tonie speaks, grouped the way a person picks one.

teddyCloud's catalogue names a tonie's language with its region, as the shop
it came from does: de-de, de-ch, en-us, en-gb. Someone choosing what to play
asks for German or English, not for the shop, so the lists group by the
language alone. Nothing known about a tonie is a group of its own rather than
a guess.
"""

from __future__ import annotations

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
