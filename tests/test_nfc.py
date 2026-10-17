"""Tests for reading Flipper Zero dumps of tonies."""

import pytest

from tonie_studio.nfc import DumpError, parse_dump

AUTH = "00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF 00 11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff"


def dump(uid="E0 04 03 50 17 C5 DF 11", data=AUTH, extra=""):
    lines = [
        "Filetype: Flipper NFC device",
        "Version: 4",
        "# UID is common for all formats",
        f"UID: {uid}" if uid is not None else "",
        "Block Count: 8",
        f"Data Content: {data}" if data is not None else "",
        "Security Status: 00 00 00 00 00 00 00 00",
        extra,
    ]
    return "\n".join(lines)


class TestParsing:
    def test_the_tonie_is_named_after_its_reversed_uid(self):
        tonie = parse_dump(dump())

        assert tonie.ruid == "11dfc517500304e0"
        assert (tonie.folder, tonie.file) == ("11DFC517", "500304E0")

    def test_the_auth_is_the_data_content_without_spaces(self):
        tonie = parse_dump(dump())

        assert tonie.auth == AUTH.replace(" ", "").upper()

    def test_reads_windows_line_ends_and_a_byte_order_mark(self):
        tonie = parse_dump("\ufeff" + dump().replace("\n", "\r\n"))

        assert tonie.ruid == "11dfc517500304e0"

    def test_a_comment_mentioning_uid_does_not_count(self):
        tonie = parse_dump(dump(extra="# UID: 00 00 00 00 00 00 00 00"))

        assert tonie.ruid == "11dfc517500304e0"


class TestRefusing:
    @pytest.mark.parametrize("uid, data", [(None, AUTH), ("E0 04 03 50 17 C5 DF 11", None)])
    def test_a_dump_without_uid_or_data_content(self, uid, data):
        with pytest.raises(DumpError) as raised:
            parse_dump(dump(uid=uid, data=data))

        assert raised.value.key == "nfc.missing_fields"

    @pytest.mark.parametrize("uid", ["E0 04 03 50 17 C5 DF", "E0 04 03 50 17 C5 DF 1G", "E00403 5017C5DF11"])
    def test_a_uid_that_is_not_eight_hex_bytes(self, uid):
        with pytest.raises(DumpError) as raised:
            parse_dump(dump(uid=uid))

        assert raised.value.key == "nfc.bad_uid"

    @pytest.mark.parametrize("data", [AUTH[:-3], AUTH[:-1] + "g"])
    def test_data_content_that_is_not_32_hex_bytes(self, data):
        with pytest.raises(DumpError) as raised:
            parse_dump(dump(data=data))

        assert raised.value.key == "nfc.bad_auth"
