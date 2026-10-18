"""Tests for the pages of the sticker sheet, the overview and the printer calibration."""

from unittest.mock import patch

import pytest
from web_helpers import captured_sheet, jpeg_bytes, print_sheet, publish_album


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

        response = print_sheet(client, [("TS-0001", 4)])

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_the_back_side_doubles_what_goes_on_the_sheet(self, client, runner, teddycloud):
        # One text side per copy, so four tonies come off the sheet as four
        # pairs rather than four pictures and a guess.
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            print_sheet(client, [("TS-0001", 2)], back="on")

        stickers = asked[-1]
        assert [sticker.count for sticker in stickers] == [2, 2]
        assert [sticker.back is None for sticker in stickers] == [True, False]

    def test_without_the_box_ticked_only_the_cover_is_printed(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            print_sheet(client, [("TS-0001", 1)])

        assert all(sticker.back is None for sticker in asked[-1])

    def test_the_back_can_leave_the_track_names_off(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            print_sheet(client, [("TS-0001", 1)], back="on")
            print_sheet(client, [("TS-0001", 1)], back="on", back_tracks="on")

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
            print_sheet(client, [("TS-0001", 1)], back="on")

        notes = " ".join(asked[-1][1].back.notes)
        assert "2021" in notes
        assert "66 Minuten" in notes

    def test_a_tonie_recorded_before_says_less_rather_than_guessing(self, client, runner,
                                                                    teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        with captured_sheet() as asked:
            print_sheet(client, [("TS-0001", 1)], back="on")

        notes = " ".join(asked[-1][1].back.notes)
        assert "Minuten" not in notes
        assert "TS-0001" in notes

    def test_the_page_offers_the_back_side(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="back"' in page and "Rückseite" in page

    def test_takes_an_uploaded_image_as_well(self, client):
        response = print_sheet(client, files=[("files", ("extra.jpg", jpeg_bytes(), "image/jpeg"))])

        assert response.content.startswith(b"%PDF")

    def test_offers_the_choice_between_filling_and_fitting(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="picks"' in page and 'value="fit"' in page
        assert "randlos" in page and "ganzes Cover" in page

    def test_passes_the_chosen_mode_through_per_sticker(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()
        seen = []

        import tonie_studio.printing as web
        original = web.build_sheet
        try:
            web.build_sheet = lambda stickers, target, shift=(0.0, 0.0), progress=None, layout=None: (
                seen.extend(s.mode for s in stickers) or original(stickers, target))
            print_sheet(client, [("TS-0001", 2, "fit")])
        finally:
            web.build_sheet = original

        assert seen == ["fit", "fit"] or seen == ["fit"]

    def test_refuses_a_sheet_with_nothing_on_it(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        response = print_sheet(client, [("TS-0001", 0)])

        assert response.status_code == 400

    def test_keeps_only_the_latest_sheets_for_downloading_again(self, client, settings):
        from tonie_studio.printing import KEEP_SHEETS

        for _sheet in range(KEEP_SHEETS + 2):
            print_sheet(client, files=[("files", ("extra.jpg", jpeg_bytes(), "image/jpeg"))])

        assert len(list(settings.data_dir.glob("sheet-*.pdf"))) == KEEP_SHEETS


class TestPrintFromTonie:
    def test_a_recorded_tonie_s_page_offers_its_sticker(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert "/sheet?model=TS-0001" in client.get("/tonies/7ab4ed0e500304e0").text

    def test_the_sheet_arrives_with_that_motif_asked_for(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet?model=TS-0001").text

        assert 'value="1:fill:TS-0001"' in page

    def test_without_a_model_nothing_is_preselected(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'value="0:fill:TS-0001"' in page


class TestOverviewSheet:
    def test_lists_the_tonies_on_stickers(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()

        response = client.get("/sheet/overview")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_a_model_on_no_sticker_is_left_out(self, client, runner, teddycloud):
        from tonie_studio.printing import overview_entries

        publish_album(client, runner, teddycloud)
        teddycloud.custom_models.append({"model": "TS-0009", "series": "Nirgends"})

        titles = [entry.series for entry in overview_entries(teddycloud, {})]

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

        def instead(stickers, target, shift=(0.0, 0.0), progress=None, layout=None):
            shifts.append(shift)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.printing.build_sheet", side_effect=instead):
            print_sheet(client, [("TS-0001", 1)])

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
        from tonie_studio.printing import back_of
        from tonie_studio.models import Model

        # The release is 2019-01-01 in the catalogue's seconds-since-1970 form.
        model = Model(id="TS-0001", series="S", episode="E", release="1546300800",
                      tracks=["a", "b"])

        assert back_of(model, 1500, Translator("en")).notes[0] == "2019 - 2 tracks - 25 minutes"
        assert back_of(model, 1500, Translator("de")).notes[0] == "2019 - 2 Tracks - 25 Minuten"
        assert back_of(model, 1500, Translator("en")).language == "en"


class TestSheetRefusesWhatItCannotPrint:
    def test_only_pictures_it_cannot_read_is_refused_not_a_crash(self, client):
        response = print_sheet(client, files=[("files", ("x.png", b"not an image", "image/png"))])

        assert response.status_code == 400

    def test_a_count_beyond_a_sheet_full_is_held_to_what_a_person_can_ask_for(
            self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]
        asked = []

        def instead(stickers, target, shift=(0.0, 0.0), progress=None, layout=None):
            asked.extend(sticker.count for sticker in stickers)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.printing.build_sheet", side_effect=instead):
            print_sheet(client, [(model, 100000000)])

        assert asked and max(asked) <= 99


class TestSheetReadsTheModelsOnce:
    def test_a_sheet_of_several_tonies_asks_for_the_models_once(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]
        original = teddycloud.custom_tonies
        asked = []
        teddycloud.custom_tonies = lambda: asked.append(1) or original()

        with captured_sheet():
            print_sheet(client, [(model, 1)] * 3)

        assert len(asked) == 1


class TestNoPdfIsLeftBehind:
    def test_a_sheet_that_fails_half_way_leaves_no_file(self, client, runner, teddycloud,
                                                        settings):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]

        def half_written(stickers, target, shift, progress=None, layout=None):
            target.write_bytes(b"%PDF-1.4 half")
            raise RuntimeError("reportlab ran out of something")

        with patch("tonie_studio.printing.build_sheet", side_effect=half_written):
            response = print_sheet(client, [(model, 1)])

        assert response.status_code == 500
        assert list(settings.data_dir.glob("*.pdf")) == []

    def test_housekeeping_clears_a_pdf_a_dead_process_left(self, settings):
        import os
        import time

        from tonie_studio.printing import clear_old_pdfs

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        old = settings.data_dir / "sheet-dead.pdf"
        new = settings.data_dir / "sheet-sending.pdf"
        old.write_bytes(b"%PDF"), new.write_bytes(b"%PDF")
        two_days_ago = time.time() - 2 * 86400
        os.utime(old, (two_days_ago, two_days_ago))

        clear_old_pdfs(settings.data_dir)

        assert not old.exists() and new.exists()


class TestPrintedCoversAreNotStickers:
    """What comes out of the printer is a cover to cut out; a sticker is the NFC tag."""

    def test_no_page_calls_the_printed_circle_an_aufkleber(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        for path in ("/sheet", "/tonies", "/checkup"):
            assert "Aufkleber" not in client.get(path).text, path

    def test_the_print_page_says_plain_paper_and_cutting_out(self, client):
        client.cookies.set("lang", "en")

        page = client.get("/sheet").text

        assert "46 mm covers to cut out" in page
        assert "46 mm stickers" not in page


class TestEveryStickerIsOnThePage:
    """The page lists every sticker with a cover, findable by its UID, and prints any number."""

    @staticmethod
    def two_stickers(client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.images["TS-0001.jpg"] = jpeg_bytes()
        teddycloud.add_tag("11dfc517500304e0", source="lib://x.taf", tonie_model="TS-0001")

    def test_a_row_per_sticker_even_when_two_play_the_same(self, client, runner, teddycloud):
        self.two_stickers(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert page.count('data-model="TS-0001"') == 2
        assert "E0:04:03:50:0E:ED:B4:7A" in page and "E0:04:03:50:17:C5:DF:11" in page

    def test_the_uid_is_searchable_with_and_without_its_colons(self, client, runner,
                                                              teddycloud):
        self.two_stickers(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert "e0:04:03:50:17:c5:df:11 e004035017c5df11" in page

    def test_a_model_on_no_sticker_is_still_offered(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.custom_models.append({"model": "TS-0009", "series": "Nirgends",
                                         "pic": "/custom_img/TS-0009.jpg"})

        assert 'data-model="TS-0009"' in client.get("/sheet").text

    def test_a_search_asked_for_by_address_is_put_in_the_search_box(self, client, runner,
                                                                     teddycloud):
        publish_album(client, runner, teddycloud)

        assert 'value="bobo"' in client.get("/sheet?q=bobo").text

    def test_a_sheet_of_hundreds_of_covers_is_taken(self, client, runner, teddycloud):
        # Three fields a row would pass the 1000 fields a form may carry.
        publish_album(client, runner, teddycloud)

        with captured_sheet() as asked:
            response = print_sheet(client, [("TS-0001", 1)] * 600)

        assert response.status_code == 200 and len(asked[-1]) == 600


class TestTheSheetIsMadeInTheBackground:
    def test_asking_leads_to_a_page_that_watches_it(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        with captured_sheet():
            response = client.post("/sheet", data={"picks": ["1:fill:TS-0001"]},
                                   follow_redirects=False)
            job_id = response.headers["location"].rsplit("/", 1)[1]
            client.app.state.prints.get(job_id).done.wait(10)

        assert response.status_code == 303
        assert client.get(f"/sheet/print/{job_id}").status_code == 200
        state = client.get(f"/api/sheet/print/{job_id}").json()
        assert (state["state"], state["percent"]) == ("done", 100)
        # Opened again, the page offers the file instead of downloading it anew.
        assert '"finished": true' in client.get(f"/sheet/print/{job_id}").text

    def test_a_job_nobody_started_is_unknown(self, client):
        assert client.get("/sheet/print/nope").status_code == 404
        assert client.get("/sheet/print/nope/pdf").status_code == 404

    def test_a_failed_sheet_says_why(self, client):
        response = client.post("/sheet", files=[("files", ("x.png", b"not an image", "image/png"))],
                               follow_redirects=False)
        job_id = response.headers["location"].rsplit("/", 1)[1]
        client.app.state.prints.get(job_id).done.wait(10)

        state = client.get(f"/api/sheet/print/{job_id}").json()

        assert state["state"] == "failed" and state["problem"]


class TestLayouts:
    """Covers cut with scissors or with a circle punch come from the same page."""

    def test_the_page_offers_both_ways_of_cutting(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/sheet").text

        assert 'name="layout" form="sheetform" value="sheet" checked' in page
        assert 'value="punch"' in page and "44,5 mm" in page

    def test_the_chosen_layout_reaches_the_sheet(self, client, runner, teddycloud):
        from tonie_studio.sheet import PUNCH, SHEET

        publish_album(client, runner, teddycloud)
        layouts = []

        def instead(stickers, target, shift=(0.0, 0.0), progress=None, layout=None):
            layouts.append(layout)
            target.write_bytes(b"%PDF")
            return []

        with patch("tonie_studio.printing.build_sheet", side_effect=instead):
            print_sheet(client, [("TS-0001", 1)], layout="punch")
            print_sheet(client, [("TS-0001", 1)])
            print_sheet(client, [("TS-0001", 1)], layout="nonsense")

        assert layouts == [PUNCH, SHEET, SHEET]

    def test_the_back_side_is_laid_out_for_the_layout_asked_for(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        sheet = client.get("/api/sheet/back", params={"model": "TS-0001"}).json()
        punch = client.get("/api/sheet/back",
                           params={"model": "TS-0001", "layout": "punch"}).json()

        assert sheet["plain"][0]["bold"] and sheet["tracks"] and punch["tracks"]

    def test_the_back_side_of_an_unknown_model_is_refused(self, client):
        assert client.get("/api/sheet/back", params={"model": "TS-9999"}).status_code == 404
