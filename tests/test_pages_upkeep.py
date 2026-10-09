"""Tests for the pages of the checkup, the backup and the restore."""



from web_helpers import encode_and_assign_page, publish_album


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

    def test_deletes_a_taf_nothing_plays(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        teddycloud.files["/Lichterkinder/Alt.taf"] = b"x"
        teddycloud.taf_headers["/Lichterkinder/Alt.taf"] = {"valid": True}

        client.post("/checkup/fix", data={"fix": "delete_taf", "target": "/Lichterkinder/Alt.taf"})

        assert "/Lichterkinder/Alt.taf" not in teddycloud.files

    def test_an_unknown_repair_is_refused(self, client):
        response = client.post("/checkup/fix", data={"fix": "format_disk", "target": "/"},
                               follow_redirects=False)

        assert response.headers["location"] == "/checkup?stale=1"


class TestBackupPages:
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
        client.post(f"/restore/{token}")

        assert teddycloud.custom_models == saved
        assert not (settings.data_dir / "restore" / f"{token}.zip").exists()

    def test_a_confirmation_cannot_be_replayed(self, client, runner, teddycloud):
        publish_album(client, runner, teddycloud)
        data = client.get("/backup/download").content
        teddycloud.custom_models.clear()
        page = client.post("/restore", files={"file": ("b.zip", data, "application/zip")}).text
        token = page.split('action="/restore/', 1)[1].split('"', 1)[0]
        client.post(f"/restore/{token}")

        assert client.post(f"/restore/{token}").status_code == 404

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
