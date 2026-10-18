"""Tests for the copies of slow answers that pages decorate with."""

import pytest
from web_helpers import eventually

from tonie_studio.cache import BackgroundCache, TtlCache


class TestBackgroundCache:
    def test_answers_nothing_until_the_first_load_and_never_waits(self):
        import threading

        gate = threading.Event()
        cache = BackgroundCache(lambda: gate.wait(5) and "loaded", name="test")

        assert cache.get() is None
        gate.set()
        eventually(lambda: cache.get() == "loaded")

    def test_a_failed_load_leaves_the_last_answer(self):
        answers = iter(["first"])

        def load():
            return next(answers)  # the second call raises StopIteration

        cache = BackgroundCache(load, name="test", ttl=0)
        cache.get()
        eventually(lambda: cache.get() == "first")
        cache.get()

        assert cache.get() == "first"

    @pytest.mark.parametrize("error, level", [
        ("away", "INFO"),
        ("bug", "ERROR"),
    ])
    def test_teddycloud_away_is_weather_but_a_bug_is_an_error(self, caplog, error, level):
        import logging

        from tonie_studio.teddycloud import TeddyCloudError

        def load():
            raise TeddyCloudError("unreachable") if error == "away" else KeyError("models")

        caplog.set_level(logging.INFO, logger="tonie_studio.cache")
        cache = BackgroundCache(load, name="models", ttl=0)
        cache.get()
        # By the cache's name: a refresh of another test may still be logging.
        ours = lambda: [r for r in caplog.records  # noqa: E731
                        if r.name == "tonie_studio.cache" and "models" in r.getMessage()]
        eventually(ours)

        (record,) = ours()
        assert record.levelname == level
        assert (record.exc_info is not None) == (error == "bug")


class TestTtlCache:
    @staticmethod
    def counting(clock):
        loads = []
        cache = TtlCache(lambda key: loads.append(key) or key.upper(), ttl=10, clock=clock)
        return cache, loads

    def test_an_answer_is_reused_until_it_is_too_old(self):
        now = [0.0]
        cache, loads = self.counting(lambda: now[0])

        assert cache.get("a") == "A" and cache.get("a") == "A"
        now[0] = 11
        cache.get("a")

        assert loads == ["a", "a"]

    def test_each_key_has_its_own_answer(self):
        cache, loads = self.counting(lambda: 0.0)

        cache.get("a")
        cache.get("b")

        assert loads == ["a", "b"]

    def test_forgetting_loads_again(self):
        cache, loads = self.counting(lambda: 0.0)

        cache.get("a")
        cache.forget()
        cache.get("a")

        assert loads == ["a", "a"]

    def test_a_failed_load_is_not_kept(self):
        def load(key):
            calls.append(key)
            if len(calls) == 1:
                raise OSError("share away")
            return "back"

        calls = []
        cache = TtlCache(load, ttl=10, clock=lambda: 0.0)

        with pytest.raises(OSError):
            cache.get("a")

        assert cache.get("a") == "back"

    def test_an_answer_loaded_across_a_forget_is_not_kept(self):
        # A scan that began before an upload must not put the old list back.
        cache = None

        def load(key):
            calls.append(key)
            if len(calls) == 1:
                cache.forget()
                return "before"
            return "after"

        calls = []
        cache = TtlCache(load, ttl=10, clock=lambda: 0.0)

        assert cache.get("a") == "before"
        assert cache.get("a") == "after"


    def test_callers_missing_at_once_share_one_load(self):
        # Twenty cover requests arriving together would each walk the share.
        import threading
        import time

        loads = []

        def slow(key):
            loads.append(key)
            time.sleep(0.05)
            return key.upper()

        cache = TtlCache(slow, ttl=10)
        start = threading.Barrier(10, timeout=5)
        answers = []

        def ask():
            start.wait()
            answers.append(cache.get("a"))

        threads = [threading.Thread(target=ask) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert loads == ["a"] and answers == ["A"] * 10
