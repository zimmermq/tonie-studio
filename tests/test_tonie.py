"""Tests for putting a taf on a tag and giving it a cover."""

import pytest

from tonie_studio.encode import EncodeResult
from tonie_studio.models import (MODEL_PREFIX, find_model, find_model_by_audio, next_model_id,
                                 remove_model, update_model)
from tonie_studio.tags import TonieError, list_tags
from tonie_studio.tonie import copy_tag, move_tag, publish, publish_stream


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

    def test_a_tonie_teddycloud_keeps_beside_its_json_is_not_free(self, teddycloud):
        # A real tonie the box read and teddyCloud cached in its content tree
        # plays its own audio although it has no library source.
        teddycloud.tag_entries["deb4ed0e500304e0"].update(exists=True, tonie_model="11000286")

        tag = next(tag for tag in list_tags(teddycloud) if tag.ruid == "deb4ed0e500304e0")

        assert tag.assigned

    def test_a_tonie_named_by_a_model_is_not_free_either(self, teddycloud):
        teddycloud.tag_entries["deb4ed0e500304e0"].update(tonie_model="11000286")

        tag = next(tag for tag in list_tags(teddycloud) if tag.ruid == "deb4ed0e500304e0")

        assert tag.assigned

    def test_a_blank_sticker_is_free(self, teddycloud):
        assert not any(tag.assigned for tag in list_tags(teddycloud))

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
        assert (model.series, model.episode) == ("Neu", "Neue Folge")

    def test_keeps_the_audio_identity(self, teddycloud, encoded):
        # audio_id and hash are what lets teddyCloud recognise the taf.
        # Renaming a title must not cost that.
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="Alt", episode="Alt", tracks=[])

        update_model(teddycloud, model_id, series="Neu")

        model = find_model(teddycloud, model_id)
        assert model.audio_ids == [448533926]
        assert model.hashes == ["0573B07756E60D8431E19C2C1194572E5F428B37"]

    def test_replaces_the_cover(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        update_model(teddycloud, model_id, cover=b"neues-bild")

        assert teddycloud.images[f"{model_id}.jpg"] == b"neues-bild"
        assert find_model(teddycloud, model_id).picture == f"/custom_img/{model_id}.jpg"

    def test_leaves_untouched_what_was_not_named(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=["Eins", "Zwei"])

        update_model(teddycloud, model_id, series="Neu")

        assert find_model(teddycloud, model_id).tracks == ["Eins", "Zwei"]

    def test_refuses_a_model_that_is_not_there(self, teddycloud):
        with pytest.raises(TonieError):
            update_model(teddycloud, "TS-9999", series="x")

    def test_keeps_the_shop_product_it_was_linked_to(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        update_model(teddycloud, model_id, shop_link="10000123")
        update_model(teddycloud, model_id, series="Neu")

        assert find_model(teddycloud, model_id).shop_link == "10000123"

    def test_a_model_never_linked_has_no_shop_product(self, teddycloud, encoded):
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded,
                           series="L", episode="H", tracks=[])

        assert find_model(teddycloud, model_id).shop_link is None


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

        assert found.id == model_id

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
        assert (model.series, model.episode) == ("Kinderradio", "rund um die Uhr")
        assert "audio_id" not in model.to_json() and "hash" not in model.to_json()

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

        assert find_model(teddycloud, "TS-0001").year == 2021

    def test_reads_a_year_out_of_a_full_date(self, teddycloud, encoded):
        # An audio tag's date field is often a whole date, not just a year.
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"],
                year="2019-03-01")

        assert find_model(teddycloud, "TS-0001").year == 2019

    def test_leaves_the_field_out_when_the_year_makes_no_sense(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"],
                year="unbekannt")

        assert find_model(teddycloud, "TS-0001").release == ""

    def test_a_model_without_a_date_has_no_year(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["Eins"])

        assert find_model(teddycloud, "TS-0001").year is None


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
        assert model.picture == f"/custom_img/{model_id}.jpg"
        assert model.tracks == ["Zwei"]


class TestSameAudioOnTwoStickers:
    """teddyCloud 0.7.0 refuses a second custom model with the same audio_id and hash."""

    def test_the_second_sticker_gets_a_model_of_its_own_without_the_audio(self, teddycloud,
                                                                          encoded, cover):
        teddycloud.unique_audio = True
        first = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="Eins",
                        tracks=["a"], cover=cover)

        second = publish(teddycloud, "deb4ed0e500304e0", encoded, series="S", episode="Zwei",
                         tracks=["a"], cover=cover)

        models = {model["model"]: model for model in teddycloud.custom_tonies()}
        assert models[first]["audio_id"] == [encoded.audio_id]
        assert "audio_id" not in models[second] and models[second]["episodes"] == "Zwei"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["tonie_model"] == second

    def test_publishing_again_onto_the_same_model_keeps_its_audio(self, teddycloud, encoded,
                                                                  cover):
        teddycloud.unique_audio = True
        model_id = publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="Eins",
                           tracks=["a"], cover=cover)

        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="Eins neu",
                tracks=["a"], cover=cover, model_id=model_id)

        (model,) = teddycloud.custom_tonies()
        assert model["audio_id"] == [encoded.audio_id] and model["episodes"] == "Eins neu"


class TestPublishingAtOnce:
    def test_two_publishes_at_the_same_time_make_two_models(self, teddycloud, encoded, cover):
        teddycloud.unique_audio = True
        # Placing a sticker while a batch encode publishes is two threads; with
        # the id picked before a slow cover upload both took TS-0001.
        import threading
        import time

        original = teddycloud.upload

        def slow_upload(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)

        teddycloud.upload = slow_upload
        ids = []
        threads = [threading.Thread(target=lambda ruid=ruid: ids.append(
            publish(teddycloud, ruid, encoded, series="S", episode=ruid, tracks=["a"], cover=cover)))
            for ruid in ("7ab4ed0e500304e0", "deb4ed0e500304e0")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(set(ids)) == 2
        assert len(teddycloud.custom_tonies()) == 2


class TestOnlyAFreeSticker:
    """Asked for a free sticker, publish checks it is free where nothing else can write."""

    def test_a_sticker_taken_just_before_the_write_is_left_alone(self, teddycloud, encoded,
                                                                  monkeypatch):
        import tonie_studio.tonie as tonie

        original = tonie.require_tag

        def taken_meanwhile(client, ruid):
            tag = original(client, ruid)
            teddycloud.tag_entries[ruid]["source"] = "lib://Andere/Folge.taf"
            return tag

        monkeypatch.setattr(tonie, "require_tag", taken_meanwhile)

        with pytest.raises(TonieError) as caught:
            publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E",
                    tracks=["a"], require_free=True)

        assert caught.value.key == "tonie.sticker_taken"
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Andere/Folge.taf"
        assert teddycloud.custom_tonies() == []

    def test_without_asking_it_replaces_as_before(self, teddycloud, encoded):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"

        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["a"])

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == encoded.source


class TestRemoveTonie:
    """One step takes a tonie away: the sticker is freed, and whatever only it used goes too."""

    @pytest.fixture
    def made(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E", tracks=["a"])
        return teddycloud.tag_entries["7ab4ed0e500304e0"]["tonie_model"]

    def test_frees_the_sticker_and_deletes_model_and_taf(self, tmp_path, teddycloud, encoded, made):
        from tonie_studio.tonie import remove_tonie

        removal = remove_tonie(teddycloud, "7ab4ed0e500304e0", tmp_path)

        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        assert (entry["source"], entry["tonie_model"]) == ("", "")
        assert teddycloud.custom_tonies() == []
        assert encoded.target not in teddycloud.files
        assert (removal.model, removal.taf) == (made, encoded.target)

    def test_keeps_what_another_sticker_still_uses(self, tmp_path, teddycloud, encoded, made):
        from tonie_studio.tonie import remove_tonie

        copy_tag(teddycloud, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        removal = remove_tonie(teddycloud, "deb4ed0e500304e0", tmp_path)

        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == ""
        assert [model["model"] for model in teddycloud.custom_tonies()] == [made]
        assert encoded.target in teddycloud.files
        assert (removal.model, removal.taf) == (None, None)

    def test_an_official_model_is_left_alone(self, tmp_path, teddycloud, encoded):
        from tonie_studio.tonie import remove_tonie

        teddycloud.tag_entries["7ab4ed0e500304e0"].update(source=encoded.source,
                                                         tonie_model="10000001")

        removal = remove_tonie(teddycloud, "7ab4ed0e500304e0", tmp_path)

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["tonie_model"] == ""
        assert removal.model is None and removal.taf == encoded.target

    def test_a_sticker_whose_taf_is_already_gone_is_freed(self, tmp_path, teddycloud):
        from tonie_studio.tonie import remove_tonie

        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Weg/Weg.taf"

        removal = remove_tonie(teddycloud, "7ab4ed0e500304e0", tmp_path)

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == ""
        assert removal.taf is None

    def test_a_free_sticker_has_nothing_to_remove(self, tmp_path, teddycloud):
        from tonie_studio.tonie import remove_tonie

        with pytest.raises(TonieError) as caught:
            remove_tonie(teddycloud, "7ab4ed0e500304e0", tmp_path)

        assert caught.value.key == "tonie.nothing_to_remove"

    def test_what_was_noted_about_the_model_goes_too(self, tmp_path, teddycloud, made):
        from tonie_studio.albumnotes import made as albums_made
        from tonie_studio.albumnotes import remember_made
        from tonie_studio.runtimes import playing_times, remember
        from tonie_studio.tonie import remove_tonie

        remember(tmp_path, made, 600)
        remember_made(tmp_path, "album", made)

        remove_tonie(teddycloud, "7ab4ed0e500304e0", tmp_path)

        assert playing_times(tmp_path) == {} and albums_made(tmp_path) == {}


class TestTrackNamesKeepTheirChapters:
    def test_a_track_without_a_name_keeps_its_place(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="S", episode="E",
                tracks=["Eins", "", "Drei"])

        assert teddycloud.custom_tonies()[0]["tracks"] == ["Eins", "", "Drei"]


class TestMoveTag:
    """A tonie moved onto another sticker: the new one plays it, the old one is gone."""

    @pytest.fixture
    def published(self, teddycloud, encoded):
        publish(teddycloud, "7ab4ed0e500304e0", encoded, series="Lichterkinder",
                episode="Halloween", tracks=["Eins"])
        return teddycloud

    def test_the_new_sticker_plays_it_and_the_old_one_is_gone(self, published):
        played = published.tag_entries["7ab4ed0e500304e0"]["source"]

        move_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert published.tag_entries["deb4ed0e500304e0"]["source"] == played
        assert "7ab4ed0e500304e0" not in [tag.ruid for tag in list_tags(published)]

    def test_the_audio_and_the_model_stay(self, published):
        model = published.tag_entries["7ab4ed0e500304e0"]["tonie_model"]

        move_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert "/Lichterkinder/Halloween.taf" in published.files
        assert find_model(published, model) is not None

    def test_a_sticker_sharing_its_folder_stays(self, published):
        # teddyCloud files stickers under the first half of their id, so two
        # can share a folder; only the moved one's files go.
        published.add_tag("7ab4ed0e11111111", source="lib://x.taf")

        move_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert "7ab4ed0e11111111" in [tag.ruid for tag in list_tags(published)]

    def test_audio_cached_beside_the_old_entry_goes_too(self, published):
        # teddyCloud lists a sticker for any file of it, so a leftover would
        # bring it back.
        published.files["/7AB4ED0E/500304E0"] = b"cached audio"

        move_tag(published, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert "/7AB4ED0E/500304E0" not in published.files

    def test_nothing_is_removed_when_there_is_nothing_to_move(self, teddycloud):
        with pytest.raises(TonieError):
            move_tag(teddycloud, "7ab4ed0e500304e0", "deb4ed0e500304e0")

        assert "7ab4ed0e500304e0" in [tag.ruid for tag in list_tags(teddycloud)]
