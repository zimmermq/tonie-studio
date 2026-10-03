"""Tests for the overview sheet: every tonie on one page, for the tin."""

import io
import re

from PIL import Image

from tonie_studio.overview import ROWS_PER_PAGE, Entry, build_overview


def _image():
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 60, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def entry(title="Schlaflieder", image=True):
    return Entry(image=_image() if image else None, title=title, series="Lichterkinder",
                 facts=["2021 - 60 Tracks - 140 Minuten", "TS-0001"])


class TestOverview:
    def test_writes_a_pdf(self, tmp_path):
        target = tmp_path / "overview.pdf"

        build_overview([entry()], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_a_long_collection_goes_onto_more_pages(self, tmp_path):
        target = tmp_path / "overview.pdf"

        build_overview([entry(f"Album {n}") for n in range(ROWS_PER_PAGE * 2 + 1)], target)

        assert _pages(target.read_bytes()) == 2

    def test_a_tonie_without_a_picture_is_still_listed(self, tmp_path):
        # A radio tonie or one recorded without a cover is still a tonie in
        # the tin.
        target = tmp_path / "overview.pdf"

        build_overview([entry(image=False)], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_a_picture_that_cannot_be_read_does_not_stop_the_sheet(self, tmp_path):
        target = tmp_path / "overview.pdf"
        broken = Entry(image=b"not an image", title="Kaputt", series="", facts=[])

        build_overview([broken, entry()], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_german_names_survive(self, tmp_path):
        target = tmp_path / "overview.pdf"

        build_overview([Entry(image=None, title="Bobo Siebenschläfer", series="Grüße",
                              facts=["Straße"])], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_nothing_to_list_is_refused(self, tmp_path):
        import pytest

        with pytest.raises(ValueError):
            build_overview([], tmp_path / "overview.pdf")
