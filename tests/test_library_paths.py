"""Tests for telling a library path from one that would lead somewhere else."""

import pytest

from tonie_studio.library import safe_library_path


class TestSafeLibraryPath:
    @pytest.mark.parametrize("path", ["/Lichterkinder/Halloween.taf", "/by/audioID/1.taf",
                                      "/Bobo Siebenschl\u00e4fer/Folge 1.taf"])
    def test_a_plain_path_is_one(self, path):
        assert safe_library_path(path)

    @pytest.mark.parametrize("path", ["", "Halloween.taf", "/../config.ini", "/a/../../b.taf",
                                      "/a/./b.taf", "//b.taf", "/a/\x00b.taf", "/a\\..\\b.taf",
                                      "/a/b\n.taf"])
    def test_one_that_climbs_or_hides_something_is_not(self, path):
        assert not safe_library_path(path)
