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

log = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


class TtlCache(Generic[K, T]):
    """The answer of ``load`` for each key, reused for ``ttl`` seconds.

    A missing or stale answer is loaded while the caller waits; a load that
    raises is passed on and not kept, so the next call tries again. Two
    callers missing at once may both load, which costs time, never a wrong
    answer. An answer whose load began before a ``forget`` is handed to its
    caller but not kept. ``clock`` is for tests.
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

    def get(self, key: K) -> T:
        now = self.clock()
        with self._lock:
            kept = self._answers.get(key)
            generation = self._generation
        if kept and now - kept[0] < self.ttl:
            return kept[1]
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
        except Exception as error:  # whatever it is, the page simply goes without
            log.info("the %s are not to be had this time: %s", self.name, error)
        finally:
            with self._lock:
                if value is not None:
                    self._value = value
                self._at = time.monotonic()
                self._loading = False
