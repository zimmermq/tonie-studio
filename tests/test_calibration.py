"""Tests for lining the printer up with the sticker sheet."""

import pytest
from reportlab.lib.units import mm

from tonie_studio.calibration import (LARGEST_SHIFT_MM, build_calibration, expected_first_centre,
                                      read_shift, save_measurement)
from tonie_studio.sheet import sticker_positions


class TestExpectedPosition:
    def test_the_first_centre_is_where_the_sheet_puts_it(self):
        # The numbers printed on the calibration sheet are what a person
        # measures against; they have to come from the same arithmetic.
        from reportlab.lib.pagesizes import A4

        first = sticker_positions(1)[0]
        left_mm, top_mm = expected_first_centre()

        assert first.x == pytest.approx(left_mm * mm)
        assert A4[1] - first.y == pytest.approx(top_mm * mm)


class TestShift:
    def test_nothing_measured_means_no_shift(self, tmp_path):
        assert read_shift(tmp_path) == (0.0, 0.0)

    def test_a_cross_printed_too_far_right_moves_everything_left(self, tmp_path):
        left, top = expected_first_centre()

        save_measurement(tmp_path, left + 1.5, top)

        assert read_shift(tmp_path) == pytest.approx((-1.5, 0.0))

    def test_a_cross_printed_too_low_moves_everything_up(self, tmp_path):
        left, top = expected_first_centre()

        save_measurement(tmp_path, left, top + 2.0)

        assert read_shift(tmp_path) == pytest.approx((0.0, -2.0))

    def test_a_wild_measurement_is_capped(self, tmp_path):
        # A misread ruler would push the stickers off the page; anything past
        # the cap is a mistake, not a printer.
        left, top = expected_first_centre()

        save_measurement(tmp_path, left + 40, top - 40)

        dx, dy = read_shift(tmp_path)
        assert abs(dx) == LARGEST_SHIFT_MM and abs(dy) == LARGEST_SHIFT_MM


class TestShiftedSheet:
    def test_the_shift_moves_every_sticker_by_exactly_that(self):
        plain = sticker_positions(3)
        shifted = sticker_positions(3, shift_mm=(-1.5, 2.0))

        for before, after in zip(plain, shifted):
            assert after.x - before.x == pytest.approx(-1.5 * mm)
            # Down on paper is down in PDF coordinates too, which grow upwards.
            assert after.y - before.y == pytest.approx(-2.0 * mm)


class TestCalibrationSheet:
    def test_writes_a_pdf(self, tmp_path):
        target = tmp_path / "cal.pdf"

        build_calibration(target)

        assert target.read_bytes().startswith(b"%PDF")
