"""Tests for following what is on the Toniebox.

The parsing is tested by feeding the watcher the frames teddyCloud sends, so
these are the real payloads, not a paraphrase of them.
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from tonie_studio.boxstate import BoxWatcher, ruid_from_uid


def _frames(watcher, *events):
    """Feed (event, data) pairs the way the stream delivers them."""
    for event, data in events:
        watcher.handle(event, data)


@pytest.fixture
def watcher():
    return BoxWatcher("http://teddycloud.test")


class TestRuidFromUid:
    def test_reverses_the_byte_order(self):
        # teddyCloud reports a tag on the box as the plain UID, but names the
        # same tag in its content tree by the reversed one. Matching them up
        # without this is silently looking at the wrong tonie.
        assert ruid_from_uid("E00403500EEDC9A9") == "a9c9ed0e500304e0"

    def test_is_its_own_inverse(self):
        assert ruid_from_uid(ruid_from_uid("E00403500EEDC9A9")) == "e00403500eedc9a9"

    def test_refuses_something_that_is_not_a_uid(self):
        assert ruid_from_uid("nonsense") is None
        assert ruid_from_uid("") is None


class TestPlacing:
    def test_a_valid_tag_lands_on_the_box(self, watcher):
        _frames(watcher, ("TagValid", "E00403500EEDC9A9"))

        state = watcher.state
        assert state.on_box is True
        assert state.ruid == "a9c9ed0e500304e0"

    def test_remembers_when_it_was_placed(self, watcher):
        before = datetime.now(timezone.utc)

        _frames(watcher, ("TagValid", "E00403500EEDC9A9"))

        assert watcher.state.since >= before

    def test_a_second_tonie_replaces_the_first(self, watcher):
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("TagValid", "E00403500EEDB0A5"))

        assert watcher.state.ruid == "a5b0ed0e500304e0"

    def test_an_unknown_tag_is_not_a_tonie_on_the_box(self, watcher):
        # TagInvalid means teddyCloud does not know the tag. It matters for
        # learning a new sticker, but nothing is playing.
        _frames(watcher, ("TagInvalid", "E00403500EEDFFFF"))

        assert watcher.state.on_box is False
        assert watcher.state.unknown_ruid == "ffffed0e500304e0"


class TestRemoving:
    def test_the_raw_log_reports_the_tonie_being_lifted(self, watcher):
        # teddyCloud emits no named event for this: tbs_tag_removed is the one
        # state change that never reaches SSE by name. It is only in the raw
        # RTNL log, as function group 15, function 16067, with the tag id's
        # halves swapped the way a real box sends it.
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("rtnl-raw-log2",
                 '{"function_group":15,"function":16067,"field6":"0EEDC9A9E0040350"}'))

        assert watcher.state.on_box is False
        assert watcher.state.ruid == "a9c9ed0e500304e0"

    def test_knows_the_older_box_generation_too(self, watcher):
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("rtnl-raw-log2",
                 '{"function_group":15,"function":8630,"field6":"0EEDC9A9E0040350"}'))

        assert watcher.state.on_box is False

    def test_ignores_raw_entries_that_mean_something_else(self, watcher):
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("rtnl-raw-log2", '{"function_group":12,"function":15426}'),
                ("rtnl-raw-log2", '{"function_group":15,"function":16065}'))

        assert watcher.state.on_box is True

    def test_playback_stopping_does_not_mean_removed(self, watcher):
        # Playback also stops when the content simply ends, so treating it as
        # a removal would take the tonie off the shelf while it is still on
        # the box.
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("playback", "stopped"))

        assert watcher.state.on_box is True
        assert watcher.state.playing is False

    def test_playback_starting_is_recorded(self, watcher):
        _frames(watcher, ("TagValid", "E00403500EEDC9A9"), ("playback", "started"))

        assert watcher.state.playing is True


class TestFraming:
    def test_reads_the_json_envelope_teddycloud_wraps_around_it(self, watcher):
        watcher.feed('event: TagValid')
        watcher.feed('data: { "type":"TagValid", "data":"E00403500EEDC9A9" }')
        watcher.feed('')

        assert watcher.state.ruid == "a9c9ed0e500304e0"

    def test_survives_a_payload_that_is_not_valid_json(self, watcher):
        # teddyCloud does not escape its payloads, so a title with a quote in
        # it produces malformed frames. One bad frame must not end the stream.
        watcher.feed('event: ContentTitle')
        watcher.feed('data: { "type":"ContentTitle", "data":"He said "hi"" }')
        watcher.feed('')
        _frames(watcher, ("TagValid", "E00403500EEDC9A9"))

        assert watcher.state.ruid == "a9c9ed0e500304e0"

    def test_ignores_the_keep_alive(self, watcher):
        _frames(watcher, ("keep-alive", ""))

        assert watcher.state.ruid is None


class TestStaleness:
    def test_a_box_that_went_quiet_is_no_longer_believed(self, watcher):
        # If the box loses power mid-play, the removal never arrives. Holding
        # the last tonie on screen for hours would be a lie.
        _frames(watcher, ("TagValid", "E00403500EEDC9A9"))
        watcher._state.last_event = datetime.now(timezone.utc) - timedelta(hours=3)

        assert watcher.state.on_box is True
        assert watcher.state.stale is True

    def test_a_fresh_event_is_not_stale(self, watcher):
        _frames(watcher, ("TagValid", "E00403500EEDC9A9"))

        assert watcher.state.stale is False

    def test_nothing_seen_yet_is_neither_on_nor_stale(self, watcher):
        assert watcher.state.ruid is None
        assert watcher.state.stale is False


class TestUnknownTagAges:
    def test_a_sticker_just_held_against_the_box_counts(self, watcher):
        watcher.handle("TagInvalid", "E00403500EEDFFFF")

        assert watcher.state.fresh_unknown == "ffffed0e500304e0"

    def test_one_from_hours_ago_does_not(self, watcher):
        # Remembered forever, it keeps looking like something happening now -
        # and the assignment page reloads itself over and over because of it.
        from datetime import datetime, timedelta, timezone

        watcher.handle("TagInvalid", "E00403500EEDFFFF")
        watcher._state.unknown_since = datetime.now(timezone.utc) - timedelta(hours=2)

        assert watcher.state.fresh_unknown is None
        assert watcher.state.unknown_ruid == "ffffed0e500304e0"

    def test_nothing_seen_is_nothing_reported(self, watcher):
        assert watcher.state.fresh_unknown is None


class TestReportingChanges:
    """What the listening log is told: changes, never repeats."""

    @pytest.fixture
    def heard(self):
        return []

    @pytest.fixture
    def reporting(self, heard):
        watcher = BoxWatcher("http://teddycloud.test",
                             on_change=lambda kind, ruid, at: heard.append((kind, ruid)))
        return watcher

    def test_a_tonie_going_on_is_reported(self, reporting, heard):
        reporting.handle("TagValid", "E00403500EEDC9A9")

        reporting.flush()

        assert heard == [("placed", "a9c9ed0e500304e0")]

    def test_the_same_tonie_reported_twice_is_one_placement(self, reporting, heard):
        # The box repeats TagValid for a tonie that stays put; counting each
        # would credit one evening as a dozen listens.
        reporting.handle("TagValid", "E00403500EEDC9A9")
        reporting.handle("TagValid", "E00403500EEDC9A9")

        reporting.flush()

        assert heard == [("placed", "a9c9ed0e500304e0")]

    def test_a_tonie_coming_off_is_reported(self, reporting, heard):
        reporting.handle("TagValid", "E00403500EEDC9A9")
        reporting.handle("rtnl-raw-log2",
                         '{"function_group":15,"function":16067,"field6":"0EEDC9A9E0040350"}')

        reporting.flush()

        assert heard[-1] == ("removed", "a9c9ed0e500304e0")

    def test_playback_is_reported_against_the_tonie_on_the_box(self, reporting, heard):
        reporting.handle("TagValid", "E00403500EEDC9A9")
        reporting.handle("playback", "started")
        reporting.handle("playback", "started")
        reporting.handle("playback", "stopped")

        reporting.flush()

        assert heard[1:] == [("started", "a9c9ed0e500304e0"),
                             ("stopped", "a9c9ed0e500304e0")]

    def test_a_listener_that_fails_does_not_end_the_stream(self, heard):
        def broken(kind, ruid, at):
            raise OSError("disk full")

        watcher = BoxWatcher("http://teddycloud.test", on_change=broken)
        watcher.handle("TagValid", "E00403500EEDC9A9")

        assert watcher.state.on_box is True


# --- what the real box sends ----------------------------------------------
#
# Everything above was written from teddyCloud's source. What follows replays
# frames recorded from a real box on 03.10.2026 (tests/fixtures), with the
# second each one arrived in. They showed two things the source reading had
# missed: the tag id in a raw log entry has its two 4-byte halves swapped, and
# the box sends its log in bundles, minutes after the fact.

import json as _json
from pathlib import Path as _Path

from tonie_studio.boxstate import uid_from_field6

FIXTURE = _Path(__file__).parent / "fixtures" / "box-sse-2026-10-03.json"
TIERE_RUID = "a9c9ed0e500304e0"


def _replay(watcher, clock):
    """Feed the recorded frames, moving the clock to each one's arrival."""
    for frame in _json.loads(FIXTURE.read_text()):
        hours, minutes, seconds = map(int, frame["arrived"].split(":"))
        clock.now = datetime(2026, 10, 3, hours, minutes, seconds, tzinfo=timezone.utc)
        watcher.feed(f"event: {frame['event']}")
        watcher.feed(f"data: {frame['data']}")
        watcher.feed("")


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class TestRealBox:
    def test_reads_the_tag_id_the_way_teddycloud_does(self):
        # teddyCloud reads the first four bytes as the low word and the next
        # four as the high one (read_big_endian64 in handler_rtnl.c).
        assert uid_from_field6("0EEDC9A9E0040350") == "E00403500EEDC9A9"

    def test_ignores_what_follows_the_tag_id(self):
        assert uid_from_field6("0EEDC9A9E004035001000000") == "E00403500EEDC9A9"

    def test_refuses_something_too_short_to_be_a_tag(self):
        assert uid_from_field6("DE0E0000") is None

    def test_on_off_on_with_the_same_tonie_is_three_changes(self):
        heard = []
        clock = _Clock()
        watcher = BoxWatcher("http://teddycloud.test", clock=clock,
                             on_change=lambda kind, ruid, at: heard.append((kind, ruid)))

        _replay(watcher, clock)
        watcher.flush()

        kinds = [kind for kind, ruid in heard if kind in ("placed", "removed")]
        assert kinds == ["placed", "removed"] * 4
        assert all(ruid == TIERE_RUID for _kind, ruid in heard)

    def test_times_come_from_the_box_not_from_when_the_bundle_arrived(self):
        # The first two rounds arrived together with the third, in the same
        # second, two minutes after they happened.
        heard = []
        clock = _Clock()
        watcher = BoxWatcher("http://teddycloud.test", clock=clock,
                             on_change=lambda kind, ruid, at: heard.append((kind, at)))

        _replay(watcher, clock)
        watcher.flush()

        tag_times = [at for kind, at in heard if kind in ("placed", "removed")]
        first_on, first_off = tag_times[0], tag_times[1]
        assert (first_off - first_on).total_seconds() == pytest.approx(2.35, abs=0.01)
        third_on = tag_times[4]
        assert (third_on - first_on).total_seconds() == pytest.approx(139.7, abs=0.1)

    def test_the_bundle_is_placed_where_the_box_said_it_happened(self):
        # The freshest frame of the last bundle ties the box's clock to the
        # wall: its own uptime arrived at 20:08:14.
        heard = []
        clock = _Clock()
        watcher = BoxWatcher("http://teddycloud.test", clock=clock,
                             on_change=lambda kind, ruid, at: heard.append((kind, at)))

        _replay(watcher, clock)
        watcher.flush()

        first_on = next(at for kind, at in heard if kind == "placed")
        assert first_on < datetime(2026, 10, 3, 20, 6, 0, tzinfo=timezone.utc)

    def test_the_page_state_follows_at_once(self):
        # Only the record waits for the bundle to be complete; what the page
        # shows as on the box must not.
        clock = _Clock()
        watcher = BoxWatcher("http://teddycloud.test", clock=clock)

        _replay(watcher, clock)

        assert watcher.state.on_box is False


class TestAStickerPlacedAfterAnUnknownOne:
    def test_a_known_sticker_placed_next_is_no_longer_hidden_by_the_unknown_one(self):
        # The placing page asks about an unknown sticker first; once a known
        # one lies on the box, the unknown one is not on it any more.
        watcher = BoxWatcher("http://teddycloud.test")
        watcher.handle("TagInvalid", "E00403500EEDFFFF")
        assert watcher.state.fresh_unknown is not None

        watcher.handle("TagValid", "E00403500EEDC9A9")

        assert watcher.state.fresh_unknown is None
        assert watcher.state.ruid == "a9c9ed0e500304e0"


class TestHowTheStreamFailed:
    """A stream teddyCloud accepted and then left silent is not teddyCloud being away."""

    def _fail_once(self, error):
        watcher = BoxWatcher("http://teddycloud.test")

        def listen():
            watcher._stop.set()
            raise error

        watcher._listen = listen
        watcher._run()
        return watcher.state

    def test_a_stream_that_goes_silent_is_told_apart(self):
        import requests

        state = self._fail_once(requests.exceptions.ReadTimeout("Read timed out"))

        assert state.connected is False and state.silent is True

    def test_a_teddycloud_that_is_not_there_is_not_silent(self):
        import requests

        state = self._fail_once(requests.exceptions.ConnectionError("Connection refused"))

        assert state.connected is False and state.silent is False


class TestTheStreamItself:
    """The HTTP side: every other test feeds lines in by hand."""

    class Response:
        def __init__(self, lines, status=200):
            self.lines, self.status_code, self.encoding = lines, status, None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode and self.encoding == "utf-8"
            yield from self.lines

    def recorded_lines(self):
        for frame in _json.loads(FIXTURE.read_text()):
            yield f"event: {frame['event']}"
            yield f"data: {frame['data']}"
            yield ""

    def test_the_recorded_stream_read_over_http_finds_the_tonie(self, monkeypatch):
        heard = []
        clock = _Clock()
        watcher = BoxWatcher("http://teddycloud.test", clock=clock,
                             on_change=lambda kind, ruid, at: heard.append(kind))
        asked = {}

        def get(url, **kwargs):
            asked.update(url=url, **kwargs)
            return self.Response(list(self.recorded_lines()))

        monkeypatch.setattr(requests, "get", get)

        watcher._listen()
        watcher.flush()

        assert asked["url"] == "http://teddycloud.test/api/sse" and asked["stream"] is True
        assert watcher.state.connected and "placed" in heard

    def test_an_error_answer_ends_the_stream_before_anything_is_read(self, monkeypatch):
        watcher = BoxWatcher("http://teddycloud.test")
        monkeypatch.setattr(requests, "get", lambda url, **kwargs: self.Response([], status=502))

        with pytest.raises(requests.HTTPError):
            watcher._listen()

        assert not watcher.state.connected
