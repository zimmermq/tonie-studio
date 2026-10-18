"""Tests for the pages of several albums at once, the walkthrough and the stickers placed on the
box.
"""

import re
from unittest.mock import patch

from web_helpers import (batch_ids, checked_ruid, current_step, encode_and_assign_page,
                         first_album_id, group_jobs, place_sticker, publish_album, run_batch,
                         start_placing, start_placing_archive, upload_series)

from tonie_studio.jobs import JobState


class TestSeveralFolders:
    def test_a_folder_of_folders_becomes_one_album_each(self, client):
        response = upload_series(client)

        assert response.status_code == 303
        assert len(batch_ids(response)) == 3
        page = client.get("/").text
        assert "Folge 1" in page and "Folge 2" in page and "Folge 3" in page

    def test_the_batch_page_lists_the_episodes_in_order_with_their_stickers(self, client):
        ids = batch_ids(upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.index("Folge 1") < page.index("Folge 2")
        assert "7ab4ed0e500304e0" in page and "deb4ed0e500304e0" in page

    def test_says_which_episodes_will_have_no_sticker(self, client):
        ids = batch_ids(upload_series(client, episodes=3))

        page = client.get("/batch", params={"ids": ids}).text

        # Two free tags, three episodes.
        assert page.count("ohne Sticker") == 1

    def test_encodes_every_album_and_assigns_the_free_tags(self, client, runner, teddycloud):
        ids = batch_ids(upload_series(client, episodes=2))

        run_batch(client, runner, ids, series="Serie")

        sources = sorted(entry["source"] for entry in teddycloud.tag_entries.values())
        assert sources == ["lib://Serie/Folge 1.taf", "lib://Serie/Folge 2.taf"]
        episodes = sorted(model["episodes"] for model in teddycloud.custom_tonies())
        assert episodes == ["Folge 1", "Folge 2"]

    def test_the_first_free_tag_gets_the_first_episode(self, client, runner, teddycloud):
        from tonie_studio.tags import list_tags

        free = [tag.ruid for tag in list_tags(teddycloud) if not tag.assigned]
        ids = batch_ids(upload_series(client, episodes=2))

        run_batch(client, runner, ids, series="Serie")

        assert teddycloud.tag_entries[free[0]]["source"] == "lib://Serie/Folge 1.taf"

    def test_an_episode_without_a_sticker_is_encoded_and_left_to_assign(self, client, runner,
                                                                        teddycloud):
        ids = batch_ids(upload_series(client, episodes=3))

        run_batch(client, runner, ids, series="Serie")

        from tonie_studio.encode import library_path, taf_exists

        assert taf_exists(teddycloud, library_path("Serie", "Folge 3"))
        assert len(teddycloud.custom_tonies()) == 2
        third = next(job for job in runner.all() if job.label.endswith("Folge 3.taf"))
        assert third.state == JobState.DONE and not third.result.model

    def test_a_sticker_taken_in_the_meantime_is_not_overwritten(self, client, runner, teddycloud):
        ids = batch_ids(upload_series(client, episodes=2))
        # Taken as the first encode is queued, after the plan was made.
        original = runner.submit

        def take_then_submit(*args, **kwargs):
            teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
            teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Zwei.taf"
            return original(*args, **kwargs)

        runner.submit = take_then_submit
        run_batch(client, runner, ids, series="Serie")

        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Andere/Folge.taf"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Andere/Zwei.taf"
        for job in runner.all():
            assert job.state == JobState.DONE and job.result.assign_error
        note = client.get(f"/api/job/{runner.all()[0].id}").json()["note"]
        assert note and not note.startswith("batch.")

    def test_the_edited_names_are_used(self, client, runner, teddycloud):
        ids = batch_ids(upload_series(client, episodes=2))

        run_batch(client, runner, ids, series="Bobo", episodes=["Im Zoo", "Am Meer"])

        models = {model["episodes"]: model for model in teddycloud.custom_tonies()}
        assert set(models) == {"Im Zoo", "Am Meer"}
        assert all(model["series"] == "Bobo" for model in models.values())

    def test_the_album_list_offers_to_pick_several(self, client):
        page = client.get("/").text

        assert 'name="ids"' in page and 'action="/batch"' in page

    def test_refuses_an_empty_batch(self, client):
        assert client.post("/batch", data={}).status_code == 400

    def test_the_result_lists_every_encode(self, client, runner):
        ids = batch_ids(upload_series(client, episodes=2))

        response = run_batch(client, runner, ids, series="Serie")

        page = client.get(response.headers["location"]).text
        assert page.count('class="item" data-job=') == 2

    def test_an_encoded_album_without_a_sticker_only_gets_one(self, client, runner, teddycloud):
        # No free sticker, so the first round leaves tafs and no tonies - the
        # state a restart in the middle of placing leaves behind as well.
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = batch_ids(upload_series(client, episodes=2))
        run_batch(client, runner, ids, series="Serie")
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")

        page = client.get("/batch", params={"ids": ids}).text
        with patch("tonie_studio.publishing.encode_files",
                   side_effect=AssertionError("encoded again")):
            run_batch(client, runner, ids, series="Serie", stickers="auto")

        assert page.count("schon encodiert, bekommt nur noch einen Sticker") == 2
        assert teddycloud.tag_entries["aaaaaaaaaaaaaaaa"]["source"] == "lib://Serie/Folge 1.taf"
        assert [model["episodes"] for model in teddycloud.custom_tonies()] == ["Folge 1"]

    def test_an_encoded_album_a_sticker_plays_is_skipped(self, client, runner, teddycloud, settings):
        ids = batch_ids(upload_series(client, episodes=2))
        run_batch(client, runner, ids, series="Serie")
        # Neither recorded nor recognisable: only the taf and its sticker are left.
        (settings.data_dir / "albums-made.json").write_text("{}")
        for model in teddycloud.custom_models:
            model["episodes"], model["tracks"] = "anders", ["x", "y"]

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("schon encodiert und auf einem Sticker") == 2

    def test_a_taf_with_other_chapters_is_not_taken_for_the_album(self, client, runner, teddycloud):
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = batch_ids(upload_series(client, episodes=2))
        run_batch(client, runner, ids, series="Serie")
        for header in teddycloud.taf_headers.values():
            header["trackSeconds"] = [1, 2, 3, 4, 5]

        page = client.get("/batch", params={"ids": ids}).text

        assert "nur noch einen Sticker" not in page
        assert page.count("schon encodiert (liegt in der Library), aber kein Sticker spielt es") == 2

    def test_episode_ten_comes_after_episode_two(self, client):
        files = [("files", (f"Serie/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (10, 2, 1)]
        ids = batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.index('value="Folge 1"') < page.index('value="Folge 2"') < page.index('value="Folge 10"')

    def test_the_folder_holding_the_episodes_is_the_suggested_series(self, client):
        ids = batch_ids(upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count('name="series" value="Serie"') == 2

    def test_a_deeper_pick_keeps_only_series_and_episode(self, client):
        files = [("files", (f"Kinder/Serie/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (1, 2)]
        ids = batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids})

        assert page.status_code == 200
        assert page.text.count('name="series" value="Serie"') == 2

    def test_albums_of_different_series_keep_their_own(self, client, runner, teddycloud):
        files = [("files", (f"{series}/Folge 1/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n, series in enumerate(("Bobo", "Lichterkinder"), 1)]
        ids = batch_ids(client.post("/upload", files=files, follow_redirects=False))

        page = client.get("/batch", params={"ids": ids}).text
        run_batch(client, runner, ids)

        assert 'value="Bobo"' in page and 'value="Lichterkinder"' in page
        sources = sorted(entry["source"] for entry in teddycloud.tag_entries.values())
        assert sources == ["lib://Bobo/Folge 1.taf", "lib://Lichterkinder/Folge 1.taf"]

    def test_a_skipped_album_still_sends_its_names(self, client, runner):
        # A disabled field is not submitted, which would hand the next album
        # the skipped one's name; read-only fields are.
        ids = batch_ids(upload_series(client, episodes=2))
        run_batch(client, runner, ids, series="Serie")

        page = client.get("/batch", params={"ids": ids}).text
        rows = page.split('<div class="list">')[1]

        assert rows.count('name="episodes"') == 2 and rows.count("readonly") == 4
        assert 'name="episodes" value="Folge 1"' in rows and "disabled>" not in rows.split("<button")[0]

    def test_an_album_that_already_has_a_tonie_is_skipped(self, client, runner, teddycloud):
        # Made back then under names typed by hand, so its taf path differs
        # from what the batch would suggest; the tonie is what gives it away.
        ids = batch_ids(upload_series(client, episodes=2))
        run_batch(client, runner, ids, series="Serie")
        from tonie_studio.encode import taf_exists

        # The audio still plays, from where it was put back then.
        for job in runner.all():
            moved = job.label.replace(".taf", " (alt).taf")
            teddycloud.files[moved] = teddycloud.files.pop(job.label)
            for entry in teddycloud.tag_entries.values():
                if entry["source"] == "lib:/" + job.label:
                    entry["source"] = "lib:/" + moved
            assert not taf_exists(teddycloud, job.label)
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("hat schon einen Tonie") == 2
        assert "Nichts Neues zu encodieren" in page

    def test_says_before_the_click_that_stickers_are_written_at_once(self, client):
        ids = batch_ids(upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert "ohne weitere R\u00fcckfrage" in page
        assert "2 Alben encodieren und auf Sticker legen" in page
        assert "Freie Sticker sehen gleich aus" in page

    def test_the_button_does_not_promise_stickers_when_none_are_free(self, client, teddycloud):
        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        ids = batch_ids(upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert "2 Alben encodieren" in page and "auf Sticker legen" not in page
        assert "Freie Sticker sehen gleich aus" not in page


class TestPlacingStickers:
    def test_a_placement_still_waiting_can_be_found_again(self, client, runner, teddycloud):
        # Placing works only while its page is open; someone who left it
        # needs a way back, or the stickers laid down later get nothing.
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        assert f'href="/parts/{group}"' in client.get("/").text

        place_sticker(client, group, "deb4ed0e500304e0")
        place_sticker(client, group, "7ab4ed0e500304e0")

        assert f'href="/parts/{group}"' not in client.get("/").text

    def test_nothing_is_assigned_until_a_sticker_is_placed(self, client, runner, teddycloud):
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        assert all(not entry["source"] for entry in teddycloud.tag_entries.values())
        assert "Leg einen freien Sticker auf die Box" in client.get(f"/parts/{group}").text

    def test_each_placed_sticker_gets_the_next_episode(self, client, runner, teddycloud):
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        first = place_sticker(client, group, "deb4ed0e500304e0")
        second = place_sticker(client, group, "7ab4ed0e500304e0")

        assert first["status"] == "assigned" and first["episode"] == "Folge 1"
        assert second["status"] == "assigned" and second["episode"] == "Folge 2"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"
        assert teddycloud.tag_entries["7ab4ed0e500304e0"]["source"] == "lib://Serie/Folge 2.taf"

    def test_a_sticker_placed_during_the_encode_is_filled_when_it_finishes(self, client, runner,
                                                                          teddycloud):
        import threading

        import tonie_studio.publishing as common

        gate = threading.Event()
        original = common.encode_files

        def slow(*args, **kwargs):
            assert gate.wait(5)
            return original(*args, **kwargs)

        with patch("tonie_studio.publishing.encode_files", side_effect=slow):
            group = start_placing(client, runner)
            placed = place_sticker(client, group, "deb4ed0e500304e0")
            assert placed["status"] == "reserved" and placed["episode"] == "Folge 1"
            gate.set()
            for job in runner.all():
                assert job.done.wait(5)

        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"

    def test_an_episode_assigned_by_hand_is_not_placed_again(self, client, runner, teddycloud):
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)
        first = next(job for job in runner.all() if job.result.episode == "Folge 1")
        client.post(f"/job/{first.id}/assign", data={
            "ruid": "deb4ed0e500304e0", "series": "Serie", "episode": "Folge 1"},
            follow_redirects=False)

        placed = place_sticker(client, group, "7ab4ed0e500304e0")

        assert placed["status"] == "assigned" and placed["episode"] == "Folge 2"
        assert len(teddycloud.custom_tonies()) == 2
        assert placed["next"] is None

    def test_the_sticker_still_on_the_box_is_not_given_a_second_episode(self, client, runner,
                                                                         teddycloud):
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        place_sticker(client, group, "deb4ed0e500304e0")
        again = place_sticker(client, group, "deb4ed0e500304e0")

        assert again["status"] == "assigned" and again["episode"] == "Folge 1"
        assert not teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]

    def test_a_sticker_that_plays_something_is_left_alone(self, client, runner, teddycloud):
        teddycloud.tag_entries["deb4ed0e500304e0"]["source"] = "lib://Andere/Folge.taf"
        group = start_placing(client, runner)

        answer = place_sticker(client, group, "deb4ed0e500304e0")

        assert answer["status"] == "taken"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Andere/Folge.taf"

    def test_a_sticker_teddycloud_has_not_filed_yet_is_waited_for(self, client, runner):
        group = start_placing(client, runner)

        assert place_sticker(client, group, "ffffffffffffffff")["status"] == "unknown"

    def test_says_when_every_episode_has_its_sticker(self, client, runner, teddycloud):
        teddycloud.add_tag("aaaaaaaaaaaaaaaa")
        group = start_placing(client, runner, episodes=2)
        for job in runner.all():
            assert job.done.wait(5)
        place_sticker(client, group, "deb4ed0e500304e0")
        place_sticker(client, group, "7ab4ed0e500304e0")

        answer = place_sticker(client, group, "aaaaaaaaaaaaaaaa")

        assert answer["status"] == "complete"
        assert not teddycloud.tag_entries["aaaaaaaaaaaaaaaa"]["source"]

    def test_the_batch_page_offers_both_ways(self, client):
        ids = batch_ids(upload_series(client, episodes=2))

        page = client.get("/batch", params={"ids": ids}).text

        assert 'name="stickers" value="auto"' in page and 'name="stickers" value="place"' in page

    def test_an_unknown_batch_is_refused(self, client):
        assert client.post("/api/batch/nope/place", data={"ruid": "x"}).status_code == 404

    def test_an_encode_done_before_the_batch_is_set_up_still_gets_its_sticker(
            self, client, runner, teddycloud):
        # Each encode finishes before the next is even queued, so the first
        # ones settle before the request has registered the batch.
        original = runner.submit

        def submit_and_finish(*args, **kwargs):
            job_id = original(*args, **kwargs)
            assert runner.get(job_id).done.wait(5)
            return job_id

        runner.submit = submit_and_finish
        group = start_placing(client, runner)

        answer = place_sticker(client, group, "deb4ed0e500304e0")

        assert answer["status"] == "assigned"
        assert teddycloud.tag_entries["deb4ed0e500304e0"]["source"] == "lib://Serie/Folge 1.taf"


class TestWalkthrough:
    """One path for a new tonie: choose, check, encode, sticker, print."""

    def test_an_album_in_the_list_leads_to_checking_it(self, client):
        identifier = first_album_id(client)

        assert f'href="/batch?ids={identifier}"' in client.get("/").text

    def test_the_detailed_album_page_is_one_click_away(self, client):
        identifier = first_album_id(client)

        page = client.get("/batch", params={"ids": [identifier]}).text

        assert f'href="/album/{identifier}"' in page
        assert client.get(f"/album/{identifier}").status_code == 200

    def test_one_uploaded_folder_goes_to_checking_too(self, client):
        response = client.post("/upload", files=[
            ("files", ("Album/01-eins.mp3", b"\x01" * 8, "audio/mpeg")),
        ], follow_redirects=False)

        assert response.headers["location"].startswith("/batch?ids=")

    def test_every_step_says_where_it_is(self, client, runner):
        identifier = first_album_id(client)

        assert 'class="steps"' in client.get("/").text
        assert current_step(client.get("/").text) == "1"
        assert current_step(client.get("/batch", params={"ids": [identifier]}).text) == "2"
        group = start_placing_archive(client, identifier)
        assert current_step(client.get(f"/parts/{group}").text) in ("3", "4")

    def test_after_the_stickers_comes_printing_exactly_these(self, client, runner, teddycloud):
        ids = batch_ids(upload_series(client, episodes=2))
        response = client.post("/batch", data={"ids": ids, "series": ["Serie"] * 2,
                                               "stickers": "place"}, follow_redirects=False)
        group = response.headers["location"].rsplit("/", 1)[1]
        for job in runner.all():
            assert job.done.wait(5)
        place_sticker(client, group, "deb4ed0e500304e0")
        place_sticker(client, group, "7ab4ed0e500304e0")

        page = client.get(f"/parts/{group}").text
        models = sorted(model["model"] for model in teddycloud.custom_tonies())

        assert f'href="/sheet?model={models[0]}&amp;model={models[1]}"' in page
        assert models == [job["model"] for job in sorted(
            (client.get(f"/api/job/{job_id}").json() for job_id in group_jobs(page)),
            key=lambda job: job["model"])]

    def test_the_sheet_takes_several_tonies_at_once(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_tonies()[0]["model"]

        page = client.get("/sheet", params={"model": [model, "TS-9999"]}).text

        assert page.count('class="want" checked') == 1
        assert current_step(page) == "5"

    def test_the_sheet_without_a_walkthrough_has_no_steps(self, client):
        assert 'class="steps"' not in client.get("/sheet").text

    def test_a_free_sticker_on_the_box_is_chosen_on_the_assignment_page(self, client, runner, box):
        job, _page = encode_and_assign_page(client, runner)
        assert checked_ruid(client.get(f"/job/{job.id}/assign").text) != "deb4ed0e500304e0"
        # The same sticker as uid; the tag index names it by its ruid.
        box.handle("TagValid", "E00403500EEDB4DE")

        page = client.get(f"/job/{job.id}/assign").text

        assert checked_ruid(page) == "deb4ed0e500304e0"


class TestWalkthroughHints:
    def test_an_empty_album_list_says_why_and_what_to_do(self, make_site, settings, tmp_path):
        import shutil

        shutil.rmtree(settings.archive_dir)
        empty = make_site()
        page = empty.get("/").text

        assert "Keine Alben gefunden" in page

    def test_the_album_list_explains_without_jargon(self, client):
        page = client.get("/").text
        intro = page[page.index('<p class="sub">'):page.index("</p>", page.index('<p class="sub">'))]

        assert "Taf" not in intro and "Library" not in intro

    def test_placing_says_there_is_no_need_to_wait(self, client, runner):
        ids = batch_ids(upload_series(client, episodes=2))
        response = client.post("/batch", data={"ids": ids, "series": ["S"] * 2, "stickers": "place"},
                               follow_redirects=False)

        page = client.get(response.headers["location"]).text

        assert "nicht warten" in page

    def test_the_last_way_of_handing_out_stickers_is_remembered(self, client, runner):
        ids = batch_ids(upload_series(client, episodes=2))
        assert 'value="place" checked' in client.get("/batch", params={"ids": ids}).text

        client.post("/batch", data={"ids": ids, "series": ["S"] * 2, "stickers": "auto"},
                    follow_redirects=False)
        page = client.get("/batch", params={"ids": ids}).text

        assert 'value="auto" checked' in page and 'value="place" checked' not in page

    def test_until_calibrated_the_print_page_points_at_the_calibration(self, client, runner,
                                                                       teddycloud):
        publish_album(client, runner, teddycloud)

        assert "vorgestanzte" in client.get("/sheet").text
        client.post("/sheet/calibration", data={"left_mm": "31.5", "top_mm": "50.5"})
        assert "vorgestanzte" not in client.get("/sheet").text


class TestRecognisingTonies:
    def _album_with(self, client, *names):
        files = [("files", (f"Neu/Album/{n:02d}-{name}.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n, name in enumerate(names, 1)]
        response = client.post("/upload", files=files, follow_redirects=False)
        return response.headers["location"].split("ids=")[1]

    def test_an_album_whose_tracks_a_tonie_already_has_is_recognised(self, client, teddycloud):
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "Unter meinem Bett",
                                        "episodes": "Folge 1",
                                        "tracks": ["Der Tee", "Immer muss ich", "Sommer lang"]})
        identifier = self._album_with(client, "der_tee", "immer_muss_ich", "sommer_lang")

        page = client.get("/batch", params={"ids": [identifier]}).text

        assert "hat schon einen Tonie (TS-0042)" in page

    def test_one_track_left_out_still_counts(self, client, teddycloud):
        # Recorded without the advert at the start, as some tonies are.
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": ["Zwei", "Drei", "Vier", "Fuenf"]})
        identifier = self._album_with(client, "werbung", "zwei", "drei", "vier", "fuenf")

        assert "TS-0042" in client.get("/batch", params={"ids": [identifier]}).text

    def test_numbered_chapters_alone_prove_nothing(self, client, teddycloud):
        # Every audiobook calls its tracks "Kapitel 1" to "Kapitel n".
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": [f"Kapitel {n}" for n in range(1, 12)]})
        identifier = self._album_with(client, *[f"kapitel_{n}" for n in range(1, 14)])

        assert "TS-0042" not in client.get("/batch", params={"ids": [identifier]}).text

    def test_a_different_album_is_not_mistaken(self, client, teddycloud):
        teddycloud.upsert_custom_tonie({"model": "TS-0042", "series": "S", "episodes": "E",
                                        "tracks": ["Eins", "Zwei", "Drei"]})
        identifier = self._album_with(client, "eins", "vier", "fuenf", "sechs")

        assert "TS-0042" not in client.get("/batch", params={"ids": [identifier]}).text


class TestLoadingHint:
    def test_choosing_albums_says_it_is_reading_them(self, client):
        page = client.get("/").text

        assert 'id="loading"' in page and "Lese" in page


class TestSpellingsOfAPath:
    def test_a_taf_played_under_the_other_spelling_of_an_umlaut_counts_as_played(
            self, client, runner, teddycloud):
        # A share hands back decomposed umlauts, teddyCloud keeps whatever it
        # was given: the same path can arrive in two spellings.
        import unicodedata

        for entry in teddycloud.tag_entries.values():
            entry["source"] = "lib://Andere/Folge.taf"
        files = [("files", (f"B\u00e4r/Folge {n}/01-a.mp3", bytes([n]) * 8, "audio/mpeg"))
                 for n in (1, 2)]
        ids = batch_ids(client.post("/upload", files=files, follow_redirects=False))
        run_batch(client, runner, ids, series="B\u00e4r")
        sticker = next(iter(teddycloud.tag_entries))
        teddycloud.tag_entries[sticker]["source"] = unicodedata.normalize(
            "NFD", "lib://B\u00e4r/Folge 1.taf")

        page = client.get("/batch", params={"ids": ids}).text

        assert page.count("schon encodiert und auf einem Sticker") == 1
        assert page.count("schon encodiert, bekommt nur noch einen Sticker") == 1


class TestALongBatchKeepsItsJobs:
    def test_episodes_finished_long_ago_still_get_their_sticker(self, client, runner, teddycloud):
        # The runner forgets finished jobs beyond a cap; a batch still waiting
        # for its stickers must not lose its episodes to that.
        group = start_placing(client, runner, episodes=2)
        for job in runner.all():
            assert job.done.wait(5)
        for _ in range(60):
            other = runner.submit("encode", "/other.taf", lambda progress: None)
            assert runner.get(other).done.wait(5)

        answer = place_sticker(client, group, "deb4ed0e500304e0")

        assert answer["status"] == "assigned" and answer["episode"] == "Folge 1"


class TestPlacingAfterEncodesFailed:
    """A sticker placed when nothing is left to fill says why, not that all is done."""

    def test_when_every_episode_failed_the_sticker_hears_that(self, client, runner, teddycloud):
        teddycloud.encode_fails = "ffmpeg died"
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)

        placed = place_sticker(client, group, "deb4ed0e500304e0")

        assert placed["status"] == "none_waiting"
        assert "fehlgeschlagen" in placed["message"] and "schon einen Sticker" not in placed["message"]
        assert "2 Folgen sind fehlgeschlagen" in placed["then"]

    def test_when_every_episode_has_its_sticker_it_still_says_so(self, client, runner, teddycloud):
        group = start_placing(client, runner)
        for job in runner.all():
            assert job.done.wait(5)
        place_sticker(client, group, "deb4ed0e500304e0")
        place_sticker(client, group, "7ab4ed0e500304e0")
        teddycloud.add_tag("1111ed0e500304e0")

        placed = place_sticker(client, group, "1111ed0e500304e0")

        assert placed["status"] == "complete"
        assert placed["then"] == "Alle Folgen haben einen Sticker."


class TestSilentTonies:
    """A tonie whose audio was lost - a teddyCloud volume, a restore - is made again, not skipped."""

    def silent(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        teddycloud.files.pop("/" + entry["source"].split("lib://", 1)[1])
        return entry["tonie_model"], entry["source"]

    def test_the_batch_says_its_audio_is_made_again(self, client, runner, teddycloud):
        self.silent(client, runner, teddycloud)

        page = client.get(f"/batch?ids={first_album_id(client)}").text

        assert "Audio fehlt" in page

    def test_it_plays_again_as_the_same_tonie(self, client, runner, teddycloud):
        model, source = self.silent(client, runner, teddycloud)

        run_batch(client, runner, [first_album_id(client)])

        entry = teddycloud.tag_entries["7ab4ed0e500304e0"]
        assert (entry["tonie_model"], entry["source"]) == (model, source)
        assert "/" + source.split("lib://", 1)[1] in teddycloud.files
        assert [m["model"] for m in teddycloud.custom_models] == [model]


class TestTheDetailedPath:
    """One album edited in detail, from the batch, still walks the five steps."""

    def test_editing_in_detail_keeps_the_batch_open(self, client):
        # A link in the same tab drops whatever was typed in the other rows.
        page = client.get(f"/batch?ids={first_album_id(client)}").text

        assert re.search(r'<a href="/album/[^"]+" target="_blank"', page)

    def test_the_album_job_and_sticker_pages_show_where_one_is(self, client, runner):
        job, assign = encode_and_assign_page(client, runner)

        assert current_step(client.get(f"/album/{first_album_id(client)}").text) == "2"
        assert current_step(client.get(f"/job/{job.id}").text) == "3"
        assert current_step(assign.text) == "4"

    def test_putting_it_on_a_sticker_leads_to_that_tonie_and_its_cover(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        response = client.post(f"/job/{job.id}/assign", data={
            "ruid": "7ab4ed0e500304e0", "series": "Lichterkinder", "episode": "Halloween",
            "category": "music"}, follow_redirects=False)

        assert response.headers["location"] == "/tonies/7ab4ed0e500304e0"
