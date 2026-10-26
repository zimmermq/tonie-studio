"""Finding a podcast by name, to subscribe to or archive without hunting for its address.

Two places are asked: the ARD Audiothek, whose shows the archive reads in
full, and Apple's podcast directory, which knows the feed of nearly every
podcast. Both are public interfaces that answer a plain request.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote

import requests

from .podcast import LARGEST_FEED, FeedError, Fetch, download
from .podcast_archive import SHOW

log = logging.getLogger(__name__)

AUDIOTHEK = "https://api.ardaudiothek.de/search/programsets"
DIRECTORY = "https://itunes.apple.com/search"

# Results per place; more is a list nobody reads to the end.
RESULTS = 10

# How long each place may take to connect and to answer, in seconds. The page
# waits for the search, both places are asked at once, and the proxy in front
# of the app gives up after some fifteen seconds.
ANSWER_WITHIN = (4, 5)

# The places asked, as Found.where names them.
IN_AUDIOTHEK, IN_DIRECTORY = "audiothek", "directory"
PLACES = (IN_AUDIOTHEK, IN_DIRECTORY)


@dataclass
class Found:
    title: str
    # Who makes it: the station or the podcast's author.
    by: str
    # What the archive takes: a feed or a link of an Audiothek show.
    source: str
    # The feed a subscription takes; "" where there is none, as for an Audiothek show.
    feed: str
    episodes: Optional[int]
    # One of PLACES.
    where: str


def search(query: str, fetch: Fetch = download) -> Tuple[List[Found], List[str]]:
    """What both places know of ``query``, and the places that did not answer.

    A place that fails is named, not raised: what the other found is still worth showing.
    """
    query = query.strip()
    if not query:
        return [], []
    places = ((IN_AUDIOTHEK, _audiothek), (IN_DIRECTORY, _directory))
    with ThreadPoolExecutor(len(places)) as pool:
        asked = [(where, pool.submit(ask, query, fetch)) for where, ask in places]
    found, failed = [], []
    for where, answer in asked:
        try:
            found += answer.result()
        except (FeedError, requests.RequestException, ValueError, KeyError, TypeError) as error:
            log.warning("podcast search in %s failed: %s", where, error)
            failed.append(where)
    return found, failed


def _audiothek(query: str, fetch: Fetch) -> List[Found]:
    answer = json.loads(fetch(f"{AUDIOTHEK}?query={quote(query)}&limit={RESULTS}", LARGEST_FEED,
                              timeout=ANSWER_WITHIN))
    nodes = answer["data"]["search"]["programSets"]["nodes"] or []
    found = []
    for node in nodes:
        link = node.get("sharingUrl") or ""
        if not SHOW.search(link):
            continue
        found.append(Found(title=node.get("title") or "", source=link, feed="",
                           by=(node.get("publicationService") or {}).get("title") or "",
                           episodes=node.get("numberOfElements"), where=IN_AUDIOTHEK))
    return found


def _directory(query: str, fetch: Fetch) -> List[Found]:
    answer = json.loads(fetch(f"{DIRECTORY}?term={quote(query)}&entity=podcast&country=de"
                              f"&limit={RESULTS}", LARGEST_FEED, timeout=ANSWER_WITHIN))
    found = []
    for result in answer.get("results") or []:
        feed = result.get("feedUrl") or ""
        if not feed:
            continue
        found.append(Found(title=result.get("collectionName") or "", source=feed, feed=feed,
                           by=result.get("artistName") or "",
                           episodes=result.get("trackCount"), where=IN_DIRECTORY))
    return found
