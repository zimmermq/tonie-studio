"""Tests for the overview sheet: every tonie on one page, for the tin."""

import io
import re

from PIL import Image

from tonie_studio.overview import COLUMNS, ROWS_PER_PAGE, Entry, build_overview


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

        per_page = COLUMNS * ROWS_PER_PAGE
        build_overview([entry(f"Album {n}") for n in range(per_page + 1)], target)

        assert _pages(target.read_bytes()) == 2

    def test_thirty_six_fit_on_a_page(self):
        # A collection of several hundred bought tonies stays a list of some
        # twenty pages rather than forty.
        assert COLUMNS * ROWS_PER_PAGE == 36

    def test_a_picture_is_fetched_only_when_its_entry_is_drawn(self, tmp_path):
        fetched = []

        def fetcher(name):
            def fetch():
                fetched.append(name)
                return _image()
            return fetch

        build_overview([Entry(image=fetcher("a"), title="A"), Entry(image=fetcher("b"), title="B"),
                        Entry(image=lambda: None, title="Without")], tmp_path / "overview.pdf")

        assert fetched == ["a", "b"]

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

    def test_composes_umlauts_written_as_letter_and_mark(self, tmp_path):
        # Album names from macOS folders spell "ue" as u followed by a combining
        # diaeresis (U+0308), which the PDF font prints as the letter and a box.
        from unittest.mock import patch

        from reportlab.pdfgen.canvas import Canvas

        drawn = []
        original = Canvas.drawString

        def capture(self, x, y, text, *args, **kwargs):
            drawn.append(text)
            return original(self, x, y, text, *args, **kwargs)

        with patch.object(Canvas, "drawString", capture):
            build_overview([Entry(image=None, title="Ku\u0308rbis", series="Gru\u0308\u00dfe",
                                  facts=["Su\u0308\u00dfes"])], tmp_path / "overview.pdf")

        text = " ".join(drawn)
        assert "\u0308" not in text
        assert "K\u00fcrbis" in text and "Gr\u00fc\u00dfe" in text and "S\u00fc\u00dfes" in text

    def test_nothing_to_list_is_refused(self, tmp_path):
        import pytest

        with pytest.raises(ValueError):
            build_overview([], tmp_path / "overview.pdf")
