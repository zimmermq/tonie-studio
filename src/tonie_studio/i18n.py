"""Translations for everything a person reads, on screen and on paper.

Each language is one JSON file in ``locales``, named by its code. Adding a
language means adding a file: the app finds it, offers it in the switcher, and
the tests check that it has every key the default has, with the same
placeholders. Nothing in the code lists the languages.

A file maps keys such as ``checkup.title`` to text. A text may hold
``{placeholders}``, filled in when it is used, and may instead be an object
with ``one`` and ``other`` for words that change with a number. The key
``_meta`` carries the language's own name and how it writes numbers and dates.

Keys are names rather than German sentences used as keys. That is what lets a
test notice a translation that is missing, instead of the page quietly showing
the source language.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

LOCALES = Path(__file__).parent / "locales"

# The language of the interface as it was written, and the one used when
# nothing better is known.
DEFAULT = "de"

COOKIE = "lang"

_CODE = re.compile(r"^[a-z]{2,3}$")

Message = Union[str, Dict[str, str]]


@lru_cache(maxsize=1)
def catalogs() -> Dict[str, Dict[str, Message]]:
    """Every language file, by code. Read once; the files ship with the app."""
    found = {}
    for path in sorted(LOCALES.glob("*.json")):
        if _CODE.match(path.stem):
            found[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return found


def languages() -> List[Tuple[str, str]]:
    """(code, own name) of every language there is a file for."""
    return [(code, str(catalog.get("_meta", {}).get("name", code)))
            for code, catalog in catalogs().items()]


def negotiate(cookie: Optional[str], accept_language: Optional[str]) -> str:
    """The language to answer in: the one chosen, else the browser's, else the default."""
    known = catalogs()
    if cookie and cookie in known:
        return cookie
    wanted = []
    for part in (accept_language or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        code, _, rest = piece.partition(";")
        quality = 1.0
        if rest.strip().startswith("q="):
            try:
                quality = float(rest.strip()[2:])
            except ValueError:
                quality = 0.0
        wanted.append((quality, code.strip().lower().split("-")[0]))
    for _quality, code in sorted(wanted, key=lambda item: -item[0]):
        if code in known:
            return code
    return DEFAULT


class Translator:
    """Turns keys into text in one language, with the default as fallback."""

    def __init__(self, lang: str):
        known = catalogs()
        self.lang = lang if lang in known else DEFAULT
        self._messages = known[self.lang]
        self._fallback = known[DEFAULT]
        meta = self._messages.get("_meta", {})
        self._decimal = meta.get("decimal", ".")
        self._date = meta.get("date", "%Y-%m-%d %H:%M")

    def __call__(self, key: str, count: Optional[int] = None, **params) -> str:
        message = self._messages.get(key, self._fallback.get(key))
        if message is None:
            log.warning("no text for %r", key)
            return key
        if isinstance(message, dict):
            message = message.get("one" if count == 1 else "other", "")
        try:
            return message.format(count=count, **params)
        except (KeyError, IndexError, ValueError) as error:
            log.warning("text %r in %s cannot be filled: %s", key, self.lang, error)
            return message

    def number(self, value: float, digits: int = 1) -> str:
        return f"{value:.{digits}f}".replace(".", self._decimal)

    def size(self, value: int) -> str:
        """A file size as people read it: 1.2 MB, 340 KB."""
        if value >= 1024 * 1024:
            return f"{self.number(value / (1024 * 1024))} MB"
        if value >= 1024:
            return f"{round(value / 1024)} KB"
        return self("unit.bytes", count=value)

    def when(self, value: Optional[datetime]) -> str:
        """A moment in the container's time zone, written the language's way."""
        if value is None:
            return "-"
        return value.astimezone().strftime(self._date)

    def duration(self, value) -> str:
        """A listening time as a person says it: 1 h 24 min, 24 min."""
        if not value:
            return "-"
        total = int(value.total_seconds() // 60)
        if total < 1:
            return self("unit.under_a_minute")
        hours, rest = divmod(total, 60)
        return f"{hours} h {rest} min" if hours else f"{rest} min"

    def strings(self, *keys: str) -> Dict[str, Message]:
        """Raw texts for a page's own script, which fills in its own values."""
        return {key: self._messages.get(key, self._fallback.get(key, key)) for key in keys}


# The language problems are written in for the log, which is read by whoever
# runs the app rather than by whoever uses it.
LOG_LANGUAGE = "en"


class Problem(Exception):
    """Something gone wrong that a person reads, said in their language when shown.

    It carries a key and its values rather than finished text, because where it
    is raised nobody knows who will read it. ``str()`` gives the log's language.
    """

    def __init__(self, key: str, **params):
        self.key = key
        self.params = params
        super().__init__(key)

    def __str__(self) -> str:
        return self.say(Translator(LOG_LANGUAGE))

    def say(self, translate: Translator) -> str:
        """The text in ``translate``'s language; a value that is itself a
        problem, such as the cause of this one, is said in it too."""
        params = {name: value.say(translate) if isinstance(value, Problem) else value
                  for name, value in self.params.items()}
        return translate(self.key, **params)
