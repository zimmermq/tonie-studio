"""Copies of slow answers.

TtlCache reuses an answer for a while and loads a new one while the caller
waits. BackgroundCache is for pages that only decorate with an answer: a
teddyCloud that does not answer holds a request for its whole timeout,
three times with retries. A page that merely marks albums must not wait for
that, so it is handed the last copy at once, and a stale copy starts one
refresh in the background; the page after next shows the newer one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Generic, Hashable, Optional, Tuple, TypeVar

import requests

from .teddycloud import TeddyCloudError

log = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


class TtlCache(Generic[K, T]):
    """The answer of ``load`` for each key, reused for ``ttl`` seconds.

    A missing or stale answer is loaded while the caller waits; a load that
    raises is passed on and not kept, so the next call tries again. Callers
    missing the same key at once share one load: the first loads, the others
    wait for its answer - twenty cover requests arriving together would
    otherwise walk the archive twenty times. An answer whose load began
    before a ``forget`` is handed to its caller but not kept. ``clock`` is
    for tests.
    """

    def __init__(self, load: Callable[[K], T], ttl: float,
                 clock: Callable[[], float] = time.monotonic):
        self.load = load
        self.ttl = ttl
        self.clock = clock
        self._answers: Dict[K, Tuple[float, T]] = {}
        # Counts forgets, so a load can tell one happened while it ran.
        self._generation = 0
        self._lock = threading.Lock()
        # One per key being loaded, held for the load.
        self._loading: Dict[K, threading.Lock] = {}

    def _fresh(self, key: K) -> Optional[Tuple[float, T]]:
        with self._lock:
            kept = self._answers.get(key)
        return kept if kept and self.clock() - kept[0] < self.ttl else None

    def get(self, key: K) -> T:
        kept = self._fresh(key)
        if kept:
            return kept[1]
        with self._lock:
            loading = self._loading.setdefault(key, threading.Lock())
        with loading:
            # Another caller may have loaded it while this one waited.
            kept = self._fresh(key)
            if kept:
                return kept[1]
            now = self.clock()
            with self._lock:
                generation = self._generation
            value = self.load(key)
            with self._lock:
                if generation == self._generation:
                    self._answers[key] = (now, value)
            return value

    def forget(self) -> None:
        """Drop every answer, for a change that has to show on the next call."""
        with self._lock:
            self._answers.clear()
            self._generation += 1


class BackgroundCache(Generic[T]):
    """The last answer of ``load``, refreshed in the background, never waited for.

    A failed refresh keeps the last answer and is not retried for ``ttl``
    seconds either, so an absent teddyCloud is asked once a minute rather
    than on every page. ``name`` names the loading thread and the log line.
    """

    def __init__(self, load: Callable[[], T], name: str, ttl: float = 60.0):
        self.load = load
        self.name = name
        self.ttl = ttl
        self._value: Optional[T] = None
        self._at = float("-inf")
        self._loading = False
        self._lock = threading.Lock()

    def get(self) -> Optional[T]:
        """The last answer, or None before the first; never blocks."""
        with self._lock:
            start = time.monotonic() - self._at > self.ttl and not self._loading
            if start:
                self._loading = True
            value = self._value
        if start:
            threading.Thread(target=self._refresh, name=self.name, daemon=True).start()
        return value

    def _refresh(self) -> None:
        value = None
        try:
            value = self.load()
        except (TeddyCloudError, requests.RequestException, OSError) as error:
            # teddyCloud or the network being away: the page simply goes without.
            log.info("the %s are not to be had this time: %s", self.name, error)
        except Exception:
            # Anything else is a bug, which would leave the page without for
            # good; it needs its traceback, not a line among the weather.
            log.exception("loading the %s failed", self.name)
        finally:
            with self._lock:
                if value is not None:
                    self._value = value
                self._at = time.monotonic()
                self._loading = False
