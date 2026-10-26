"""Tests for the podcast archive: every episode of a podcast as its own taf in the library."""

import json
from datetime import datetime, timezone

import pytest
from fake_teddycloud import FakeTeddyCloud

import tonie_studio.podcast_archive as podcast_archive
from tonie_studio.encode import library_path
from tonie_studio.podcast import FeedError
from tonie_studio.podcast_archive import (Archive, ArchiveUpdater, add_archive, load_archives,
                                          read_source, refresh_archive, remove_archive,
                                          update_archive)
from tonie_studio.teddycloud import TeddyCloudError

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Kakadu</title>
    <item>
      <title>Wie schlafen Fische?</title>
      <guid>ep-2</guid>
      <pubDate>Fri, 25 Sep 2026 06:00:00 +0200</pubDate>
      <enclosure url="https://example.org/ep2.mp3" type="audio/mpeg"/>
      <itunes:image href="https://example.org/fische.jpg"/>
    </item>
    <item>
      <title>Gr\xc3\xbc\xc3\x9fe aus dem Zoo</title>
      <guid>ep-1</guid>
      <pubDate>Fri, 18 Sep 2026 06:00:00 +0200</pubDate>
      <enclosure url="https://example.org/ep1.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""

SHOW = "urn:ard:show:0e3f60ed4ba198df"
SHOW_LINK = f"https://www.ardaudiothek.de/sendung/unser-sandmaennchen/{SHOW}/"
EPISODE_LINK = "https://www.ardaudiothek.de/episode/urn:ard:episode:4f223e3b7c1dfe52/"


def node(number, title, when, picture="moffels"):
    return {"id": str(number), "title": title, "publicationStartDateAndTime": when,
            "duration": 390, "image": {"url1X1": f"https://img.example/{picture}?w={{width}}"},
            "audios": [{"url": f"https://media.example/{number}.mp3",
                        "downloadUrl": f"https://media.example/{number}.mp3"}]}


NODES = [node(103, "Märchen: Rumpelstilzchen", "2026-10-25T19:04:00+01:00"),
         node(102, "Jan und Henry: Der Affe", "2026-10-23T19:04:00+02:00"),
         node(101, "Aus Zappelduster wird Unser Sandmännchen", "2022-09-16T19:05:00+02:00")]


class FakeWeb:
    """Answers the feed, the Audiothek API and the episodes from memory."""

    def __init__(self, feed=FEED, nodes=None):
        self.feed = feed
        self.nodes = list(NODES if nodes is None else nodes)
        self.asked = []
        self.pictures_fail = False

    def __call__(self, url, limit=None):
        self.asked.append(url)
        if url.endswith("feed.xml"):
            return self.feed
        if url.startswith("https://api.ardaudiothek.de/items/urn:ard:episode:"):
            return json.dumps({"data": {"item": {"programSet": {"coreId": SHOW}}}}).encode()
        if url.startswith("https://img.example/") or url.endswith(".jpg"):
            if self.pictures_fail:
                raise FeedError("feed.not_found", host="img.example", detail="gone")
            return b"\xff\xd8\xff" + url.encode()
        if url.startswith(f"https://api.ardaudiothek.de/programsets/{SHOW}"):
            query = dict(part.split("=") for part in url.split("?", 1)[1].split("&"))
            offset, size = int(query["offset"]), int(query["limit"])
            return json.dumps({"data": {"programSet": {
                "coreId": SHOW, "title": "Unser Sandmännchen", "numberOfElements": len(self.nodes),
                "image": {"url1X1": "https://img.example/sandmann?w={width}"},
                "items": {"nodes": self.nodes[offset:offset + size]}}}}).encode()
        return f"audio of {url}".encode()


@pytest.fixture
def cloud():
    return FakeTeddyCloud()


def archive(source="https://example.org/feed.xml", **fields):
    return Archive(id="kakadu", source=source, **fields)


def stored_paths(cloud):
    return sorted(target for target, _sources in cloud.encoded)


def paths(entry):
    return {guid: stored.path for guid, stored in entry.archived.items()}


class TestReadingTheSource:
    def test_a_feed_gives_its_episodes(self):
        feed = read_source("https://example.org/feed.xml", FakeWeb())

        assert feed.title == "Kakadu"
        assert [episode.guid for episode in feed.episodes] == ["ep-2", "ep-1"]

    def test_an_audiothek_show_gives_every_episode(self):
        feed = read_source(SHOW_LINK, FakeWeb())

        assert feed.title == "Unser Sandmännchen"
        assert [episode.guid for episode in feed.episodes] == ["103", "102", "101"]
        assert feed.episodes[0].url == "https://media.example/103.mp3"
        assert feed.episodes[0].seconds == 390
        assert feed.episodes[0].published == datetime(2026, 10, 25, 18, 4, tzinfo=timezone.utc)

    def test_an_audiothek_show_is_read_page_by_page(self, monkeypatch):
        monkeypatch.setattr(podcast_archive, "AUDIOTHEK_PAGE", 2)
        web = FakeWeb()

        feed = read_source(SHOW_LINK, web)

        assert len(feed.episodes) == 3
        assert len([url for url in web.asked if "/programsets/" in url]) == 2

    def test_an_audiothek_episode_link_leads_to_its_show(self):
        feed = read_source(EPISODE_LINK, FakeWeb())

        assert feed.title == "Unser Sandmännchen"
        assert len(feed.episodes) == 3

    def test_the_new_name_of_the_audiothek_is_understood(self):
        feed = read_source(SHOW_LINK.replace("www.ardaudiothek.de", "www.ardsounds.de"), FakeWeb())

        assert feed.title == "Unser Sandmännchen"

    def test_an_audiothek_link_without_a_show_or_episode_is_refused(self):
        with pytest.raises(FeedError) as raised:
            read_source("https://www.ardaudiothek.de/rubrik/kinder/", FakeWeb())

        assert raised.value.key == "archive.audiothek_link"

    def test_the_square_picture_of_the_show_is_its_cover(self):
        feed = read_source(SHOW_LINK, FakeWeb())

        assert feed.image.startswith("https://img.example/sandmann?w=")
        assert "{width}" not in feed.image


class TestRefreshing:
    def test_every_episode_becomes_its_own_taf_in_a_folder_of_the_podcast(self, cloud, tmp_path):
        entry = archive()

        left = refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        assert left == 0
        assert stored_paths(cloud) == sorted([library_path("Kakadu", "Wie schlafen Fische?"),
                                              library_path("Kakadu", "Grüße aus dem Zoo")])
        assert all(len(sources) == 1 for _target, sources in cloud.encoded)
        assert paths(entry) == {"ep-2": library_path("Kakadu", "Wie schlafen Fische?"),
                                "ep-1": library_path("Kakadu", "Grüße aus dem Zoo")}

    def test_the_oldest_episodes_come_first_and_only_so_many_at_a_time(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)

        left = refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(), most=2)

        assert left == 1
        assert [target for target, _sources in cloud.encoded] == [
            library_path("Unser Sandmännchen", "Aus Zappelduster wird Unser Sandmännchen"),
            library_path("Unser Sandmännchen", "Jan und Henry: Der Affe")]

    def test_the_next_refresh_carries_on_where_the_last_stopped(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(), most=2)

        left = refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(), most=2)

        assert left == 0
        assert len(cloud.encoded) == 3
        assert len(entry.archived) == 3

    def test_nothing_new_means_nothing_is_encoded(self, cloud, tmp_path):
        entry = archive()
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        assert len(cloud.encoded) == 2

    def test_a_new_episode_is_added_and_the_old_ones_stay(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())
        newer = [node(104, "Herr Fuchs und Frau Elster", "2026-10-26T19:04:00+01:00")] + NODES

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(nodes=newer))

        assert len(cloud.encoded) == 4
        assert all(path in cloud.files for path in paths(entry).values())

    def test_the_folder_keeps_its_name_when_the_podcast_is_renamed(self, cloud, tmp_path):
        entry = archive()
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())
        renamed = FEED.replace(b"<title>Kakadu</title>", b"<title>Kakadu - neu</title>").replace(
            b"<guid>ep-2</guid>", b"<guid>ep-3</guid>")

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(renamed))

        assert entry.archived["ep-3"].path.startswith(library_path("Kakadu", "x")[:-len("x.taf")])

    def test_a_name_already_taken_gets_the_date_of_the_episode(self, cloud, tmp_path):
        cloud.dirs.add("/Kakadu")
        cloud.files[library_path("Kakadu", "Wie schlafen Fische?")] = b"another taf"
        entry = archive()

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        assert entry.archived["ep-2"].path == library_path("Kakadu",
                                                           "Wie schlafen Fische? (2026-09-25)")

    def test_an_episode_stored_before_a_crash_is_not_encoded_again(self, cloud, tmp_path):
        # The taf was written, then the app went away before the archive was
        # saved: the target noted before the encode is taken as the episode's.
        target = library_path("Kakadu", "Wie schlafen Fische?")
        cloud.dirs.add("/Kakadu")
        cloud.files[target] = b"our taf"
        entry = archive(storing={"ep-2": target})

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        assert entry.archived["ep-2"].path == target
        assert entry.storing == {}
        assert [target for target, _sources in cloud.encoded] == [
            library_path("Kakadu", "Grüße aus dem Zoo")]

    def test_the_target_is_noted_before_the_encode(self, cloud, tmp_path):
        entry = add_archive(tmp_path, "https://example.org/feed.xml", "Kakadu")
        noted = []
        real_encode = cloud.encode

        def encode(target, sources, special="library"):
            noted.append(load_archives(tmp_path)[0].storing)
            real_encode(target, sources, special)
        cloud.encode = encode

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(), most=1)

        assert noted == [{"ep-1": library_path("Kakadu", "Grüße aus dem Zoo")}]

    def test_a_failed_encode_keeps_the_episodes_stored_before_it(self, cloud, tmp_path):
        entry = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")
        web = FakeWeb()
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=web, most=1)
        cloud.encode_fails = "ffmpeg died"

        with pytest.raises(TeddyCloudError, match="ffmpeg died"):
            refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=web)

        assert len(load_archives(tmp_path)[0].archived) == 1

    def test_leaves_no_downloads_behind(self, cloud, tmp_path):
        refresh_archive(cloud, archive(), tmp_path, now=NOW, fetch=FakeWeb())

        assert not any(path.is_file() for path in (tmp_path / "podcasts").rglob("*"))

    def test_says_how_many_episodes_the_podcast_has(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb(), most=1)

        assert entry.total == 3
        assert entry.checked == NOW.isoformat()


class TestWhatIsKept:
    def test_title_date_and_length_of_each_episode(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        stored = entry.archived["103"]
        assert (stored.title, stored.published, stored.seconds) == (
            "Märchen: Rumpelstilzchen", "2026-10-25T19:04:00+01:00", 390)

    def test_the_picture_of_each_episode_is_kept_here(self, cloud, tmp_path):
        entry = archive(SHOW_LINK)

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        picture = podcast_archive.picture_of(tmp_path, entry.archived["103"])
        assert picture.startswith(b"\xff\xd8\xff")

    def test_a_picture_several_episodes_share_is_kept_once(self, cloud, tmp_path):
        web = FakeWeb()
        refresh_archive(cloud, archive(SHOW_LINK), tmp_path, now=NOW, fetch=web)

        assert len(list((tmp_path / podcast_archive.PICTURES).iterdir())) == 1
        assert len([url for url in web.asked if url.startswith("https://img.example/")]) == 1

    def test_a_feed_episode_has_its_own_picture(self, cloud, tmp_path):
        entry = archive()

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        assert podcast_archive.picture_of(tmp_path, entry.archived["ep-2"])
        assert podcast_archive.picture_of(tmp_path, entry.archived["ep-1"]) is None

    def test_an_episode_is_stored_without_a_picture_that_cannot_be_had(self, cloud, tmp_path):
        web = FakeWeb()
        web.pictures_fail = True
        entry = archive(SHOW_LINK)

        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=web)

        assert len(entry.archived) == 3
        assert podcast_archive.picture_of(tmp_path, entry.archived["103"]) is None

    def test_the_episode_of_a_library_path_is_found(self, cloud, tmp_path):
        entry = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        found = podcast_archive.episode_at(tmp_path, entry.archived["102"].path)

        assert found.title == "Jan und Henry: Der Affe"
        assert podcast_archive.episode_at(tmp_path, "/Andere/Folge.taf") is None


class TestStore:
    def test_archives_survive_a_restart(self, tmp_path):
        added = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")

        (loaded,) = load_archives(tmp_path)

        assert (loaded.id, loaded.source, loaded.folder) == (added.id, SHOW_LINK,
                                                             "Unser Sandmännchen")

    def test_removing_an_archive_keeps_its_tafs(self, cloud, tmp_path):
        entry = add_archive(tmp_path, "https://example.org/feed.xml", "Kakadu")
        refresh_archive(cloud, entry, tmp_path, now=NOW, fetch=FakeWeb())

        remove_archive(tmp_path, entry.id)

        assert load_archives(tmp_path) == []
        assert all(path in cloud.files for path in paths(entry).values())

    def test_a_refresh_finishing_after_a_removal_does_not_bring_it_back(self, tmp_path):
        entry = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")
        remove_archive(tmp_path, entry.id)

        entry.storing = {"103": "/Unser Sandmännchen/x.taf"}
        update_archive(tmp_path, entry)

        assert load_archives(tmp_path) == []


class TestTheUpdater:
    def _updater(self, tmp_path, cloud, web):
        submitted = []

        class Jobs:
            def submit(self, kind, label, work):
                submitted.append((kind, label))
                work(lambda *args: None)
                return f"job-{len(submitted)}"

        return ArchiveUpdater(lambda: cloud, tmp_path, Jobs(), web), submitted

    def test_queues_the_next_batch_until_everything_is_stored(self, cloud, tmp_path, monkeypatch):
        monkeypatch.setattr(podcast_archive, "BATCH", 2)
        entry = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")
        updater, submitted = self._updater(tmp_path, cloud, FakeWeb())

        updater.queue(entry.id)

        assert len(submitted) == 2
        assert len(load_archives(tmp_path)[0].archived) == 3

    def test_a_failure_is_kept_on_the_archive(self, cloud, tmp_path):
        entry = add_archive(tmp_path, SHOW_LINK, "Unser Sandmännchen")
        cloud.encode_fails = "disk full"
        updater, _submitted = self._updater(tmp_path, cloud, FakeWeb())

        with pytest.raises(TeddyCloudError):
            updater._refresh(entry.id)

        assert load_archives(tmp_path)[0].error == "disk full"
