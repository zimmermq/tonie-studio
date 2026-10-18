"""Tests for the pages of the checkup, the backup and the restore."""

import threading

from web_helpers import encode_and_assign_page, first_album_id, publish_album

from tonie_studio.tags import list_tags


class TestCheckupPage:
    def test_a_healthy_setup_says_so(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = True

        assert "Alles in Ordnung" in client.get("/checkup").text

    def test_repairs_a_garbled_name_and_nothing_else(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        model = teddycloud.custom_models[0]
        model["series"] = "Bobo SiebenschlÃ¤fer"
        before = {key: value for key, value in model.items() if key != "series"}

        response = client.post("/checkup/fix", data={"fix": "repair_model", "target": "TS-0001"},
                               follow_redirects=False)

        repaired_model = teddycloud.custom_models[0]
        assert response.status_code == 303
        assert repaired_model["series"] == "Bobo Siebenschläfer"
        assert {key: value for key, value in repaired_model.items() if key != "series"} == before

    def test_switches_off_the_cloud_fallback(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid]["nocloud"] = False

        client.post("/checkup/fix", data={"fix": "nocloud", "target": ruid})

        assert teddycloud.tag_entries[ruid]["nocloud"] is True

    def test_switches_off_the_cloud_for_every_sticker_at_once(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.add_tag("11dfc517500304e0", source="lib://by/audioID/1.taf", nocloud=False)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = False

        page = client.get("/checkup").text
        response = client.post("/checkup/fix_all", data={"fix": "nocloud"},
                               follow_redirects=False)
        _finish_bulk(client, response)

        assert 'action="/checkup/fix_all"' in page
        assert response.status_code == 303
        assert all(entry["nocloud"] for entry in teddycloud.tag_entries.values()
                   if entry["source"])

    def test_answers_at_once_and_works_on_behind_it(self, client, runner, teddycloud):
        # Hundreds of stickers take longer than a gateway in front of the app
        # waits for an answer; the browser then saw an error for work that
        # went on and succeeded.
        publish_album(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = False
        writing = threading.Event()
        original = teddycloud.set_tag
        teddycloud.set_tag = lambda *a, **k: writing.wait(5) and original(*a, **k)

        response = client.post("/checkup/fix_all", data={"fix": "nocloud"},
                               follow_redirects=False)
        watching = client.get(response.headers["location"])

        assert response.headers["location"].startswith("/checkup?running=")
        assert 'http-equiv="refresh"' in watching.text
        writing.set()
        _finish_bulk(client, response)

    def test_a_finished_run_says_so(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = False

        response = client.post("/checkup/fix_all", data={"fix": "nocloud"},
                               follow_redirects=False)
        _finish_bulk(client, response)
        after = client.get(response.headers["location"], follow_redirects=False)

        assert after.status_code == 303 and after.headers["location"] == "/checkup?done=nocloud"

    def test_a_run_that_fails_says_why_on_the_checkup(self, client, runner, teddycloud):
        from tonie_studio.teddycloud import TeddyCloudError

        publish_album(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = False

        def refuse(*args, **kwargs):
            raise TeddyCloudError("teddyCloud said no")

        teddycloud.set_tag = refuse
        response = client.post("/checkup/fix_all", data={"fix": "nocloud"},
                               follow_redirects=False)
        _finish_bulk(client, response)
        page = client.get(response.headers["location"])

        assert page.status_code == 200 and "teddyCloud said no" in page.text
        assert 'http-equiv="refresh"' not in page.text

    def test_a_run_the_app_no_longer_knows_is_just_the_checkup(self, client):
        response = client.get("/checkup?running=gone", follow_redirects=False)

        assert response.headers["location"] == "/checkup"

    def test_only_a_harmless_repair_is_made_all_at_once(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        assert client.post("/checkup/fix_all", data={"fix": "delete_taf"}).status_code == 400

    def test_refuses_a_repair_that_is_no_longer_due(self, client, runner, teddycloud):
        # A page left open must not act on what it showed an hour ago.
        publish_album(client, runner, teddycloud)
        for entry in teddycloud.tag_entries.values():
            entry["nocloud"] = True
        played = next(e["source"] for e in teddycloud.tag_entries.values() if e["source"])
        path = "/" + played[len("lib://"):]

        response = client.post("/checkup/fix", data={"fix": "delete_taf", "target": path},
                               follow_redirects=False)

        assert response.headers["location"] == "/checkup?stale=1"
        assert path in teddycloud.files

    def test_empties_a_sticker_whose_taf_is_gone(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid].update(source="lib://Weg/Weg.taf", nocloud=True)

        page = client.get("/checkup").text
        client.post("/checkup/fix", data={"fix": "empty_sticker", "target": ruid})

        assert 'value="empty_sticker"' in page and "Leeren" in page
        assert teddycloud.tag_entries[ruid]["source"] == ""
        assert teddycloud.tag_entries[ruid]["tonie_model"] == ""
        assert ruid in [tag.ruid for tag in list_tags(teddycloud) if not tag.assigned]

    def test_offers_to_make_a_silent_tonie_again_first(self, client, runner, teddycloud):
        # Emptying loses the tonie; its album is still here to encode again.
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid].update(source="lib://Weg/Weg.taf", nocloud=True)
        album = first_album_id(client)

        page = client.get("/checkup").text

        rebuild = f'href="/batch?ids={album}"'
        assert rebuild in page
        assert page.index(rebuild) < page.index('value="empty_sticker"')

    def test_a_finding_about_a_sticker_leads_to_its_tonie(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid].update(source="lib://Weg/Weg.taf", nocloud=True)

        assert f'href="/tonies/{ruid}"' in client.get("/checkup").text

    def test_asks_before_emptying_a_sticker(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        ruid = next(r for r, e in teddycloud.tag_entries.items() if e["source"])
        teddycloud.tag_entries[ruid].update(source="lib://Weg/Weg.taf", nocloud=True)

        page = client.get("/checkup").text
        form = page[page.index('value="empty_sticker"') - 600:page.index('value="empty_sticker"')]

        assert "data-confirm=" in form

    def test_deletes_a_taf_nothing_plays(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.files["/Lichterkinder/Alt.taf"] = b"x"
        teddycloud.taf_headers["/Lichterkinder/Alt.taf"] = {"valid": True}

        client.post("/checkup/fix", data={"fix": "delete_taf", "target": "/Lichterkinder/Alt.taf"})

        assert "/Lichterkinder/Alt.taf" not in teddycloud.files

    def test_a_taf_nothing_plays_can_go_on_a_sticker_instead(self, client, runner, teddycloud):
        # "Maybe encoded ahead on purpose": then deleting is the wrong offer.
        publish_album(client, runner, teddycloud)
        teddycloud.files["/Lichterkinder/Alt.taf"] = b"x"
        teddycloud.taf_headers["/Lichterkinder/Alt.taf"] = {"valid": True}

        assert 'href="/library/assign?path=/Lichterkinder/Alt.taf"' in client.get("/checkup").text

    def test_an_unknown_repair_is_refused(self, client):
        response = client.post("/checkup/fix", data={"fix": "format_disk", "target": "/"},
                               follow_redirects=False)

        assert response.headers["location"] == "/checkup?stale=1"


class TestBackupPages:
    def test_warns_when_a_backup_would_be_too_large_to_restore(self, client, runner, teddycloud,
                                                                monkeypatch):
        publish_album(client, runner, teddycloud)
        monkeypatch.setattr("tonie_studio.backup.LARGEST_BACKUP", 10)

        assert 'class="warn"' in client.get("/backup").text

    def test_downloads_a_backup(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        response = client.get("/backup/download")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]

    def test_a_preview_writes_nothing(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()

        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text

        assert teddycloud.custom_models == []
        assert "Jetzt wiederherstellen" in page

    def test_confirming_restores_and_forgets_the_upload(self, client, runner, teddycloud, settings):
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        saved = [dict(model) for model in teddycloud.custom_models]
        teddycloud.custom_models.clear()

        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        _restore(client, token)

        assert teddycloud.custom_models == saved
        assert not (settings.data_dir / "restore" / f"{token}.zip").exists()

    def test_a_confirmation_cannot_be_replayed(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        _restore(client, token)

        assert client.post(f"/restore/{token}").status_code == 404

    def test_the_result_is_a_page_of_its_own_that_survives_a_reload(self, client, runner,
                                                                    teddycloud):
        # A result that was the answer to the confirmation would, reloaded,
        # send the confirmation again and say the restore had expired.
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]

        response = client.post(f"/restore/{token}", follow_redirects=False)
        done = response.headers["location"]
        _wait_for_restore(client, token)

        assert response.status_code == 303
        assert client.get(done).status_code == 200
        assert client.get(done).text == client.get(done).text

    def test_tonies_left_silent_can_be_made_again_from_the_result(self, client, runner,
                                                                  teddycloud):
        # A backup holds no audio: after a lost volume the stickers come back
        # pointing at tafs that are gone.
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        source = teddycloud.tag_entries["7ab4ed0e500304e0"]["source"]
        teddycloud.files.pop("/" + source.split("lib://", 1)[1])
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]

        done = _restore(client, token).text

        assert 'href="/batch?ids=' in done

    def test_the_preview_offers_to_save_what_is_here_first(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()

        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text

        assert 'href="/backup/download"' in page

    def test_teddycloud_away_while_restoring_lets_one_try_again(self, client, runner, teddycloud):
        # The uploaded file is kept until the restore went through; otherwise
        # an outage means uploading the backup again.
        from unittest.mock import patch

        from tonie_studio.teddycloud import TeddyCloudError

        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]

        with patch("tonie_studio.pages.upkeep.apply_restore",
                   side_effect=TeddyCloudError("unreachable")):
            failed = _restore(client, token)

        assert "unreachable" in failed.text and f'action="/restore/{token}"' in failed.text
        assert 'http-equiv="refresh"' not in failed.text
        _restore(client, token)
        assert teddycloud.custom_models

    def test_confirming_answers_at_once_and_restores_behind_it(self, client, runner, teddycloud):
        # Hundreds of stickers take longer than a gateway in front of the app
        # waits; the result must not depend on the confirmation's answer.
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        writing = threading.Event()
        original = teddycloud.upsert_custom_tonie
        teddycloud.upsert_custom_tonie = lambda model: writing.wait(5) and original(model)

        response = client.post(f"/restore/{token}", follow_redirects=False)
        watching = client.get(response.headers["location"])
        again = client.post(f"/restore/{token}", follow_redirects=False)

        assert response.headers["location"] == f"/restore/{token}/done"
        assert 'http-equiv="refresh"' in watching.text
        assert again.status_code == 303
        writing.set()
        _wait_for_restore(client, token)
        assert teddycloud.custom_models
        assert 'http-equiv="refresh"' not in client.get(f"/restore/{token}/done").text

    def test_refuses_a_file_that_is_not_a_backup(self, client):
        response = client.post("/restore", files={"file": ("x.zip", b"nope", "application/zip")})

        assert response.status_code == 400

    def test_refuses_a_token_that_is_a_path(self, client):
        assert client.post("/restore/..%2F..%2Fetc").status_code in (400, 404)


class TestRestoreLimits:
    def test_a_huge_upload_is_refused(self, client, monkeypatch):
        import tonie_studio.backup as backup
        import tonie_studio.pages.upkeep as upkeep

        monkeypatch.setattr(backup, "LARGEST_BACKUP", 1000)
        monkeypatch.setattr(upkeep, "LARGEST_BACKUP", 1000)

        response = client.post("/restore", files={"file": ("b.zip", b"x" * 5000, "application/zip")})

        assert response.status_code == 400

    def test_a_backup_without_a_date_still_shows(self, client, runner, teddycloud):
        import io
        import json
        import zipfile

        publish_album(client, runner, teddycloud)
        original = zipfile.ZipFile(io.BytesIO(client.get("/backup/download").content))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as rebuilt:
            for name in original.namelist():
                data = original.read(name)
                if name == "manifest.json":
                    manifest = json.loads(data)
                    del manifest["created"]
                    data = json.dumps(manifest).encode()
                rebuilt.writestr(name, data)

        response = client.post("/restore", files={"file": ("b.zip", buffer.getvalue(), "application/zip")})

        assert response.status_code == 200


class TestCheckupSparesWork:
    def test_an_encoded_but_unassigned_taf_is_not_offered_for_deletion(self, client, runner):
        job, _page = encode_and_assign_page(client, runner)

        page = client.get("/checkup").text

        assert job.label not in page


class TestBackupPageSizes:
    def test_lists_the_parts_and_their_total(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)

        page = client.get("/backup").text

        for part in ("Modelle", "Sticker", "Cover", "Spielzeiten", "Hörprotokoll", "zusammen etwa"):
            assert part in page

    def test_no_fixed_size_promise_is_left(self, client):
        assert "Megabyte" not in client.get("/backup").text


def _finish_bulk(client, response):
    """Wait for the run a fix_all answer points at."""
    job_id = response.headers["location"].split("running=")[1]
    assert client.app.state.prints.get(job_id).done.wait(10)


def _wait_for_restore(client, token):
    """Wait for the restore run of ``token``."""
    runs = [job for job in client.app.state.prints.all()
            if job.kind == "restore" and job.label == token]
    assert runs and all(job.done.wait(10) for job in runs)


def _restore(client, token):
    """Confirm a restore, wait for it, and answer its result page."""
    response = client.post(f"/restore/{token}", follow_redirects=False)
    assert response.status_code == 303
    _wait_for_restore(client, token)
    return client.get(response.headers["location"])
