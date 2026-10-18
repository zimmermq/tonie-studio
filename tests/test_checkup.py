"""Tests for the checkup: finding what is broken before someone at the box does.

Every check here answers a failure that has already happened once on the real
instance, so the fixtures reproduce those states rather than invented ones.
"""

import pytest
from fake_teddycloud import FakeTeddyCloud

from tonie_studio.checkup import examine, repaired
from tonie_studio.teddycloud import TeddyCloudError

TIERE = "a9c9ed0e500304e0"
BOBO = "a5b0ed0e500304e0"


@pytest.fixture
def cloud():
    cloud = FakeTeddyCloud()
    cloud.create_dir("/Lichterkinder")
    cloud.files["/Lichterkinder/Tierische Tierlieder.taf"] = b"taf"
    cloud.taf_headers["/Lichterkinder/Tierische Tierlieder.taf"] = {"valid": True}
    cloud.custom_models.append({"model": "TS-0001", "series": "Lichterkinder",
                                "episodes": "Tierische Tierlieder", "tracks": ["Papagei"],
                                "pic": "/custom_img/TS-0001.jpg"})
    cloud.add_tag(TIERE, source="lib://Lichterkinder/Tierische Tierlieder.taf",
                  tonie_model="TS-0001", nocloud=True)
    return cloud


def kinds(findings):
    return sorted(finding.kind for finding in findings)


class TestRepaired:
    def test_undoes_utf8_read_as_latin1(self):
        assert repaired("Bobo SiebenschlÃ¤fer") == "Bobo Siebenschläfer"

    def test_leaves_a_correct_umlaut_alone(self):
        assert repaired("Bobo Siebenschläfer") is None
        assert repaired("Grüße") is None

    def test_leaves_a_decomposed_umlaut_alone(self):
        # The archive's file names are NFD: a plain letter and a combining
        # diaeresis. That is correct text, not damage.
        assert repaired("Bärenlied") is None

    def test_undoes_damage_done_twice(self):
        once = "ä".encode("utf-8").decode("latin-1")
        twice = once.encode("utf-8").decode("latin-1")

        assert repaired(f"Schl{twice}fer") == "Schläfer"

    def test_plain_ascii_is_never_damage(self):
        assert repaired("Papagei") is None
        assert repaired("") is None


class TestHealthy:
    def test_a_healthy_setup_has_nothing_to_report(self, cloud):
        assert examine(cloud) == []


class TestMissingTaf:
    def test_a_tag_pointing_at_nothing_is_an_error(self, cloud):
        # 29.09.2026: two tags were left pointing at a path that did not exist,
        # and the box played nothing without a word.
        cloud.add_tag(BOBO, source="lib://Markus_Osterwald/Weg.taf", nocloud=True)

        found = [f for f in examine(cloud) if f.kind == "missing_taf"]

        assert len(found) == 1
        assert found[0].severity == "error"
        assert found[0].target == BOBO

    def test_offers_to_empty_a_sticker_whose_taf_is_gone(self, cloud):
        cloud.add_tag(BOBO, source="lib://Markus_Osterwald/Weg.taf", nocloud=True)

        found = next(f for f in examine(cloud) if f.kind == "missing_taf")

        assert found.fix == "empty_sticker"

    def test_names_the_model_a_rebuild_would_start_from(self, cloud):
        cloud.add_tag(BOBO, source="lib://Markus_Osterwald/Weg.taf", tonie_model="TS-0002",
                      nocloud=True)

        found = next(f for f in examine(cloud) if f.kind == "missing_taf")

        assert found.params["model"] == "TS-0002"

    def test_audio_an_encode_is_making_is_not_missing(self, cloud):
        # A rebuild under way writes exactly the path the sticker expects;
        # offering to empty that sticker meanwhile would undo the rebuild.
        cloud.add_tag(BOBO, source="lib://Markus_Osterwald/Weg.taf", nocloud=True)

        found = examine(cloud, busy={"/Markus_Osterwald/Weg.taf"})

        assert "missing_taf" not in kinds(found)

    def test_offers_the_repair_when_the_path_is_only_garbled(self, cloud):
        # The exact shape of that incident: the taf is there, the path naming
        # it had its umlaut mangled.
        cloud.create_dir("/Markus_Osterwald")
        cloud.files["/Markus_Osterwald/Bobo_Siebenschläfer.taf"] = b"taf"
        garbled = "lib://Markus_Osterwald/Bobo_SiebenschlÃ¤fer.taf"
        cloud.add_tag(BOBO, source=garbled, nocloud=True)

        found = next(f for f in examine(cloud) if f.target == BOBO)

        assert found.fix == "repair_source"
        assert "Bobo_Siebenschläfer.taf" in found.preview

    def test_a_library_that_could_not_be_read_claims_nothing_is_missing(self, cloud):
        # A folder that failed to list would otherwise make every tag in it
        # look broken, and deleting things is the obvious next step.
        def unreadable(path="/", special=None):
            raise TeddyCloudError("gone")

        cloud.list_dir = unreadable

        found = examine(cloud)

        assert "missing_taf" not in kinds(found)
        assert "orphan_taf" not in kinds(found)
        assert "library_unreadable" in kinds(found)


class TestCloudFallback:
    def test_a_library_tag_that_may_ask_the_cloud_is_a_warning(self, cloud):
        cloud.tag_entries[TIERE]["nocloud"] = False

        found = next(f for f in examine(cloud) if f.kind == "cloud_fallback")

        assert found.severity == "warning"
        assert found.fix == "nocloud"

    def test_a_stream_is_left_to_teddycloud(self, cloud):
        # teddyCloud sets the flags of a stream itself.
        cloud.add_tag(BOBO, source="https://radio.example/stream", nocloud=False, live=True)

        assert "cloud_fallback" not in kinds(examine(cloud))


class TestBoxSounds:
    def test_the_box_s_own_sounds_are_no_leftovers(self, cloud):
        # teddyCloud caches the box's startup jingle and the like from the
        # cloud; no sticker plays them, the box does.
        cloud.create_dir("/by")
        cloud.create_dir("/by/audioID")
        cloud.files["/by/audioID/1580745093.taf"] = b"x" * 512
        cloud.taf_headers["/by/audioID/1580745093.taf"] = {"valid": True}
        cloud.taf_info["/by/audioID/1580745093.taf"] = {"model": "box-en-us-02-00000000",
                                                         "series": "System sounds en-us"}

        assert "orphan_taf" not in kinds(examine(cloud))


class TestGarbledNames:
    def test_a_garbled_model_name_is_an_error_with_its_repair(self, cloud):
        cloud.custom_models[0]["series"] = "Bobo SiebenschlÃ¤fer"

        found = next(f for f in examine(cloud) if f.kind == "garbled_model")

        assert found.fix == "repair_model"
        assert "Bobo Siebenschläfer" in found.preview

    def test_finds_a_garbled_track_name_too(self, cloud):
        cloud.custom_models[0]["tracks"] = ["FÃ¼nf kleine Fische"]

        assert "garbled_model" in kinds(examine(cloud))


class TestLeftovers:
    def test_a_taf_no_tag_plays_is_reported_with_its_size(self, cloud):
        cloud.files["/Lichterkinder/Alt.taf"] = b"x" * 2048
        cloud.taf_headers["/Lichterkinder/Alt.taf"] = {"valid": True}

        found = next(f for f in examine(cloud) if f.kind == "orphan_taf")

        assert found.severity == "note"
        assert found.target == "/Lichterkinder/Alt.taf"
        assert found.fix == "delete_taf"

    def test_a_model_no_tag_names_is_reported(self, cloud):
        cloud.custom_models.append({"model": "TS-0009", "series": "Vergessen"})

        found = next(f for f in examine(cloud) if f.kind == "orphan_model")

        assert found.target == "TS-0009"
        assert found.fix == "delete_model"

    def test_only_this_apps_models_count_as_leftovers(self, cloud):
        # A model made by hand in teddyCloud is somebody's decision.
        cloud.custom_models.append({"model": "MEIN-1", "series": "Von Hand"})

        assert "orphan_model" not in kinds(examine(cloud))

    def test_a_tag_that_plays_but_has_no_name_is_mentioned(self, cloud):
        cloud.add_tag(BOBO, source="lib://Lichterkinder/Tierische Tierlieder.taf",
                      nocloud=True)

        found = next(f for f in examine(cloud) if f.kind == "nameless")

        assert found.fix is None


class TestOrder:
    def test_errors_come_before_warnings_before_notes(self, cloud):
        cloud.files["/Lichterkinder/Alt.taf"] = b"x"
        cloud.tag_entries[TIERE]["nocloud"] = False
        cloud.add_tag(BOBO, source="lib://Weg.taf", nocloud=True)

        severities = [finding.severity for finding in examine(cloud)]

        assert severities == sorted(severities, key=["error", "warning", "note"].index)


class TestWorkInProgress:
    def test_a_taf_waiting_to_be_given_to_a_tonie_is_not_offered_for_deletion(self, cloud):
        # Between an encode finishing and someone choosing the sticker, the taf
        # is played by nothing. Offering to delete it then throws away the work.
        cloud.files["/Lichterkinder/Neu.taf"] = b"x"

        found = examine(cloud, busy={"/Lichterkinder/Neu.taf"})

        assert "orphan_taf" not in kinds(found)

    def test_the_copy_a_replace_encodes_beside_its_target_is_not_offered(self, cloud):
        # Replacing a taf encodes the new audio beside it first, for minutes.
        cloud.files["/Lichterkinder/Tierische Tierlieder.new-0a1b2c3d.taf"] = b"x"

        found = examine(cloud, busy={"/Lichterkinder/Tierische Tierlieder.taf"})

        assert "orphan_taf" not in kinds(found)

    def test_a_copy_a_failed_replace_left_is_offered(self, cloud):
        cloud.files["/Lichterkinder/Tierische Tierlieder.new-0a1b2c3d.taf"] = b"x"

        assert "orphan_taf" in kinds(examine(cloud))

    def test_nothing_under_a_busy_folder_is_offered(self, cloud):
        # A podcast refresh builds its new taf before the sticker is switched.
        cloud.create_dir("/Podcasts")
        cloud.files["/Podcasts/Kakadu 2026-10-03 040000.taf"] = b"x"

        found = examine(cloud, busy_folders={"/Podcasts"})

        assert "orphan_taf" not in kinds(found)


class TestApplyingRepairs:
    """What each offered repair writes, and nothing besides."""

    def test_a_garbled_path_is_pointed_at_the_real_taf(self, cloud, tmp_path):
        from tonie_studio.checkup import apply_fix

        cloud.create_dir("/Markus_Osterwald")
        cloud.files["/Markus_Osterwald/Bobo_Siebenschl\u00e4fer.taf"] = b"taf"
        cloud.add_tag(BOBO, source="lib://Markus_Osterwald/Bobo_Siebenschl\u00c3\u00a4fer.taf",
                      nocloud=True)

        apply_fix(cloud, tmp_path, "repair_source", BOBO)

        assert cloud.tag_entries[BOBO]["source"] == "lib://Markus_Osterwald/Bobo_Siebenschl\u00e4fer.taf"

    def test_a_path_that_is_not_garbled_is_not_touched(self, cloud, tmp_path):
        from tonie_studio.checkup import apply_fix
        from tonie_studio.tags import TonieError

        with pytest.raises(TonieError) as caught:
            apply_fix(cloud, tmp_path, "repair_source", TIERE)

        assert caught.value.key == "checkup.nothing_to_repair"
        assert cloud.tag_entries[TIERE]["source"] == "lib://Lichterkinder/Tierische Tierlieder.taf"

    def test_deleting_a_leftover_model_takes_its_notes_along(self, cloud, tmp_path):
        from tonie_studio.checkup import apply_fix
        from tonie_studio.runtimes import playing_times, remember

        cloud.custom_models.append({"model": "TS-0009", "series": "Vergessen"})
        remember(tmp_path, "TS-0009", 300)

        apply_fix(cloud, tmp_path, "delete_model", "TS-0009")

        assert [m["model"] for m in cloud.custom_models] == ["TS-0001"]
        assert playing_times(tmp_path) == {}

    def test_repairing_a_model_that_is_gone_says_so(self, cloud, tmp_path):
        from tonie_studio.checkup import apply_fix
        from tonie_studio.tags import TonieError

        with pytest.raises(TonieError) as caught:
            apply_fix(cloud, tmp_path, "repair_model", "TS-0042")

        assert caught.value.key == "tonie.model_missing"

    def test_an_unknown_repair_is_a_bug_not_a_refusal(self, cloud, tmp_path):
        from tonie_studio.checkup import apply_fix

        with pytest.raises(ValueError):
            apply_fix(cloud, tmp_path, "format_disk", TIERE)

    def test_a_taf_given_to_a_sticker_meanwhile_is_not_deleted(self, cloud, tmp_path):
        # The checks ran a moment ago; a copy onto grandma's sticker since
        # must not lose its audio to the cleanup.
        from tonie_studio.checkup import apply_fix
        from tonie_studio.tags import TonieError

        cloud.files["/Lichterkinder/Alt.taf"] = b"x"
        cloud.add_tag(BOBO, source="lib://Lichterkinder/Alt.taf")

        with pytest.raises(TonieError) as caught:
            apply_fix(cloud, tmp_path, "delete_taf", "/Lichterkinder/Alt.taf")

        assert caught.value.key == "checkup.now_used"
        assert "/Lichterkinder/Alt.taf" in cloud.files

    def test_deleting_and_checking_happen_under_the_writers_lock(self, cloud, tmp_path):
        from web_helpers import waits_for_writing

        from tonie_studio.checkup import apply_fix

        cloud.files["/Lichterkinder/Alt.taf"] = b"x"

        assert waits_for_writing(
            lambda: apply_fix(cloud, tmp_path, "delete_taf", "/Lichterkinder/Alt.taf"))

