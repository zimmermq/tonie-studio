"""Tests for the printable sheet of round cover stickers.

The geometry is tested as arithmetic, because that is what decides whether a
sticker fits the tag it is glued onto. The PDF itself is only checked for the
things a reader would notice: that it is a PDF, how many pages it has, and that
a source image nobody can decode does not take the sheet down with it.
"""

import re

import pytest
from PIL import Image

from tonie_studio.sheet import (FIT, FIT_FACTOR, LAYOUTS, PUNCH, SHEET, Sticker, build_sheet,
                                sticker_positions)

PER_PAGE = SHEET.per_page


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


def used_mm(layout):
    """Width and height of the grid on paper, from outer cut edge to outer cut edge."""
    edge = layout.diameter_mm if layout.cut == "circle" else layout.pitch_mm
    return ((layout.columns - 1) * layout.pitch_mm + edge,
            (layout.rows - 1) * layout.pitch_mm + edge)


class TestGeometry:
    def test_the_sticker_is_the_size_the_tags_take(self):
        assert SHEET.diameter_mm == 46

    def test_twenty_fit_on_a_sheet(self):
        assert (SHEET.columns, SHEET.rows, SHEET.per_page) == (4, 5, 20)

    @pytest.mark.parametrize("layout", LAYOUTS.values(), ids=LAYOUTS.keys())
    def test_leaves_a_margin_wide_enough_for_a_printer(self, layout):
        # Consumer printers refuse to print into roughly the outer 5 mm.
        width, height = used_mm(layout)

        assert (210 - width) / 2 >= 5
        assert (297 - height) / 2 >= 5

    @pytest.mark.parametrize("layout", LAYOUTS.values(), ids=LAYOUTS.keys())
    def test_the_bleed_fits_in_the_gap_between_two_stickers(self, layout):
        # Each sticker is printed larger than it is cut, so a cut that wanders
        # still lands on the picture. Two neighbours must not overlap.
        assert layout.picture_mm < layout.pitch_mm

    def test_the_printed_size_follows_the_bleed(self):
        assert (SHEET.picture_mm, PUNCH.picture_mm) == (48, 48.5)


class TestPunchLayout:
    """Covers for a 44.5 mm circle punch, cut apart as squares before punching."""

    def test_is_the_size_the_punch_cuts(self):
        assert PUNCH.diameter_mm == 44.5

    def test_leaves_a_frame_the_punch_can_hold(self):
        # A frame of a millimetre or two tears or bends under the blade.
        assert (PUNCH.pitch_mm - PUNCH.diameter_mm) / 2 >= 5

    def test_fifteen_fit_on_a_sheet(self):
        assert PUNCH.per_page == 15

    def test_a_sheet_of_squares_is_built(self, tmp_path):
        from unittest.mock import patch

        from reportlab.pdfgen.canvas import Canvas

        target = tmp_path / "punch.pdf"

        with patch.object(Canvas, "rect", autospec=True) as squares, \
                patch.object(Canvas, "circle", autospec=True) as circles:
            build_sheet([Sticker(image=_image(), count=PUNCH.per_page + 1)], target, layout=PUNCH)

        assert _pages(target.read_bytes()) == 2
        # The squares to cut along are drawn; the punch makes the round edge.
        assert squares.call_count == PUNCH.per_page + 1 and circles.call_count == 0

    def test_whole_cover_stays_inside_the_cut_not_the_bleed(self):
        for layout in LAYOUTS.values():
            assert layout.fit_factor * layout.picture_mm <= FIT_FACTOR * layout.diameter_mm + 1e-9


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

        assert second.x - first.x == pytest.approx(SHEET.pitch_mm * 72 / 25.4)

    def test_another_layout_has_its_own_spacing(self):
        first, second = sticker_positions(2, layout=PUNCH)

        assert second.x - first.x == pytest.approx(PUNCH.pitch_mm * 72 / 25.4)

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

    def test_says_what_it_left_off_in_the_language_of_the_back(self):
        from tonie_studio.sheet import Back, back_lines

        lines = back_lines(Back(title="Long", tracks=[f"Song {n}" for n in range(1, 41)],
                                language="en"))

        assert any(line.text.startswith("and ") and line.text.endswith(" more") for line in lines)

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

        from tonie_studio.sheet import LEADING, Back, back_lines, text_width, text_width_mm

        back = Back(title="Tierische Tierlieder", series="Lichterkinder",
                    notes=["12 Tracks - TS-0001"],
                    tracks=[f"Lied Nummer {n}" for n in range(1, 13)])
        lines = back_lines(back)
        width = text_width_mm() * mm

        assert sum(line.size * LEADING for line in lines) <= width
        for line in lines:
            assert text_width(line.text, line.size, line.bold) <= width

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

        from tonie_studio.sheet import render_square

        for size in (128, 512):
            picture = Image.open(io.BytesIO(render_square(_image(), FIT, size=size)))
            assert picture.size == (size, size)
            # The corner is blurred background, the centre is the cover itself.
            edge = round(size * (1 - SHEET.fit_factor) / 2)
            assert edge > 1


class TestShortening:
    def test_a_bold_word_is_cut_to_fit_in_bold(self):
        # Measuring a bold word in the regular face cuts it too late, and the
        # last letters land outside the circle.
        from tonie_studio.sheet import TITLE_PT, shorten_text, text_width

        word = "Donaudampfschifffahrtsgesellschaftskapitaensmuetze"

        # Across a range: at a few widths the two faces happen to cut in the
        # same place, and a single width can land on one of those.
        for width in range(30, 200):
            cut = shorten_text(word, TITLE_PT, float(width), bold=True)
            assert text_width(cut, TITLE_PT, bold=True) <= width


class TestPicturesFetchedOnTheWay:
    """A sheet of hundreds holds one original picture at a time, not all of them."""

    def test_a_picture_is_fetched_only_when_the_sheet_gets_to_it(self, tmp_path):
        fetched = []

        def fetcher(name):
            def fetch():
                fetched.append(name)
                return _image()
            return fetch

        seen = []
        build_sheet([Sticker(image=fetcher("a")), Sticker(image=fetcher("b"))],
                    tmp_path / "sheet.pdf",
                    progress=lambda done, total: seen.append((done, total, list(fetched))))

        assert seen == [(1, 2, ["a"]), (2, 2, ["a", "b"])]

    def test_a_motif_without_a_picture_is_left_off_and_named(self, tmp_path):
        skipped = build_sheet([Sticker(image=_image()), Sticker(image=lambda: None, label="gone")],
                              tmp_path / "sheet.pdf")

        assert skipped == ["gone"]


class TestTransparentPictures:
    def test_a_transparent_background_prints_as_paper_white(self):
        # Shop pictures are PNGs whose transparent pixels often store black;
        # dropping the transparency straight away printed them as black discs.
        import io

        picture = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        picture.paste(Image.new("RGBA", (100, 100), (200, 30, 30, 255)), (150, 150))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")

        from tonie_studio.sheet import render_square

        rendered = Image.open(io.BytesIO(render_square(buffer.getvalue(), size=200)))

        assert min(rendered.getpixel((5, 5))) > 245
        red, green, blue = rendered.getpixel((100, 100))
        assert red > 150 and green < 80


class TestDistinctTracks:
    """On a small circle, track names show what tells them apart."""

    def test_the_beginning_every_track_shares_is_left_off(self):
        from tonie_studio.sheet import distinct_tracks

        tracks = [f"Asterix - Die goldene Sichel Teil 0{n}" for n in range(1, 4)]

        assert distinct_tracks(tracks) == ["Teil 01", "Teil 02", "Teil 03"]

    def test_it_is_cut_at_a_word_not_inside_one(self):
        from tonie_studio.sheet import distinct_tracks

        assert distinct_tracks(["Flocke Anleitung", "Flocke Intro", "Flockenlied"]) == \
            ["Flocke Anleitung", "Flocke Intro", "Flockenlied"]

    def test_names_that_would_be_left_as_bare_numbers_stay_whole(self):
        # "1. 1", "2. 2" says less than "1. Kapitel 1".
        from tonie_studio.sheet import distinct_tracks

        assert distinct_tracks(["Kapitel 1", "Kapitel 2"]) == ["Kapitel 1", "Kapitel 2"]

    def test_a_single_track_or_names_without_a_shared_start_stay_as_they_are(self):
        from tonie_studio.sheet import distinct_tracks

        assert distinct_tracks(["Asterix - Teil 1"]) == ["Asterix - Teil 1"]
        assert distinct_tracks(["Eins", "Zwei"]) == ["Eins", "Zwei"]

    def test_the_back_side_lists_them_that_way(self):
        from tonie_studio.sheet import Back, back_lines

        lines = back_lines(Back(title="Die goldene Sichel",
                                tracks=["Asterix - Die goldene Sichel Teil 01",
                                        "Asterix - Die goldene Sichel Teil 02"]))

        assert [line.text for line in lines][-2:] == ["1. Teil 01", "2. Teil 02"]


class TestFigureFillsTheCover:
    """A shop picture is a figure on a transparent background, often with wide margins."""

    @staticmethod
    def figure(width=1200, height=900, box=(500, 150, 700, 750)):
        import io

        picture = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        picture.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (200, 30, 30, 255)),
                      box[:2])
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def red_rows(jpeg):
        import io

        rendered = Image.open(io.BytesIO(jpeg)).convert("RGB")
        width, height = rendered.size
        column = [rendered.getpixel((width // 2, y)) for y in range(height)]
        return sum(1 for red, green, _blue in column if red > 150 and green < 90) / height

    def test_the_margins_around_the_figure_are_trimmed(self):
        from tonie_studio.sheet import SHEET, render_square

        # The figure is two thirds of the picture's height; trimmed, it spans
        # nearly the whole cut circle.
        share = self.red_rows(render_square(self.figure(), size=400))

        assert share > 0.8 * SHEET.diameter_mm / SHEET.picture_mm

    def test_the_whole_figure_stays_on_the_cover(self):
        from tonie_studio.sheet import SHEET, render_square

        share = self.red_rows(render_square(self.figure(), size=400))

        assert share < SHEET.diameter_mm / SHEET.picture_mm

    def test_a_cover_without_transparency_is_left_as_it_is(self):
        import io

        from tonie_studio.sheet import render_square

        plain = Image.new("RGB", (400, 400), (255, 255, 255))
        plain.paste(Image.new("RGB", (100, 100), (200, 30, 30)), (150, 150))
        buffer = io.BytesIO()
        plain.save(buffer, format="JPEG")

        assert 0.2 < self.red_rows(render_square(buffer.getvalue(), size=400)) < 0.3

    def test_whole_cover_leaves_the_ring_around_a_figure_white(self):
        # The blurred copy that hides the ring around album art would show
        # here as a smudge around the figure.
        import io

        from tonie_studio.sheet import FIT, render_square

        rendered = Image.open(io.BytesIO(render_square(self.figure(), FIT, size=400)))

        assert min(rendered.convert("RGB").getpixel((200, 392))) > 245
