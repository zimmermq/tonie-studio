"""Tests for the printable sheet of round cover stickers.

The geometry is tested as arithmetic, because that is what decides whether a
sticker fits the tag it is glued onto. The PDF itself is only checked for the
things a reader would notice: that it is a PDF, how many pages it has, and that
a source image nobody can decode does not take the sheet down with it.
"""

import re

import pytest
from PIL import Image

from tonie_studio.sheet import (
    BLEED_MM,
    FIT,
    FIT_FACTOR,
    COLUMNS,
    DIAMETER_MM,
    GAP_MM,
    PER_PAGE,
    ROWS,
    Sticker,
    build_sheet,
    sticker_positions,
)


def _image(width=800, height=800, colour=(200, 60, 40)) -> bytes:
    import io

    # Marked in the corner, not flat: a blur and a crop both leave a single
    # colour untouched, so a flat test image proves nothing about either.
    picture = Image.new("RGB", (width, height), colour)
    picture.paste(Image.new("RGB", (width // 6, height // 6), (20, 30, 220)), (0, 0))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG")
    return buffer.getvalue()


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


class TestGeometry:
    def test_the_sticker_is_the_size_the_tags_take(self):
        assert DIAMETER_MM == 46

    def test_twenty_fit_on_a_sheet(self):
        assert (COLUMNS, ROWS, PER_PAGE) == (4, 5, 20)

    def test_the_grid_stays_inside_an_a4_page(self):
        width_used = COLUMNS * DIAMETER_MM + (COLUMNS - 1) * GAP_MM
        height_used = ROWS * DIAMETER_MM + (ROWS - 1) * GAP_MM

        assert width_used <= 210
        assert height_used <= 297

    def test_leaves_a_margin_wide_enough_for_a_printer(self):
        # Consumer printers refuse to print into roughly the outer 5 mm.
        width_used = COLUMNS * DIAMETER_MM + (COLUMNS - 1) * GAP_MM
        height_used = ROWS * DIAMETER_MM + (ROWS - 1) * GAP_MM

        assert (210 - width_used) / 2 >= 5
        assert (297 - height_used) / 2 >= 5

    def test_the_bleed_fits_in_the_gap_between_two_stickers(self):
        # Each sticker is printed larger than it is cut, so a cut that wanders
        # still lands on the picture. Two neighbours must not overlap.
        assert 2 * BLEED_MM < GAP_MM


class TestStickerPositions:
    def test_places_as_many_as_asked_for(self):
        assert len(sticker_positions(7)) == 7

    def test_fills_a_page_before_starting_the_next(self):
        positions = sticker_positions(PER_PAGE + 1)

        assert [position.page for position in positions[:PER_PAGE]] == [0] * PER_PAGE
        assert positions[PER_PAGE].page == 1

    def test_starts_the_second_page_where_the_first_started(self):
        positions = sticker_positions(PER_PAGE + 1)

        assert (positions[PER_PAGE].x, positions[PER_PAGE].y) == (positions[0].x, positions[0].y)

    def test_runs_left_to_right_then_down(self):
        first, second, fifth = sticker_positions(5)[0], sticker_positions(5)[1], sticker_positions(5)[4]

        assert second.x > first.x
        # PDF coordinates grow upwards, so the next row sits lower.
        assert fifth.y < first.y
        assert fifth.x == pytest.approx(first.x)

    def test_spaces_them_by_the_diameter_plus_the_gap(self):
        first, second = sticker_positions(2)

        assert second.x - first.x == pytest.approx((DIAMETER_MM + GAP_MM) * 72 / 25.4)

    def test_asks_for_nothing_when_there_is_nothing(self):
        assert sticker_positions(0) == []


class TestBuildSheet:
    def test_writes_a_pdf(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image())], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_one_page_for_a_full_sheet(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image(), count=PER_PAGE)], target)

        assert _pages(target.read_bytes()) == 1

    def test_a_second_page_once_the_first_is_full(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image(), count=PER_PAGE + 1)], target)

        assert _pages(target.read_bytes()) == 2

    def test_counts_across_stickers_fill_the_page_together(self, tmp_path):
        # Two motifs asking for 18 and 3 copies are 21 stickers, not two
        # sheets of one motif each.
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image(), count=PER_PAGE - 2),
                     Sticker(image=_image(), count=3)], target)

        assert _pages(target.read_bytes()) == 2

    def test_takes_a_portrait_image_without_squashing_it(self, tmp_path):
        # A non-square cover is cropped to a centred square, never scaled to
        # fit, because a stretched face is obvious on a 46 mm circle.
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image(600, 1000))], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_skips_an_image_it_cannot_read_and_says_which(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        skipped = build_sheet([
            Sticker(image=b"not an image", label="kaputt"),
            Sticker(image=_image(), label="gut"),
        ], target)

        assert skipped == ["kaputt"]
        assert target.read_bytes().startswith(b"%PDF")

    def test_refuses_a_sheet_with_nothing_on_it(self, tmp_path):
        with pytest.raises(ValueError):
            build_sheet([], tmp_path / "sheet.pdf")

    def test_ignores_a_count_of_zero(self, tmp_path):
        with pytest.raises(ValueError):
            build_sheet([Sticker(image=_image(), count=0)], tmp_path / "sheet.pdf")


class TestFitMode:
    def test_keeps_the_whole_cover_inside_the_circle(self):
        # A square only fits inside a circle at 1/sqrt(2) of its diameter, so
        # anything larger would put the corners back outside the cut line.
        assert FIT_FACTOR <= 2 ** -0.5

    def test_produces_a_sheet_like_any_other(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image(), mode=FIT)], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_mixes_with_filled_stickers_on_one_sheet(self, tmp_path):
        target = tmp_path / "sheet.pdf"

        build_sheet([Sticker(image=_image()), Sticker(image=_image(), mode=FIT)], target)

        assert _pages(target.read_bytes()) == 1


class TestBackSide:
    def test_says_what_the_model_knows(self):
        from tonie_studio.sheet import Back, back_lines

        lines = back_lines(Back(title="Schlaflieder", series="Lichterkinder",
                                notes=["2 Tracks - TS-0001"], tracks=["Guter Mond", "Sternlein"]))
        text = [line.text for line in lines]

        assert "Schlaflieder" in text
        assert "Lichterkinder" in text
        assert "2 Tracks - TS-0001" in text
        assert "1. Guter Mond" in text

    def test_counts_the_tracks_it_had_to_leave_off(self):
        # Cutting the list mid-word reads as a rendering fault; saying how many
        # are missing reads as a decision.
        from tonie_studio.sheet import Back, back_lines

        lines = back_lines(Back(title="Lang", tracks=[f"Titel {n}" for n in range(1, 41)]))

        assert any(line.text.startswith("und ") and "weitere" in line.text for line in lines)

    def test_leaves_the_tracks_out_when_they_are_not_wanted(self):
        from tonie_studio.sheet import Back, back_lines

        lines = back_lines(Back(title="Kurz", tracks=["Eins", "Zwei"]), with_tracks=False)

        assert not any("Eins" in line.text for line in lines)

    def test_a_title_longer_than_the_circle_still_ends(self):
        # One unbroken word cannot be wrapped, so it has to be shortened.
        from tonie_studio.sheet import Back, back_lines, text_width_mm

        lines = back_lines(Back(title="Donaudampfschifffahrtsgesellschaftskapitaen"))

        assert lines and all(line.text for line in lines)
        assert lines[0].text.endswith("...")
        assert text_width_mm() > 0

    def test_nothing_sticks_out_of_the_circle(self):
        from reportlab.lib.units import mm

        from tonie_studio.sheet import (Back, LEADING, back_lines, text_width_mm,
                                        _width_of)

        back = Back(title="Tierische Tierlieder", series="Lichterkinder",
                    notes=["12 Tracks - TS-0001"],
                    tracks=[f"Lied Nummer {n}" for n in range(1, 13)])
        lines = back_lines(back)
        width = text_width_mm() * mm

        assert sum(line.size * LEADING for line in lines) <= width
        for line in lines:
            assert _width_of(line.text, line.size, line.bold) <= width

    def test_german_names_survive_the_pdf_font(self, tmp_path):
        # The built-in PDF fonts are not Unicode: an umlaut either lands in the
        # font's encoding or the sheet fails late, with the album named after
        # the one thing that cannot be printed.
        from tonie_studio.sheet import Back, Sticker, back_lines, build_sheet

        back = Back(title="Bobo Siebenschläfer", series="Grüße",
                    tracks=["Straße"])

        assert [line.text for line in back_lines(back)][:2] == \
               ["Bobo Siebenschläfer", "Grüße"]
        build_sheet([Sticker(image=_image(), back=back)], tmp_path / "u.pdf")
        assert (tmp_path / "u.pdf").read_bytes().startswith(b"%PDF")

    def test_prints_the_words_on_the_sheet(self, tmp_path):
        from tonie_studio.sheet import Back, Sticker, build_sheet

        target = tmp_path / "sheet.pdf"
        build_sheet([Sticker(image=_image(), back=Back(title="Schlaflieder"))], target)

        assert target.read_bytes().startswith(b"%PDF")

    def test_a_front_and_a_back_are_two_stickers(self, tmp_path):
        from tonie_studio.sheet import Back, Sticker, build_sheet

        target = tmp_path / "sheet.pdf"
        build_sheet([Sticker(image=_image(), count=2),
                     Sticker(image=_image(), count=2, back=Back(title="X"))], target)

        assert _pages(target.read_bytes()) == 1


class TestRenderSquare:
    """The screen and the printer share this, so a preview cannot drift."""

    def test_the_modes_do_not_produce_the_same_picture(self):
        from tonie_studio.sheet import FILL, render_square

        assert render_square(_image(), FILL) != render_square(_image(), FIT)

    def test_shrinks_the_cover_by_the_same_share_at_any_size(self):
        # The preview is rendered small and the sticker large; a blur or an
        # inset tied to the printed size would make them different pictures.
        import io

        from PIL import Image

        from tonie_studio.sheet import FIT_FACTOR, render_square

        for size in (128, 512):
            picture = Image.open(io.BytesIO(render_square(_image(), FIT, size=size)))
            assert picture.size == (size, size)
            # The corner is blurred background, the centre is the cover itself.
            edge = round(size * (1 - FIT_FACTOR) / 2)
            assert edge > 1
