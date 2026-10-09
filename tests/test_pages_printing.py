"""Tests for the pages of the sticker sheet, the overview and the printer calibration."""

from unittest.mock import patch

import pytest
from web_helpers import captured_sheet, jpeg_bytes, publish_album


class TestSheetPage:
    def test_says_so_while_no_model_has_a_cover(self, client):
        assert "Noch kein Modell" in client.get("/sheet").text

    def test_lists_a_published_tonie(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert "Lichterkinder" in page and "TS-0001" in page

    def test_passes_the_cover_through_from_teddycloud(self, client, runner, teddycloud):
        # The browser cannot reach teddyCloud's own address, so the image has to
        # come through this app.
        name = publish_album(client, runner, teddycloud)

        response = client.get(f"/cover/custom/{name}")

        assert response.status_code == 200
        assert response.content.startswith(b"\xff\xd8\xff")

    def test_refuses_a_name_that_climbs_out_of_the_image_folder(self, client):
        assert client.get("/cover/custom/..%2Fconfig").status_code in (400, 404)

    def test_serves_the_cover_the_way_the_chosen_mode_prints_it(self, client, runner, teddycloud):
        # Pointing at "ganzes Cover" and being shown the edge to edge version
        # answers a question nobody asked, so each mode renders its own picture.
        name = publish_album(client, runner, teddycloud)

        fill = client.get(f"/cover/custom/{name}?mode=fill&size=128")
        fit = client.get(f"/cover/custom/{name}?mode=fit&size=128")

        assert fill.status_code == fit.status_code == 200
        assert fill.content != fit.content

    def test_an_unknown_mode_still_gives_the_cover(self, client, runner, teddycloud):
        name = publish_album(client, runner, teddycloud)

        response = client.get(f"/cover/custom/{name}?mode=nonsense")

        assert response.status_code == 200
        assert response.content.startswith(b"\xff\xd8\xff")


class TestSheet:
    def test_builds_a_pdf_for_the_chosen_count(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        # The fake stored a three byte "jpeg", which Pillow cannot read, so the
        # cover is replaced with a real one for this test.
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        response = client.post("/sheet", data={"models": ["TS-0001"], "counts": ["4"]})

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_the_back_side_doubles_what_goes_on_the_sheet(self, client, runner, teddycloud):
        # One text side per copy, so four tonies come off the sheet as four
        # pairs rather than four pictures and a guess.
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["2"], "back": "on"})

        stickers = asked[-1]
        assert [sticker.count for sticker in stickers] == [2, 2]
        assert [sticker.back is None for sticker in stickers] == [True, False]

    def test_without_the_box_ticked_only_the_cover_is_printed(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"]})

        assert all(sticker.back is None for sticker in asked[-1])

    def test_the_back_can_leave_the_track_names_off(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"],
                                        "back": "on", "back_tracks": "on"})

        assert not asked[0][1].back.tracks  # the box is only ticked the second time
        assert asked[1][1].back.tracks

    def test_the_back_carries_the_year_and_the_playing_time(self, client, runner,
                                                            teddycloud, settings):
        # Both only exist because the app put them there: the year in the
        # catalogue's own release field, the playing time beside the app's data
        # because the format has nowhere for it.
        from tonie_studio.runtimes import remember

        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()
        model = next(entry for entry in teddycloud.custom_models
                     if entry["model"] == "TS-0001")
        model["release"] = "1609459200"  # 2021-01-01
        remember(settings.data_dir, "TS-0001", 3960)

        with captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})

        notes = " ".join(asked[-1][1].back.notes)
        assert "2021" in notes
        assert "66 Minuten" in notes

    def test_a_tonie_recorded_before_says_less_rather_than_guessing(self, client, runner,
                                                                    teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"], "back": "on"})

        notes = " ".join(asked[-1][1].back.notes)
        assert "Minuten" not in notes
        assert "TS-0001" in notes

    def test_the_page_offers_the_back_side(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="back"' in page and "Rückseite" in page

    def test_takes_an_uploaded_image_as_well(self, client):
        response = client.post(
            "/sheet",
            data={"models": [], "counts": []},
            files=[("files", ("extra.jpg", jpeg_bytes(), "image/jpeg"))],
        )

        assert response.content.startswith(b"%PDF")

    def test_offers_the_choice_between_filling_and_fitting(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="modes"' in page
        assert "randlos" in page and "ganzes Cover" in page

    def test_passes_the_chosen_mode_through_per_sticker(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()
        seen = []

        import tonie_studio.pages.printing as web
        original = web.build_sheet
        try:
            web.build_sheet = lambda stickers, target, shift=(0.0, 0.0): (
                seen.extend(s.mode for s in stickers) or original(stickers, target))
            client.post("/sheet", data={
                "models": ["TS-0001"], "counts": ["2"], "modes": ["fit"],
            })
        finally:
            web.build_sheet = original

        assert seen == ["fit", "fit"] or seen == ["fit"]

    def test_refuses_a_sheet_with_nothing_on_it(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        response = client.post("/sheet", data={"models": ["TS-0001"], "counts": ["0"]})

        assert response.status_code == 400

    def test_leaves_no_pdf_behind_in_the_data_directory(self, client, settings):
        client.post("/sheet", files=[("files", ("extra.jpg", jpeg_bytes(), "image/jpeg"))])

        assert list(settings.data_dir.glob("sheet-*.pdf")) == []


class TestPrintFromTonie:
    def test_offers_a_sticker_link_per_recorded_tonie(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert "/sheet?model=TS-0001" in client.get("/tonies").text

    def test_the_sheet_arrives_with_that_motif_asked_for(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet?model=TS-0001").text

        assert 'value="1"' in page.split('name="counts"')[1][:80]

    def test_without_a_model_nothing_is_preselected(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'value="0"' in page.split('name="counts"')[1][:80]


class TestOverviewSheet:
    def test_lists_the_tonies_on_stickers(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        response = client.get("/sheet/overview")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_a_model_on_no_sticker_is_left_out(self, client, runner, teddycloud):
        from tonie_studio.pages.printing import _overview_entries

        publish_album(client, runner, teddycloud)
        teddycloud.custom_models.append({"model": "TS-0009", "series": "Nirgends"})

        titles = [entry.series for entry in _overview_entries(teddycloud, {})]

        assert "Nirgends" not in titles

    def test_nothing_to_list_is_said(self, client):
        assert client.get("/sheet/overview").status_code == 400


class TestCalibrationPages:
    def test_the_calibration_sheet_downloads(self, client):
        response = client.get("/sheet/calibration")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_a_measurement_with_a_decimal_comma_is_understood(self, client, settings):
        from tonie_studio.calibration import expected_first_centre, read_shift

        left, top = expected_first_centre()
        typed_left = f"{left + 1:.1f}".replace(".", ",")

        client.post("/sheet/calibration", data={"left_mm": typed_left, "top_mm": f"{top:.1f}"})

        assert read_shift(settings.data_dir)[0] == pytest.approx(-1.0, abs=0.06)

    def test_the_stored_shift_reaches_the_sheet(self, client, runner, teddycloud, settings):
        from tonie_studio.calibration import expected_first_centre

        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()
        left, top = expected_first_centre()
        client.post("/sheet/calibration", data={"left_mm": str(left + 2), "top_mm": str(top)})

        shifts = []

        def instead(stickers, target, shift=(0.0, 0.0)):
            shifts.append(shift)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.pages.printing.build_sheet", side_effect=instead):
            client.post("/sheet", data={"models": ["TS-0001"], "counts": ["1"]})

        assert shifts[-1][0] == pytest.approx(-2.0, abs=0.06)

    def test_a_reset_removes_the_shift(self, client, settings):
        from tonie_studio.calibration import expected_first_centre, read_shift

        left, top = expected_first_centre()
        client.post("/sheet/calibration", data={"left_mm": str(left + 2), "top_mm": str(top + 1)})

        client.post("/sheet/calibration/reset")

        assert read_shift(settings.data_dir) == pytest.approx((0.0, 0.0))

    def test_nonsense_is_refused(self, client):
        response = client.post("/sheet/calibration", data={"left_mm": "links", "top_mm": "2"})

        assert response.status_code == 400


class TestPrintedLanguage:
    def test_the_back_side_speaks_the_readers_language(self):
        from tonie_studio.i18n import Translator
        from tonie_studio.pages.printing import _back_of
        from tonie_studio.models import Model

        # The release is 2019-01-01 in the catalogue's seconds-since-1970 form.
        model = Model(id="TS-0001", series="S", episode="E", release="1546300800",
                      tracks=["a", "b"])

        assert _back_of(model, 1500, Translator("en")).notes[0] == "2019 - 2 tracks - 25 minutes"
        assert _back_of(model, 1500, Translator("de")).notes[0] == "2019 - 2 Tracks - 25 Minuten"
        assert _back_of(model, 1500, Translator("en")).language == "en"


class TestSheetRefusesWhatItCannotPrint:
    def test_only_pictures_it_cannot_read_is_refused_not_a_crash(self, client):
        response = client.post("/sheet", files=[("files", ("x.png", b"not an image", "image/png"))])

        assert response.status_code == 400

    def test_a_count_beyond_a_sheet_full_is_held_to_what_a_person_can_ask_for(
            self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]
        asked = []

        def instead(stickers, target, shift=(0.0, 0.0)):
            asked.extend(sticker.count for sticker in stickers)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.pages.printing.build_sheet", side_effect=instead):
            client.post("/sheet", data={"models": [model], "counts": ["100000000"]})

        assert asked and max(asked) <= 99
