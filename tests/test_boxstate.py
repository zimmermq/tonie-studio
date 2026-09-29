"""Tests for following what is on the Toniebox.

The parsing is tested by feeding the watcher the frames teddyCloud sends, so
these are the real payloads, not a paraphrase of them.
"""

from datetime import datetime, timedelta, timezone

import pytest

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
        # RTNL log, as function group 15, function 16067.
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("rtnl-raw-log2",
                 '{"function_group":15,"function":16067,"field6":"E00403500EEDC9A9"}'))

        assert watcher.state.on_box is False
        assert watcher.state.ruid == "a9c9ed0e500304e0"

    def test_knows_the_older_box_generation_too(self, watcher):
        _frames(watcher,
                ("TagValid", "E00403500EEDC9A9"),
                ("rtnl-raw-log2",
                 '{"function_group":15,"function":8630,"field6":"E00403500EEDC9A9"}'))

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
