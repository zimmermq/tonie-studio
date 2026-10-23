"""What the tonies.com shop says about a tonie: description, age, running time, picture.

The catalogue teddyCloud keeps (tonies.json) names a tonie and lists its
tracks, but has no description, age recommendation or link, and knows
nothing of tonies it does not list, such as new Cuddle Tonies. The shop
does, and its pages fetch their products as JSON from its own search
service, which this module asks the same way: by article number (the
catalogue's model, the shop's salesId) for a tonie the catalogue names, and by
the words of its name for one it does not. One answer carries everything:
picture, description, age, running time, genre, themes, link and the titles
of the chapters.

The service is the shop's own and not documented. Should it change, nothing
here breaks: a lookup that fails is no answer, and the pages then simply show
less. Only the German shop is asked, the one the de-de catalogue entries
belong to.

A tonie's answer is kept beside the app's data, so the shop is asked once
per tonie; one it does not know is asked again after MISS_DAYS, since the
shop gains products over time. Many tonies are asked for in one request.
"""

from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from .store import LOCK, changing_json, from_record, read_json

log = logging.getLogger(__name__)

BASE = "https://tonies.com"
API = "https://api.prod.shop.tonies.com/api/os/products/"
SHOP_LOCALE = "de-DE"
LANGUAGE = "de-de"
FILE_NAME = "shop.json"

# How long a tonie the shop does not know is left alone before asking again.
MISS_DAYS = 30
# Which way of reading an answer a kept one was read with. An answer kept
# under an older one is asked for again: until 2 the picture was the shop's
# first, the figure beside its CD cover, rather than the figure alone.
RECORD = 2

# A search for a word of the name returns this many products at most. The
# service matches any word anywhere, also in descriptions, so the ranking
# here sorts out which of them are meant.
SEARCH_LIMIT = 30

# How many requests one search makes at most: a word each, the longest first,
# and the umlaut spelling of a word a dump wrote out ("gefuehle"), since the
# shop does not always find a word spelled out.
SEARCH_REQUESTS = 4

# How many candidates a search hands back.
MOST_FOUND = 6

# How many products one request asks for by article number. The service
# answers 200 in well under a second; a list of every tonie takes a few.
BATCH = 100

TIMEOUT = 15

# Words too common to say anything about which tonie is meant.
_COMMON = frozenset("""
    und mit der die das den dem des ein eine einer von vom fuer fur zum zur auf aus bei
    tonie tonies folge teil set and the with
""".split())

Fetch = Callable[[str, Dict[str, str]], Dict[str, Any]]


@dataclass
class ShopInfo:
    sales_id: str
    url: str = ""
    name: str = ""
    series: str = ""
    picture: str = ""
    description: str = ""  # cleaned HTML, safe to put on a page as it is
    age_min: Optional[int] = None
    minutes: Optional[int] = None
    genre: str = ""
    genre_key: str = ""  # such as audio-play, the catalogue's category for it
    themes: List[str] = field(default_factory=list)
    tracks: List[str] = field(default_factory=list)


def _fold(text: str) -> str:
    text = text.lower()
    for umlaut, spelled in (("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"), ("\u00df", "ss")):
        text = text.replace(umlaut, spelled)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _words(text: str) -> List[str]:
    return [word for word in re.findall(r"[a-z0-9]+", _fold(text))
            if len(word) >= 3 and word not in _COMMON]


def search_words(series: str, episode: str) -> List[str]:
    """The words of a name worth searching the shop for, as written, the longest first.

    The shop searches the spelling it is given: "Löwen" finds what "loewen"
    does not, so the words keep their umlauts.
    """
    words = []
    for word in re.findall(r"\w+", f"{series} {episode}".lower()):
        folded = _fold(word)
        if len(folded) >= 4 and folded not in _COMMON and word not in words:
            words.append(word)
    return sorted(words, key=len, reverse=True)


def _queries(words: List[str]) -> List[str]:
    """What to ask the shop for: each word, and its umlaut spelling where a dump wrote it out."""
    queries = []
    for word in words:
        queries.append(word)
        if re.search(r"(ae|oe|ue)", word) and not re.search("[\u00e4\u00f6\u00fc]", word):
            queries.append(word.replace("ae", "\u00e4").replace("oe", "\u00f6")
                           .replace("ue", "\u00fc"))
    return queries[:SEARCH_REQUESTS]


# The shop names a product's pictures after their content: "_1_a" is the
# figure alone. The first picture shows the figure beside its CD cover, others
# the cover alone or the figure on a Toniebox. A product with only two pictures
# shows the figure alone first, then on a box, and there the names are not
# reliable: some call the box picture "_1_a". Its order is trusted instead.
_FIGURE_ALONE = re.compile(r"_1_a[-.]")


def _figure_picture(product: Dict[str, Any]) -> str:
    """The picture of the figure alone, as a tonie's picture is, else the shop's first one."""
    urls = [(image or {}).get("url") or "" for image in product.get("images") or []]
    if len(urls) == 2 and urls[0]:
        return urls[0]
    for url in urls:
        if _FIGURE_ALONE.search(url.rsplit("/", 1)[-1]):
            return url
    return (product.get("firstImage") or {}).get("url") or ""


def info_from(document: Dict[str, Any]) -> ShopInfo:
    """A product as the shop's search answers it, reduced to what a page shows."""
    product = document.get("_source", document)
    genre = product.get("genre") or {}
    tracks = product.get("tracks") or ""
    path = product.get("productUrl") or ""
    return ShopInfo(
        sales_id=str(product.get("salesId") or ""),
        url=f"{BASE}{path}" if path.startswith("/") else "",
        name=product.get("name") or "",
        series=(product.get("series") or {}).get("name") or "",
        picture=_figure_picture(product),
        description=clean_description(product.get("description") or ""),
        age_min=product.get("ageMin"),
        minutes=product.get("runTime"),
        genre=genre.get("name") or "",
        genre_key=genre.get("key") or "",
        themes=[theme.get("name") for theme in product.get("theme") or [] if theme.get("name")],
        tracks=[line.strip() for line in tracks.splitlines() if line.strip()]
        if isinstance(tracks, str) else [str(track) for track in tracks],
    )


class _Cleaner(HTMLParser):
    """Keeps text and a handful of formatting tags without their attributes."""

    KEEP = {"b", "strong", "i", "em", "p", "br", "ul", "ol", "li"}
    # Whose content is dropped along with the tag, not only the tag.
    DROP = {"script", "style", "iframe", "object", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.dropping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.dropping += 1
        elif tag in self.KEEP and not self.dropping:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.dropping = max(0, self.dropping - 1)
        elif tag in self.KEEP and tag != "br" and not self.dropping:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(html.escape(data, quote=False))


def clean_description(text: str) -> str:
    """The shop's description HTML reduced to plain formatting, safe to show.

    It comes from a service this app does not control, so only text and
    simple formatting tags survive: no attributes, no links, no scripts.
    """
    cleaner = _Cleaner()
    cleaner.feed(text or "")
    cleaner.close()
    return re.sub(r"<p>\s*</p>", "", "".join(cleaner.out)).strip()


def _get(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    response = requests.get(url, params=params, timeout=TIMEOUT,
                            headers={"User-Agent": "tonie-studio", "Accept": "application/json"})
    response.raise_for_status()
    return response.json()


# How long the shop is left alone after it did not answer, in seconds. Every
# view of the tonie list asks it; while it is away, each would wait for the
# timeout once per batch. Kept in memory only: the shop being away says
# nothing about the tonies.
AWAY_FOR = 10 * 60


class Shop:
    """Asks the shop about tonies and keeps what it said."""

    def __init__(self, data_dir: Path, fetch: Optional[Fetch] = None,
                 clock: Callable[[], float] = time.time):
        self.data_dir = data_dir
        # Looked up when the shop is made, not when this module loads, so a
        # test can keep the real shop out by replacing _get.
        self.fetch = fetch or _get
        self.clock = clock
        # Until when the shop is taken to be away; see AWAY_FOR.
        self._away_until = 0.0

    def lookup(self, sales_id: str, language: str = LANGUAGE) -> Optional[ShopInfo]:
        """What the shop says about the product with article number ``sales_id``."""
        if language != LANGUAGE or not sales_id:
            return None
        return self.lookup_many([sales_id]).get(sales_id)

    def lookup_many(self, sales_ids: Iterable[str]) -> Dict[str, ShopInfo]:
        """What the shop says about each of ``sales_ids`` it knows, by article number.

        Kept answers are used as they are and the rest asked for, BATCH at a
        time. A product the shop does not know is remembered as missing; a
        batch the shop did not answer is not remembered at all, since the
        shop being away says nothing about the tonies.
        """
        with LOCK:
            known = read_json(self.data_dir / FILE_NAME, {})
        found: Dict[str, ShopInfo] = {}
        ask = []
        for sales_id in dict.fromkeys(sales_id for sales_id in sales_ids if sales_id):
            kept = known.get(sales_id)
            if isinstance(kept, dict) and kept.get("sales_id") and kept.get("record") == RECORD:
                found[sales_id] = from_record(ShopInfo, kept)
            elif not (isinstance(kept, dict)
                      and self.clock() - kept.get("missed", 0) < MISS_DAYS * 86400):
                ask.append(sales_id)

        answers: Dict[str, Optional[ShopInfo]] = {}
        if self.clock() < self._away_until:
            ask = []
        for start in range(0, len(ask), BATCH):
            batch = ask[start:start + BATCH]
            try:
                answer = self.fetch(API, {"shopLocale": SHOP_LOCALE, "salesId": ",".join(batch),
                                          "limit": str(len(batch))})
            except (OSError, ValueError, requests.RequestException) as error:
                log.warning("tonies.com could not be asked about %d product(s), not again "
                            "for %d minutes: %s", len(batch), AWAY_FOR // 60, error)
                self._away_until = self.clock() + AWAY_FOR
                break
            infos = {info.sales_id: info
                     for info in map(info_from, answer.get("documents") or [])}
            answers.update({sales_id: infos.get(sales_id) for sales_id in batch})

        if answers:
            with changing_json(self.data_dir / FILE_NAME, {}) as known:
                for sales_id, info in answers.items():
                    known[sales_id] = (dict(asdict(info), record=RECORD) if info
                                       else {"missed": self.clock()})
            found.update({sales_id: info for sales_id, info in answers.items() if info})
        return found

    def search(self, series: str, episode: str) -> List[ShopInfo]:
        """The shop's products whose names share most words with this one, best first."""
        wanted = set(_words(f"{series} {episode}"))
        found: Dict[str, ShopInfo] = {}
        for word in _queries(search_words(series, episode)):
            try:
                answer = self.fetch(API, {"shopLocale": SHOP_LOCALE, "search": word,
                                          "limit": str(SEARCH_LIMIT)})
            except (OSError, ValueError, requests.RequestException) as error:
                log.warning("tonies.com could not be searched for %r: %s", word, error)
                break
            for document in answer.get("documents") or []:
                info = info_from(document)
                if info.sales_id:
                    found.setdefault(info.sales_id, info)

        def shared(info: ShopInfo) -> int:
            text = _fold(f"{info.name} {info.series}")
            return sum(1 for word in wanted if word in text)

        ranked = sorted((info for info in found.values() if shared(info)),
                        key=lambda info: (-shared(info), info.name.casefold()))
        return ranked[:MOST_FOUND]
