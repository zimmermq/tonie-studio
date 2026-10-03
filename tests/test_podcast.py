"""Tests for the podcast tonie: a sticker that brings the newest episodes."""

from datetime import datetime, timezone

import pytest

from fake_teddycloud import FakeTeddyCloud
from tonie_studio.podcast import (LARGEST_FEED, FeedError, Subscription, check_public,
                                  load_subscriptions, newest, parse_feed, refresh,
                                  save_subscriptions)
from tonie_studio.teddycloud import TeddyCloudError

RUID = "7ab4ed0e500304e0"
NOW = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Kakadu \xe2\x80\x93 Der Kinderpodcast</title>
    <itunes:image href="https://example.org/cover.jpg"/>
    <item>
      <title>Warum ist der Himmel blau?</title>
      <guid>ep-3</guid>
      <pubDate>Fri, 02 Oct 2026 06:00:00 +0200</pubDate>
      <enclosure url="https://example.org/ep3.mp3" type="audio/mpeg" length="1000"/>
      <itunes:duration>24:10</itunes:duration>
    </item>
    <item>
      <title>Wie schlafen Fische?</title>
      <guid>ep-2</guid>
      <pubDate>Fri, 25 Sep 2026 06:00:00 +0200</pubDate>
      <enclosure url="https://example.org/ep2.mp3" type="audio/mpeg" length="1000"/>
    </item>
    <item>
      <title>Ein Text ohne Ton</title>
      <guid>post-1</guid>
      <pubDate>Fri, 26 Sep 2026 06:00:00 +0200</pubDate>
    </item>
    <item>
      <title>Gr\xc3\xbc\xc3\x9fe aus dem Zoo</title>
      <guid>ep-1</guid>
      <pubDate>Fri, 18 Sep 2026 06:00:00 +0200</pubDate>
      <enclosure url="https://example.org/ep1.m4a" type="audio/x-m4a" length="1000"/>
    </item>
  </channel>
</rss>"""


class TestParsing:
    def test_reads_the_title_and_the_episodes_with_audio(self):
        feed = parse_feed(FEED)

        assert feed.title == "Kakadu \u2013 Der Kinderpodcast"
        assert [episode.guid for episode in feed.episodes] == ["ep-3", "ep-2", "ep-1"]

    def test_keeps_umlauts(self):
        assert parse_feed(FEED).episodes[-1].title == "Grüße aus dem Zoo"

    def test_reads_the_cover(self):
        assert parse_feed(FEED).image == "https://example.org/cover.jpg"

    def test_reads_a_playing_time(self):
        assert parse_feed(FEED).episodes[0].seconds == 24 * 60 + 10

    def test_refuses_something_that_is_not_a_feed(self):
        with pytest.raises(FeedError):
            parse_feed(b"<html><body>Nope</body></html>")

    def test_refuses_a_feed_with_entities_that_expand(self):
        # A feed is fetched from somewhere else; an entity bomb must not take
        # the app down with it.
        bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                b'<rss><channel><title>&b;</title></channel></rss>')

        with pytest.raises(FeedError):
            parse_feed(bomb)


class TestNewest:
    def test_newest_first_and_only_as_many_as_asked(self):
        episodes = newest(parse_feed(FEED), 2)

        assert [episode.guid for episode in episodes] == ["ep-3", "ep-2"]


class FakeWeb:
    """Answers downloads from memory and records what was asked for."""

    def __init__(self, feed=FEED):
        self.feed = feed
        self.asked = []

    def __call__(self, url, limit=None):
        self.asked.append((url, limit))
        if url.endswith("feed.xml"):
            return self.feed
        if url.endswith("cover.jpg"):
            return b"\xff\xd8\xffcover"
        return f"audio of {url}".encode()


@pytest.fixture
def cloud():
    cloud = FakeTeddyCloud()
    cloud.add_tag(RUID)
    return cloud


@pytest.fixture
def subscription():
    return Subscription(id="kakadu", feed="https://example.org/feed.xml", ruid=RUID, keep=2)


class TestRefresh:
    def test_puts_the_newest_episodes_on_the_tag(self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())

        entry = cloud.tag_entries[RUID]
        assert entry["source"].startswith("lib://Podcasts/")
        assert entry["nocloud"] is True
        assert len(cloud.encoded) == 1
        assert len(cloud.encoded[0][1]) == 2  # two chapters, the two newest

    def test_names_the_tonie_after_the_podcast(self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())

        model = cloud.custom_models[0]
        assert model["series"] == "Kakadu \u2013 Der Kinderpodcast"
        assert model["tracks"] == ["Warum ist der Himmel blau?", "Wie schlafen Fische?"]

    def test_nothing_new_means_nothing_is_encoded(self, cloud, subscription, tmp_path):
        web = FakeWeb()
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=web)

        changed = refresh(cloud, subscription, tmp_path, now=NOW, fetch=web)

        assert changed is False
        assert len(cloud.encoded) == 1

    def test_a_new_episode_replaces_the_old_taf_only_after_the_new_one_is_there(
            self, cloud, subscription, tmp_path):
        # The tag must never point at nothing: the new taf is built under a
        # new name, the tag is switched, and only then is the old one removed.
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())
        first = cloud.tag_entries[RUID]["source"]
        newer = FEED.replace(b"<guid>ep-3</guid>", b"<guid>ep-4</guid>")

        refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10), fetch=FakeWeb(newer))

        second = cloud.tag_entries[RUID]["source"]
        assert second != first
        assert "/" + first[len("lib://"):] not in cloud.files
        assert "/" + second[len("lib://"):] in cloud.files

    def test_a_failed_encode_leaves_the_old_episodes_playing(self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())
        first = cloud.tag_entries[RUID]["source"]
        cloud.encode_fails = "ffmpeg died"
        newer = FEED.replace(b"<guid>ep-3</guid>", b"<guid>ep-4</guid>")

        with pytest.raises(TeddyCloudError, match="ffmpeg died"):
            refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10), fetch=FakeWeb(newer))

        assert cloud.tag_entries[RUID]["source"] == first
        assert "/" + first[len("lib://"):] in cloud.files

    def test_leaves_no_downloads_behind(self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())

        assert not any(path.is_file() for path in (tmp_path / "podcasts").rglob("*"))


class TestStore:
    def test_subscriptions_survive_a_restart(self, tmp_path, subscription):
        save_subscriptions(tmp_path, [subscription])

        assert load_subscriptions(tmp_path) == [subscription]


def newer(feed=FEED):
    return feed.replace(b"<guid>ep-3</guid>", b"<guid>ep-4</guid>")


class TestRefreshEdges:
    def test_a_long_title_still_refreshes_more_than_once(self, cloud, subscription, tmp_path):
        # Names are cut at 120 characters; the part that tells two refreshes
        # apart must not be what gets cut.
        title = "Ein sehr langer Podcasttitel " * 5
        long_feed = FEED.replace(b"Kakadu \xe2\x80\x93 Der Kinderpodcast", title.encode())
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb(long_feed))

        changed = refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10),
                          fetch=FakeWeb(newer(long_feed)))

        assert changed is True

    def test_does_not_take_back_a_sticker_now_used_for_something_else(
            self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())
        cloud.tag_entries[RUID]["source"] = "lib://Lichterkinder/Schlaflieder.taf"

        with pytest.raises(FeedError, match="anderes"):
            refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10), fetch=FakeWeb(newer()))

        assert cloud.tag_entries[RUID]["source"] == "lib://Lichterkinder/Schlaflieder.taf"

    def test_keeps_the_old_taf_while_a_copied_sticker_still_plays_it(
            self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())
        first = cloud.tag_entries[RUID]["source"]
        cloud.add_tag("deb4ed0e500304e0", source=first, nocloud=True)

        refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10), fetch=FakeWeb(newer()))

        assert "/" + first[len("lib://"):] in cloud.files

    def test_a_failed_publish_leaves_no_orphaned_taf(self, cloud, subscription, tmp_path):
        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb())
        before = set(cloud.files)

        def broken_set_tag(ruid, **fields):
            raise TeddyCloudError("teddyCloud went away")

        cloud.set_tag = broken_set_tag
        with pytest.raises(TeddyCloudError):
            refresh(cloud, subscription, tmp_path, now=NOW.replace(day=10), fetch=FakeWeb(newer()))

        assert set(cloud.files) == before

    def test_the_feed_is_fetched_with_a_small_limit(self, cloud, subscription, tmp_path):
        # A feed is text; something hundreds of megabytes large is not one.
        web = FakeWeb()

        refresh(cloud, subscription, tmp_path, now=NOW, fetch=web)

        feed_limit = next(limit for url, limit in web.asked if url.endswith("feed.xml"))
        assert feed_limit == LARGEST_FEED

    def test_a_cover_with_an_odd_name_is_stored_as_a_picture(self, cloud, subscription, tmp_path):
        odd = FEED.replace(b"https://example.org/cover.jpg", b"https://example.org/cover.php?id=7")

        refresh(cloud, subscription, tmp_path, now=NOW, fetch=FakeWeb(odd))

        assert all(name.endswith((".jpg", ".jpeg", ".png", ".webp")) for name in cloud.images)


class TestOnlyPublicAddresses:
    """Episode addresses come from whoever wrote the feed. Fetched unchecked,
    they could point the pod at teddyCloud or anything else inside the cluster."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/x.mp3", "http://localhost/x.mp3", "http://10.1.2.3/x.mp3",
        "http://192.168.0.53/x.mp3", "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.mp3", "ftp://example.org/x.mp3", "file:///etc/passwd",
    ])
    def test_refuses_what_is_not_on_the_internet(self, url):
        with pytest.raises(FeedError):
            check_public(url)

    def test_refuses_a_name_that_resolves_inside(self):
        def inside(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("10.152.183.10", port))]

        with pytest.raises(FeedError):
            check_public("http://teddycloud-service/api/reboot", resolve=inside)

    def test_lets_a_public_address_through(self):
        def outside(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("93.184.215.14", port))]

        check_public("https://example.org/ep.mp3", resolve=outside)


class TestStoringSafely:
    def test_a_refresh_finishing_after_an_unsubscribe_does_not_bring_it_back(
            self, tmp_path, subscription):
        # The refresh job holds its own copy of the subscription; writing it
        # back must not undo an unsubscribe that happened meanwhile.
        from tonie_studio.podcast import add_subscription, remove_subscription, update_subscription

        add_subscription(tmp_path, subscription)
        running_copy = load_subscriptions(tmp_path)[0]
        remove_subscription(tmp_path, subscription.id)

        running_copy.updated = "2026-10-03"
        update_subscription(tmp_path, running_copy)

        assert load_subscriptions(tmp_path) == []

    def test_two_writers_do_not_lose_each_other(self, tmp_path):
        import threading

        from tonie_studio.podcast import add_subscription

        def add(number):
            add_subscription(tmp_path, Subscription(id=f"s{number}", feed="f", ruid=RUID))

        threads = [threading.Thread(target=add, args=(number,)) for number in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(load_subscriptions(tmp_path)) == 20
