"""Tests for putting a taf on a tag and giving it a cover."""

import pytest

from fake_teddycloud import FakeTeddyCloud
from tonie_studio.encode import EncodeResult
from tonie_studio.tonie import (
    MODEL_PREFIX,
    TonieError,
    copy_tag,
    find_model,
    find_model_by_audio,
    list_tags,
    next_model_id,
    publish,
    publish_stream,
    remove_model,
    update_model,
    year_of,
)


@pytest.fixture
def teddycloud():
    cloud = FakeTeddyCloud()
    cloud.add_tag("7ab4ed0e500304e0")
    cloud.add_tag("deb4ed0e500304e0")
    return cloud


@pytest.fixture
def encoded(teddycloud):
    """An encode that really left a taf behind, as one always does."""
    teddycloud.create_dir("/Lichterkinder")
    teddycloud.files["/Lichterkinder/Halloween.taf"] = b"taf"
    return EncodeResult(
        target="/Lichterkinder/Halloween.taf",
        source="lib://Lichterkinder/Halloween.taf",
        tracks=2,
        audio_id=448533926,
        sha1="0573b07756e60d8431e19c2c1194572e5f428b37",
    )


@pytest.fixture
def cover(tmp_path):
    path = tmp_path / "cover.jpg"
    path.write_bytes(b"\xff\xd8\xff-jpeg-bytes")
    return path


class TestListTags:
    def test_leaves_out_the_system_entry(self, teddycloud):
        assert all(tag.ruid != "0000000000000000" for tag in list_tags(teddycloud))

    def test_puts_the_empty_tags_first(self, teddycloud):
        teddycloud.set_tag("7ab4ed0e500304e0", source="lib://a.taf")

        assert [tag.assigned for tag in list_tags(teddycloud)] == [False, True]

    def test_reports_what_a_tag_already_plays(self, teddycloud):
        teddycloud.set_tag("deb4ed0e500304e0", source="lib://a.taf")

        tag = next(tag for tag in list_tags(teddycloud) if tag.ruid == "deb4ed0e500304e0")

        assert tag.assigned and tag.source == "lib://a.taf"


class TestNextModelId:
    def test_starts_at_one(self, teddycloud):
        assert next_model_id(teddycloud) == f"{MODEL_PREFIX}0001"

    def test_continues_past_what_exists(self, teddycloud):
        teddycloud.upsert_custom_tonie({"model": f"{MODEL_PREFIX}0007", "series": "x"})

        assert next_model_id(teddycloud) == f"{MODEL_PREFIX}0008"

    def test_ignores_models_that_are_not_ours(self, teddycloud):
        teddycloud.upsert_custom_tonie({"model": "01-0001", "series": "Janosch"})

        assert next_model_id(teddycloud) == f"{MODEL_PREFIX}0001"


class TestPublish:
    def test_points_the_tag_at_the_taf(self, teddycloud, encoded, cover):
        publish(teddycloud, "7ab4ed0e500304e0", encoded,
                series="Lichterkinder", episode="Halloween", tracks=["A", "B"], cover=cover)

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == encoded.source

    def test_keeps_the_box_from_asking_the_real_cloud(self, teddycloud, encoded):
        # Without nocloud the box falls back to the Tonies cloud for a tag it
        # believes it owns, and plays the original content instead.
        publish(teddycloud, "7ab4ed0e500304e0", encoded,
                series="Lichterkinder", episode="Halloween", tracks=[])

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["nocloud"] is True

    def test_writes_a_model_carrying_the_names_and_the_cover(self, teddycloud, encoded, cover):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="Lichterkinder",
                           episode="Halloween", tracks=["Kleine Monster", "Suesses"], cover=cover)

        model = teddycloud.custom_tonies()[0]
        assert model["model"] == model_id
        assert (model["series"], model["episodes"]) == ("Lichterkinder", "Halloween")
        assert model["tracks"] == ["Kleine Monster", "Suesses"]
        assert model["pic"] == f"/custom_img/{model_id}.jpg"

    def test_carries_the_audio_identity_so_teddycloud_links_them_itself(self, teddycloud, encoded):
        # With audio_id and hash present, teddyCloud attaches this model to any
        # tag playing that taf without being told again.
        publish(teddycloud, "7ab4ed0e500304e0", encoded,
                series="Lichterkinder", episode="Halloween", tracks=[])

        model = teddycloud.custom_tonies()[0]
        assert model["audio_id"] == [448533926]
        assert model["hash"] == ["0573B07756E60D8431E19C2C1194572E5F428B37"]

    def test_uploads_the_cover_under_the_model_name(self, teddycloud, encoded, cover):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L",
                           episode="H", tracks=[], cover=cover)

        assert teddycloud.images[f"{model_id}.jpg"] == b"\xff\xd8\xff-jpeg-bytes"

    def test_links_the_tag_to_the_model(self, teddycloud, encoded, cover):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L",
                           episode="H", tracks=[], cover=cover)

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["tonie_model"] == model_id

    def test_writes_the_model_before_the_tag_points_at_it(self, teddycloud, encoded):
        # A tag naming a model that does not exist yet shows up as unknown in
        # teddyCloud until something reloads it.
        order = []
        teddycloud.upsert_custom_tonie = _record(order, "model", teddycloud.upsert_custom_tonie)
        teddycloud.set_tag = _record(order, "tag", teddycloud.set_tag)

        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L", episode="H", tracks=[])

        assert order == ["model", "tag"]

    def test_manages_without_a_cover(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded,
                series="L", episode="H", tracks=[])

        assert "pic" not in teddycloud.custom_tonies()[0]
        assert teddycloud.images == {}

    def test_keeps_the_extension_the_cover_actually_has(self, teddycloud, encoded, tmp_path):
        png = tmp_path / "front.PNG"
        png.write_bytes(b"\x89PNG")

        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L",
                           episode="H", tracks=[], cover=png)

        assert f"{model_id}.png" in teddycloud.images

    def test_reuses_a_model_id_instead_of_piling_up_duplicates(self, teddycloud, encoded, cover):
        first = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L",
                        episode="H", tracks=["A"], cover=cover)
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L",
                episode="H neu", tracks=["B"], cover=cover, model_id=first)

        assert len(teddycloud.custom_tonies()) == 1
        assert teddycloud.custom_tonies()[0]["episodes"] == "H neu"

    def test_records_language_and_category(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L", episode="H",
                tracks=[], language="de-de", category="audio-book")

        model = teddycloud.custom_tonies()[0]
        assert (model["language"], model["category"]) == ("de-de", "audio-book")

    def test_refuses_a_tag_the_box_has_never_seen(self, teddycloud, encoded):
        with pytest.raises(TonieError) as excinfo:
            publish(teddycloud, "ffffffffffffffff", encoded,
                    series="L", episode="H", tracks=[])

        assert "ffffffffffffffff" in str(excinfo.value)

    def test_refuses_an_encode_without_a_source(self, teddycloud):
        empty = EncodeResult(target="/a.taf", source="", tracks=1)

        with pytest.raises(TonieError):
            publish(teddycloud, "7ab4ed0e500304e0", empty,
                    series="L", episode="H", tracks=[])


def _record(log, label, original):
    def wrapper(*args, **kwargs):
        log.append(label)
        return original(*args, **kwargs)
    return wrapper


class TestUpdateModel:
    def test_changes_the_description(self, teddycloud, encoded, cover):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="Alt",
                           episode="Alt", tracks=["A"], cover=cover)

        update_model(teddycloud, model_id, series="Neu", episode="Neue Folge")

        model = find_model(teddycloud, model_id)
        assert (model["series"], model["episodes"]) == ("Neu", "Neue Folge")

    def test_keeps_the_audio_identity(self, teddycloud, encoded):
        # audio_id and hash are what lets teddyCloud recognise the taf.
        # Renaming a title must not cost that.
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="Alt", episode="Alt", tracks=[])

        update_model(teddycloud, model_id, series="Neu")

        model = find_model(teddycloud, model_id)
        assert model["audio_id"] == [448533926]
        assert model["hash"] == ["0573B07756E60D8431E19C2C1194572E5F428B37"]

    def test_replaces_the_cover(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        update_model(teddycloud, model_id, cover=b"neues-bild")

        assert teddycloud.images[f"{model_id}.jpg"] == b"neues-bild"
        assert find_model(teddycloud, model_id)["pic"] == f"/custom_img/{model_id}.jpg"

    def test_leaves_untouched_what_was_not_named(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=["Eins", "Zwei"])

        update_model(teddycloud, model_id, series="Neu")

        assert find_model(teddycloud, model_id)["tracks"] == ["Eins", "Zwei"]

    def test_refuses_a_model_that_is_not_there(self, teddycloud):
        with pytest.raises(TonieError):
            update_model(teddycloud, "TS-9999", series="x")


class TestRemoveModel:
    def test_deletes_it_and_unlinks_the_tag(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        remove_model(teddycloud, model_id, ruid="7ab4ed0e500304e0")

        assert teddycloud.custom_tonies() == []
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["tonie_model"] == ""

    def test_leaves_the_audio_on_the_tag(self, teddycloud, encoded):
        # Only the cover and the names go; the tonie still plays.
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        remove_model(teddycloud, model_id, ruid="7ab4ed0e500304e0")

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == encoded.source


class TestFindModelByAudio:
    def test_finds_the_model_holding_the_same_recording(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        found = find_model_by_audio(teddycloud, encoded.audio_id, encoded.sha1)

        assert found["model"] == model_id

    def test_matches_whatever_case_the_hash_is_written_in(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L", episode="H", tracks=[])

        assert find_model_by_audio(teddycloud, encoded.audio_id, encoded.sha1.lower())

    def test_needs_both_halves_to_agree(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L", episode="H", tracks=[])

        assert find_model_by_audio(teddycloud, 999, encoded.sha1) is None
        assert find_model_by_audio(teddycloud, encoded.audio_id, "0" * 40) is None

    def test_says_nothing_when_the_encode_carried_no_identity(self, teddycloud):
        assert find_model_by_audio(teddycloud, None, None) is None


class TestPublishStream:
    def test_points_the_tag_at_the_url(self, teddycloud):
        publish_stream(teddycloud, "7ab4ed0e500304e0",
                       "http://radio.example/kinder.mp3", series="Kinderradio")

        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        assert entry["source"] == "http://radio.example/kinder.mp3"

    def test_marks_it_live_and_keeps_the_cloud_out(self, teddycloud):
        # teddyCloud sets both itself for a source containing "://", but a tag
        # that is right when written beats one that is right after being read.
        publish_stream(teddycloud, "7ab4ed0e500304e0",
                       "http://radio.example/kinder.mp3", series="Kinderradio")

        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        assert entry["live"] is True and entry["nocloud"] is True

    def test_writes_a_model_without_an_audio_identity(self, teddycloud):
        # There is no taf, so there is nothing to recognise automatically.
        model_id = publish_stream(teddycloud, "7ab4ed0e500304e0",
                                  "http://radio.example/kinder.mp3",
                                  series="Kinderradio", episode="rund um die Uhr")

        model = find_model(teddycloud, model_id)
        assert (model["series"], model["episodes"]) == ("Kinderradio", "rund um die Uhr")
        assert "audio_id" not in model and "hash" not in model

    def test_takes_a_cover(self, teddycloud):
        model_id = publish_stream(teddycloud, "7ab4ed0e500304e0",
                                  "https://radio.example/kinder", series="K",
                                  cover=b"bild")

        assert teddycloud.images[f"{model_id}.jpg"] == b"bild"

    def test_refuses_something_that_is_not_a_url(self, teddycloud):
        with pytest.raises(TonieError):
            publish_stream(teddycloud, "7ab4ed0e500304e0", "kinderradio", series="K")

    def test_refuses_a_scheme_the_box_cannot_fetch(self, teddycloud):
        with pytest.raises(TonieError):
            publish_stream(teddycloud, "7ab4ed0e500304e0",
                           "rtsp://radio.example/kinder", series="K")

    def test_refuses_a_tag_the_box_has_never_seen(self, teddycloud):
        with pytest.raises(TonieError):
            publish_stream(teddycloud, "ffffffffffffffff",
                           "http://radio.example/kinder", series="K")


class TestPublishChecksTheAudioIsThere:
    def test_refuses_a_tag_that_would_play_nothing(self, teddycloud):
        # teddyCloud stores whatever path it is handed and says nothing when
        # the file is not there. It happened once, from a source string read
        # with the wrong encoding and written straight back, and it left two
        # tonies silent.
        missing = EncodeResult(target="/Weg.taf", source="lib://Weg.taf", tracks=1)

        with pytest.raises(TonieError) as excinfo:
            publish(teddycloud, "7ab4ed0e500304e0", missing,
                    series="L", episode="H", tracks=[])

        assert "would play nothing" in str(excinfo.value)
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == ""

    def test_accepts_one_that_is_there(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="L", episode="H", tracks=[])

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == encoded.source


class TestReleaseYear:
    """The year goes into the catalogue's own `release` field, not a new one.

    Over six and a half thousand official models use exactly twelve fields, and
    `release` is the one that carries a date. Inventing a field would mean
    hoping teddyCloud keeps it.
    """

    def test_records_the_year_a_tag_reported(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="Lichterkinder", episode="Schlaflieder",
                tracks=["Eins"], year="2021")

        assert year_of(find_model(teddycloud, "TS-0001")) == 2021

    def test_reads_a_year_out_of_a_full_date(self, teddycloud, encoded):
        # An audio tag's date field is often a whole date, not just a year.
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"],
                year="2019-03-01")

        assert year_of(find_model(teddycloud, "TS-0001")) == 2019

    def test_leaves_the_field_out_when_the_year_makes_no_sense(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"],
                year="unbekannt")

        assert "release" not in find_model(teddycloud, "TS-0001")

    def test_a_model_without_a_date_has_no_year(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"])

        assert year_of(find_model(teddycloud, "TS-0001")) is None


class TestCopyTag:
    """A second sticker playing what the first one plays: for grandma's house,
    or to replace one that got lost."""

    @pytest.fixture
    def published(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="Lichterkinder",
                episode="Halloween", tracks=["Eins"])
        return teddycloud

    def test_plays_the_same_audio_under_the_same_model(self, published):
        copy_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        original = published.tag_entries["7ab4ed0e500304e0"]
        copy = published.tag_entries["deb4ed0e500304e0"]
        assert copy["source"] == original["source"]
        assert copy["tonie_model"] == original["tonie_model"]

    def test_makes_no_second_model(self, published):
        # Two models with the same audio identity would fight over which name
        # teddyCloud shows; one model on two stickers has no such problem.
        before = len(published.custom_models)

        copy_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert len(published.custom_models) == before

    def test_keeps_the_copy_off_the_cloud(self, published):
        copy_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert published.tag_entries["deb4ed0e500304e0"]["nocloud"] is True

    def test_refuses_to_copy_a_sticker_onto_itself(self, published):
        with pytest.raises(TonieError):
            copy_tag(published, "7ab4ed0e500304e0", "7ab4ed0e500304e0")

    def test_refuses_to_copy_from_a_sticker_that_plays_nothing(self, teddycloud):
        with pytest.raises(TonieError):
            copy_tag(teddycloud, "7ab4ed0e500304e0", "deb4ed0e500304e0")

    def test_refuses_a_sticker_the_box_has_never_read(self, published):
        with pytest.raises(TonieError):
            copy_tag(published, "7ab4ed0e500304e0", "ffffffffffffffff")


class TestRepublishing:
    def test_publishing_again_without_a_picture_keeps_the_old_one(self, teddycloud, encoded):
        # A model is replaced whole on every write; without carrying the
        # picture over, refreshing a tonie's audio would strip its cover.
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E",
                           tracks=["Eins"], cover=b"\xff\xd8\xffcover")

        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E",
                tracks=["Zwei"], model_id=model_id)

        model = find_model(teddycloud, model_id)
        assert model["pic"] == f"/custom_img/{model_id}.jpg"
        assert model["tracks"] == ["Zwei"]
