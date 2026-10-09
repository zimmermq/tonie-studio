"""Tests for the teddyCloud API client.

The client's whole job is producing exactly the request teddyCloud expects, so
the tests assert on the request rather than on a parsed result: which verb,
which path, which query parameters, and above all how the body is encoded -
teddyCloud mixes urlencoded forms, bare text bodies and JSON across endpoints,
and gets a wrong encoding silently wrong.
"""

from unittest.mock import Mock, patch

import pytest

from tonie_studio.teddycloud import TeddyCloudClient, TeddyCloudError


def _ok():
    """A response the way teddyCloud answers a successful command endpoint."""
    response = Mock()
    response.status_code = 200
    response.text = "OK"
    return response


def _json(payload):
    """A response carrying a JSON document."""
    response = Mock()
    response.status_code = 200
    response.text = ""
    response.json.return_value = payload
    return response


@pytest.fixture
def client():
    return TeddyCloudClient("http://teddycloud.test")


@pytest.fixture
def request_mock(client):
    with patch.object(client, "_request") as mock:
        yield mock


class TestFromEnv:
    def test_builds_a_client_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("TS_TEDDYCLOUD_URL", "http://teddy:8080/")

        client = TeddyCloudClient.from_env()

        assert client.base_url == "http://teddy:8080"

    def test_names_the_missing_variable(self, monkeypatch):
        monkeypatch.delenv("TS_TEDDYCLOUD_URL", raising=False)

        with pytest.raises(TeddyCloudError) as excinfo:
            TeddyCloudClient.from_env()

        assert "TS_TEDDYCLOUD_URL" in str(excinfo.value)


class TestListDir:
    def test_returns_the_file_entries(self, client, request_mock):
        request_mock.return_value = _json({"files": [{"name": "a.taf", "isDir": False}]})

        entries = client.list_dir("/Lichterkinder", special="library")

        assert entries == [{"name": "a.taf", "isDir": False}]

    def test_asks_for_the_requested_path_and_root(self, client, request_mock):
        request_mock.return_value = _json({"files": []})

        client.list_dir("/Lichterkinder", special="library")

        method, path = request_mock.call_args.args
        assert (method, path) == ("GET", "/api/fileIndexV2")
        assert request_mock.call_args.kwargs["params"] == {
            "path": "/Lichterkinder", "special": "library",
        }

    def test_omits_special_for_the_content_tree(self, client, request_mock):
        request_mock.return_value = _json({"files": []})

        client.list_dir("/")

        assert request_mock.call_args.kwargs["params"] == {"path": "/"}

    def test_tolerates_a_response_without_the_files_key(self, client, request_mock):
        request_mock.return_value = _json({})

        assert client.list_dir("/") == []


class TestCreateDir:
    def test_sends_the_full_path_as_a_text_body(self, client, request_mock):
        request_mock.return_value = _ok()

        client.create_dir("/_staging/job-1", special="library")

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/dirCreate")
        assert request_mock.call_args.kwargs["params"] == {"special": "library"}
        assert request_mock.call_args.kwargs["data"] == "/_staging/job-1"

    def test_raises_when_teddycloud_answers_with_a_message(self, client, request_mock):
        # The command endpoints answer 200 with an error string, so the status
        # code alone never shows that a call failed.
        response = _ok()
        response.text = "ERROR: directory exists"
        request_mock.return_value = response

        with pytest.raises(TeddyCloudError) as excinfo:
            client.create_dir("/_staging/job-1", special="library")

        assert "directory exists" in str(excinfo.value)


class TestDelete:
    def test_sends_the_path_as_a_text_body(self, client, request_mock):
        request_mock.return_value = _ok()

        client.delete("/_staging/job-1/01.flac", special="library")

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/fileDelete")
        assert request_mock.call_args.kwargs["data"] == "/_staging/job-1/01.flac"


class TestUpload:
    def test_posts_the_file_under_the_given_name(self, client, request_mock):
        request_mock.return_value = _ok()
        stream = Mock()

        client.upload(stream, "01-track.flac", path="/_staging/job-1", special="library")

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/fileUpload")
        assert request_mock.call_args.kwargs["params"] == {
            "path": "/_staging/job-1", "special": "library",
        }
        assert request_mock.call_args.kwargs["files"] == {"file": ("01-track.flac", stream)}

    def test_rejects_a_name_containing_a_path_separator(self, client, request_mock):
        # teddyCloud stores the multipart filename verbatim and refuses
        # separators; catching it here gives a better message than a 500.
        with pytest.raises(ValueError):
            client.upload(Mock(), "sub/01-track.flac", path="/", special="library")

        request_mock.assert_not_called()


class TestEncode:
    def test_keeps_the_source_order_because_each_source_is_a_chapter(self, client, request_mock):
        request_mock.return_value = _ok()

        client.encode(
            "/Lichterkinder/Gemeinsam.taf",
            ["/_staging/job-1/01.flac", "/_staging/job-1/02.flac"],
        )

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/fileEncode")
        assert request_mock.call_args.kwargs["data"] == [
            ("source", "/_staging/job-1/01.flac"),
            ("source", "/_staging/job-1/02.flac"),
            ("target", "/Lichterkinder/Gemeinsam.taf"),
        ]

    def test_encodes_within_the_library_by_default(self, client, request_mock):
        request_mock.return_value = _ok()

        client.encode("/a.taf", ["/b.flac"])

        assert request_mock.call_args.kwargs["params"] == {"special": "library"}

    def test_allows_more_time_than_an_ordinary_call(self, client, request_mock):
        request_mock.return_value = _ok()

        client.encode("/a.taf", ["/b.flac"])

        assert request_mock.call_args.kwargs["timeout"] > client.timeout

    def test_refuses_an_empty_source_list(self, client, request_mock):
        with pytest.raises(ValueError):
            client.encode("/a.taf", [])

        request_mock.assert_not_called()


class TestFetch:
    def test_returns_the_bytes_teddycloud_serves(self, client, request_mock):
        response = Mock()
        response.status_code = 200
        response.text = ""
        response.content = b"\xff\xd8\xff"
        request_mock.return_value = response

        assert client.fetch("/custom_img/TS-0001.jpg") == b"\xff\xd8\xff"
        assert request_mock.call_args.args == ("GET", "/custom_img/TS-0001.jpg")


class TestTags:
    def test_returns_the_tag_entries(self, client, request_mock):
        request_mock.return_value = _json({"tags": [{"ruid": "7ab4ed0e500304e0"}]})

        assert client.tags() == [{"ruid": "7ab4ed0e500304e0"}]

    def test_reads_the_tag_index(self, client, request_mock):
        request_mock.return_value = _json({"tags": []})

        client.tags()

        assert request_mock.call_args.args == ("GET", "/api/getTagIndex")


class TestSetTag:
    def test_posts_the_fields_to_the_tag_of_that_ruid(self, client, request_mock):
        request_mock.return_value = _ok()

        client.set_tag("7ab4ed0e500304e0", source="lib://Serie/Folge.taf")

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/content/json/set/7ab4ed0e500304e0")
        assert request_mock.call_args.kwargs["data"] == {"source": "lib://Serie/Folge.taf"}

    def test_spells_booleans_the_way_teddycloud_reads_them(self, client, request_mock):
        # Anything but the literal "true" counts as false on the server side.
        request_mock.return_value = _ok()

        client.set_tag("7ab4ed0e500304e0", nocloud=True, hide=False)

        assert request_mock.call_args.kwargs["data"] == {"nocloud": "true", "hide": "false"}

    def test_refuses_a_ruid_that_is_not_sixteen_hex_digits(self, client, request_mock):
        with pytest.raises(ValueError):
            client.set_tag("7AB4ED0E", source="lib://a.taf")

        request_mock.assert_not_called()

    def test_refuses_a_call_that_would_change_nothing(self, client, request_mock):
        with pytest.raises(ValueError):
            client.set_tag("7ab4ed0e500304e0")

        request_mock.assert_not_called()


class TestCustomTonies:
    def test_returns_the_model_list(self, client, request_mock):
        request_mock.return_value = _json([{"model": "TRV_0001"}])

        assert client.custom_tonies() == [{"model": "TRV_0001"}]

    def test_upserts_a_model_as_json(self, client, request_mock):
        request_mock.return_value = _ok()
        model = {"model": "TRV_0001", "series": "Lichterkinder"}

        client.upsert_custom_tonie(model)

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/toniesCustomJsonUpsert")
        assert request_mock.call_args.kwargs["json"] == model

    def test_refuses_a_model_without_the_fields_the_server_requires(self, client, request_mock):
        with pytest.raises(ValueError):
            client.upsert_custom_tonie({"model": "TRV_0001"})

        request_mock.assert_not_called()

    def test_deletes_models_by_name(self, client, request_mock):
        request_mock.return_value = _ok()

        client.delete_custom_tonies(["TRV_0001", "TRV_0002"])

        method, path = request_mock.call_args.args
        assert (method, path) == ("POST", "/api/toniesCustomJsonDelete")
        assert request_mock.call_args.kwargs["json"] == {"models": ["TRV_0001", "TRV_0002"]}


class TestOverlay:
    def test_adds_the_overlay_to_every_call_when_one_is_configured(self):
        client = TeddyCloudClient("http://teddycloud.test", overlay="abc123")

        with patch.object(client, "_request", return_value=_json({"tags": []})) as request:
            client.tags()

        assert request.call_args.kwargs["params"] == {"overlay": "abc123"}

    def test_leaves_it_out_when_none_is_configured(self, client, request_mock):
        request_mock.return_value = _json({"tags": []})

        client.tags()

        assert request_mock.call_args.kwargs["params"] == {}


class TestEncoding:
    def test_reads_umlauts_as_utf8_whatever_the_header_says(self, client):
        # teddyCloud answers without a charset, and requests then falls back to
        # the HTTP default of latin-1, which turns every umlaut in a file name
        # or a series into mojibake. The library is full of them.
        import requests

        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/plain"
        response._content = '{"files":[{"name":"Bobo Siebenschl\u00e4fer.taf"}]}'.encode("utf-8")

        with patch.object(client.session, "request", return_value=response):
            entries = client.list_dir("/", special="library")

        assert entries[0]["name"] == "Bobo Siebenschl\u00e4fer.taf"

    def test_keeps_binary_answers_untouched(self, client):
        import requests

        response = requests.Response()
        response.status_code = 200
        response._content = b"\xff\xd8\xff"

        with patch.object(client.session, "request", return_value=response):
            assert client.fetch("/custom_img/a.jpg") == b"\xff\xd8\xff"


class TestRequest:
    def test_turns_a_transport_failure_into_a_teddycloud_error(self, client):
        import requests

        with patch.object(client.session, "request", side_effect=requests.ConnectionError("boom")):
            with pytest.raises(TeddyCloudError) as excinfo:
                client.tags()

        assert "http://teddycloud.test" in str(excinfo.value)

    def test_turns_an_http_error_status_into_a_teddycloud_error(self, client):
        response = Mock()
        response.status_code = 500
        response.text = "Internal error"

        with patch.object(client.session, "request", return_value=response):
            with pytest.raises(TeddyCloudError) as excinfo:
                client.tags()

        assert "500" in str(excinfo.value)

    def test_builds_the_url_from_the_base_and_the_path(self, client):
        response = _json({"tags": []})

        with patch.object(client.session, "request", return_value=response) as request:
            client.tags()

        assert request.call_args.args == ("GET", "http://teddycloud.test/api/getTagIndex")


class TestRetries:
    @pytest.mark.parametrize("scheme", ["http://", "https://"])
    def test_reads_are_retried_whichever_scheme_teddycloud_is_reached_by(self, client, scheme):
        retries = client.session.get_adapter(scheme + "teddycloud.test").max_retries

        assert retries.total == 2 and "GET" in retries.allowed_methods

    def test_writes_are_never_retried(self, client):
        retries = client.session.get_adapter("https://teddycloud.test").max_retries

        assert "POST" not in retries.allowed_methods


class TestTwin:
    def test_a_twin_talks_to_the_same_teddycloud_over_its_own_session(self):
        original = TeddyCloudClient("https://teddycloud.test/", overlay="abc", timeout=7)

        twin = original.twin()

        assert (twin.base_url, twin.overlay, twin.timeout) == ("https://teddycloud.test", "abc", 7)
        assert twin.session is not original.session
