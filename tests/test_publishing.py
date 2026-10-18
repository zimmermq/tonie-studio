"""Tests for putting a finished encode on the sticker meant for it."""

import pytest

from tonie_studio.encode import EncodeResult
from tonie_studio.publishing import Details, Outcome, put_on_sticker
from tonie_studio.teddycloud import TeddyCloudError


@pytest.fixture
def outcome(teddycloud):
    teddycloud.create_dir("/Lichterkinder")
    teddycloud.files["/Lichterkinder/Halloween.taf"] = b"taf"
    encoded = EncodeResult(target="/Lichterkinder/Halloween.taf",
                           source="lib://Lichterkinder/Halloween.taf", tracks=2)
    return Outcome(encoded=encoded, details=Details(series="Lichterkinder", episode="Halloween"),
                   tracks=["Eins", "Zwei"])


class TestPutOnSticker:
    def test_a_free_sticker_gets_it(self, teddycloud, outcome, tmp_path):
        assert put_on_sticker(teddycloud, tmp_path, "7ab4ed0e500304e0", outcome)

        assert outcome.ruid == "7ab4ed0e500304e0" and outcome.model and not outcome.assign_error

    def test_a_sticker_given_something_else_meanwhile_is_left_alone(self, teddycloud, outcome,
                                                                     tmp_path):
        teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Anderes/Hoerspiel.taf"

        assert not put_on_sticker(teddycloud, tmp_path, "7ab4ed0e500304e0", outcome)

        assert outcome.assign_error == "batch.tag_taken"
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Anderes/Hoerspiel.taf"

    def test_teddycloud_failing_is_noted_on_the_outcome_not_raised(self, teddycloud, outcome,
                                                                    tmp_path):
        # The encode itself is fine; its page offers the ordinary assignment.
        def refuse(*args, **kwargs):
            raise TeddyCloudError("teddyCloud refused /api/toniesCustomJsonUpsert", refused=True)

        teddycloud.upsert_custom_tonie = refuse

        assert not put_on_sticker(teddycloud, tmp_path, "7ab4ed0e500304e0", outcome)

        assert outcome.assign_error == "batch.publish_failed"
