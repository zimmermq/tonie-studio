"""Tests for the copies of slow answers that pages decorate with."""

from web_helpers import eventually

from tonie_studio.cache import BackgroundCache


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
