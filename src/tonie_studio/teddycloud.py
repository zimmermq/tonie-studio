"""Client for the teddyCloud management API.

teddyCloud exposes no OpenAPI description; the endpoints below were read off
the running instance and confirmed against the server sources at tag tc_v0.7.0
(``src/server.c`` routes, ``src/handler_api.c`` handlers). Three body encodings
are in use and none of them is negotiable:

- ``/api/dirCreate`` and ``/api/fileDelete`` take the bare path as the whole
  request body and answer ``200`` with the literal text ``OK``, or ``200`` with
  an error message. A caller that only checks the status code never sees a
  failure, so every command response is checked against ``OK`` here.
- ``/api/fileEncode`` and ``/content/json/set/<ruid>`` take urlencoded forms.
- the ``toniesCustomJson*`` endpoints take JSON.

Two query parameters steer which directory tree an endpoint works on.
``special=library`` selects the audio library, ``special=custom_img`` the cover
images served under ``/custom_img/``, and omitting it selects the content tree
that holds one ``<CN>/<UID>.json`` per known NFC tag. ``overlay=<id>`` selects a
per-box configuration; without it teddyCloud uses its default instance, which
is what a single-box setup wants.

The API is unauthenticated. teddyCloud's own auth endpoints are stubs that no
other handler enforces, so anything that can reach the port can write. Keep the
service off any untrusted network.
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# Long enough for a NAS that is spinning up, short enough to fail a request
# while someone is watching the page it belongs to.
REQUEST_TIMEOUT = 30

# ffmpeg decodes and re-encodes every track in one blocking request, so a long
# album legitimately takes minutes. teddyCloud reports no progress meanwhile.
ENCODE_TIMEOUT = 1800

# How long, in seconds, a taf download may go quiet before it counts as lost.
# teddyCloud passes the taf on from the Tonies cloud as it arrives and caches it
# on the way, so the whole download takes as long as the cloud needs; only a
# stall should end it. Generous, because the cloud is slow to start a large one.
DOWNLOAD_READ_TIMEOUT = 300

# How much of a downloaded taf is read at a time; it is counted and dropped.
DOWNLOAD_CHUNK = 1 << 20

# How often one file is sent when the connection drops before teddyCloud
# answers. Twice covers a connection closed under it; more would only delay
# the failure of a teddyCloud that is really gone.
UPLOAD_ATTEMPTS = 2

# How long, in seconds, an upload's header goes ahead of its body.
# teddyCloud 0.7.0 reads the request header line by line from a 32 KB buffer.
# When its first read fills that buffer with header and body together, it
# takes the whole buffer for the request line, never finds the end of the
# header, and closes the connection without an answer. That happens whenever
# the body has arrived before teddyCloud gets to read, which under load is
# often. Sent alone first, the header is read on its own. Costs this long per
# track; harmless against a teddyCloud without the bug.
# The bug is fixed on teddyCloud's develop branch but in no release yet
# (teddycloud#543). Once every supported teddyCloud has the fix, HEAD_START
# and _AfterHeadStart can go; until then, removing them loses uploads.
HEAD_START = 0.05

# A tag is addressed by its reversed UID: 16 hex digits, lower case as
# teddyCloud writes them into the content tree.
RUID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

# teddyCloud's answer to a command that did what it was told.
COMMAND_OK = "OK"


class TeddyCloudError(RuntimeError):
    """Raised when teddyCloud cannot be reached or refuses a call.

    ``status`` is the HTTP status teddyCloud answered with, or ``None`` when it
    did not answer at all.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class TeddyCloudClient:
    """Thin request builder over the teddyCloud management API."""

    def __init__(
        self,
        base_url: str,
        overlay: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.overlay = overlay
        self.timeout = timeout

        self.session = requests.Session()
        # Only idempotent verbs are retried: a repeated fileEncode would write
        # a second taf, and a repeated upload a second copy.
        retrying = HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=1, status_forcelist=[502, 503, 504],
            allowed_methods=["GET"],
        ))
        self.session.mount("http://", retrying)
        self.session.mount("https://", retrying)
        # Downloads from the Tonies cloud go over a session that never retries:
        # a repeated request would make teddyCloud fetch the whole taf again.
        self.downloads = requests.Session()

    def twin(self) -> "TeddyCloudClient":
        """A client for the same teddyCloud with a session of its own, for another thread."""
        return TeddyCloudClient(self.base_url, overlay=self.overlay, timeout=self.timeout)

    # --- directories and files -------------------------------------------

    def list_dir(self, path: str = "/", special: Optional[str] = None) -> List[Dict[str, Any]]:
        """List one directory of the library, the images or the content tree.

        Entries carry ``name``, ``isDir``, ``size``, ``date``, and for a taf a
        ``tafHeader`` with the ``audioId`` and ``sha1Hash`` that identify its
        audio to teddyCloud.
        """
        response = self._request(
            "GET", "/api/fileIndexV2", params=self._params(path=path, special=special),
        )
        return response.json().get("files", [])

    def create_dir(self, path: str, special: Optional[str] = None) -> None:
        """Create one directory, addressed by its full path from the tree root."""
        self._command("/api/dirCreate", path, special=special)

    def delete(self, path: str, special: Optional[str] = None) -> None:
        """Delete one file or empty directory."""
        self._command("/api/fileDelete", path, special=special)

    def upload(
        self,
        stream: BinaryIO,
        filename: str,
        path: str = "/",
        special: Optional[str] = None,
    ) -> None:
        """Store a stream as ``filename`` inside ``path``.

        teddyCloud takes the multipart filename verbatim and rejects one that
        contains a separator, so the name has to be a plain basename.
        """
        if "/" in filename or "\\" in filename:
            raise ValueError(f"upload name must not contain a path separator: {filename!r}")

        # The body follows the header only after HEAD_START, on a connection of
        # its own. A connection that still drops is tried once more with the
        # whole file; the staged name is simply written again. An answer from
        # teddyCloud, even a refusal, is never repeated.
        start = stream.tell()
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            stream.seek(start)
            body, content_type = _multipart(filename, stream)
            try:
                response = self._request(
                    "POST",
                    "/api/fileUpload",
                    params=self._params(path=path, special=special),
                    data=_AfterHeadStart(body),
                    headers={"Content-Type": content_type, "Connection": "close"},
                    timeout=ENCODE_TIMEOUT,
                )
                break
            except TeddyCloudError as error:
                dropped = isinstance(error.__cause__, requests.exceptions.ConnectionError)
                if not dropped or attempt == UPLOAD_ATTEMPTS:
                    raise
                log.warning("upload of %s dropped, trying again: %s", filename, error)
        self._expect_ok(response, "/api/fileUpload")

    def encode(
        self,
        target: str,
        sources: Iterable[str],
        special: str = "library",
    ) -> None:
        """Convert the source files into one taf at ``target``.

        Every source becomes its own chapter, in the order given, which is what
        the Toniebox skips between. The sources must already lie inside the
        selected tree; teddyCloud reads them from its own disk rather than from
        this request. It refuses a target that exists.
        """
        sources = list(sources)
        if not sources:
            raise ValueError("an encode needs at least one source file")

        response = self._request(
            "POST",
            "/api/fileEncode",
            params=self._params(special=special),
            data=[("source", source) for source in sources] + [("target", target)],
            timeout=ENCODE_TIMEOUT,
        )
        self._expect_ok(response, "/api/fileEncode")

    def fetch(self, path: str) -> bytes:
        """Read a file teddyCloud serves, such as a cover under /custom_img/.

        Used to pass images on to a browser that can reach this app but not
        teddyCloud, which is the normal case behind an authenticating gateway.
        """
        return self._request("GET", path).content

    # --- tags -------------------------------------------------------------

    def tags(self) -> List[Dict[str, Any]]:
        """List every NFC tag teddyCloud knows.

        A tag appears here only once the box has read it, so a fresh sticker is
        absent until it has been held against the box at least once.
        """
        response = self._request("GET", "/api/getTagIndex", params=self._params())
        return response.json().get("tags", [])

    def set_tag(self, ruid: str, **fields: Any) -> None:
        """Change fields of one tag's content entry.

        Accepts ``source`` (``lib://`` path of the taf to play), ``tonie_model``
        (the ``model`` of a tonies.json or tonies.custom.json entry, which
        supplies the cover and track names shown in teddyCloud), plus the flags
        ``live``, ``nocloud``, ``hide`` and ``claimed``. Fields left out keep
        their value.
        """
        if not RUID_PATTERN.match(ruid):
            raise ValueError(f"not a tag ruid: {ruid!r}")
        if not fields:
            raise ValueError("set_tag needs at least one field to change")

        response = self._request(
            "POST",
            f"/content/json/set/{ruid}",
            params=self._params(),
            data={key: _form_value(value) for key, value in fields.items()},
        )
        self._expect_ok(response, f"/content/json/set/{ruid}")

    def content_entry(self, ruid: str) -> Dict[str, Any]:
        """Read one tag's content JSON, such as its ``source`` and ``cloud_auth``.

        A tag whose content folder does not exist answers 404 and gives an
        empty entry here. One whose folder exists but whose JSON does not comes
        back with empty fields, so an empty ``source`` is all that tells "not
        cached" apart; whether the JSON itself exists needs :meth:`list_dir`.
        """
        if not RUID_PATTERN.match(ruid):
            raise ValueError(f"not a tag ruid: {ruid!r}")
        try:
            response = self._request("GET", f"/content/json/get/{ruid}", params=self._params())
        except TeddyCloudError as error:
            if error.status == 404:
                return {}
            raise
        return response.json()

    def cache_content(self, folder: str, file: str) -> int:
        """Have teddyCloud fetch one tag's taf from the Tonies cloud; return its size.

        teddyCloud reads ``cloud_ruid`` and ``cloud_auth`` from the content JSON
        at ``<folder>/<file>.json``, asks the cloud for the audio and passes it
        on while writing it to its cache, then, with ``cloud.cacheToLibrary``,
        moves it into the library and points the entry's ``source`` at it. The
        taf is read to the end and dropped, because teddyCloud only finishes
        the cache once the whole file has gone through.
        """
        url = f"{self.base_url}/content/download/{folder}/{file}.json"
        size = 0
        try:
            with self.downloads.get(url, params=self._params(), stream=True,
                                    timeout=(self.timeout, DOWNLOAD_READ_TIMEOUT)) as response:
                if not 200 <= response.status_code < 300:
                    raise TeddyCloudError(
                        f"teddyCloud answered {response.status_code} for the download of "
                        f"{folder}/{file}: {response.text[:200]}",
                        status=response.status_code,
                    )
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
                    size += len(chunk)
        except requests.RequestException as error:
            raise TeddyCloudError(
                f"download of {folder}/{file} from teddyCloud broke off after {size} bytes: {error}"
            ) from error
        return size

    # --- settings ---------------------------------------------------------

    def setting(self, name: str) -> str:
        """One setting's value as teddyCloud spells it, such as ``"true"``."""
        return self._request("GET", f"/api/settings/get/{name}", params=self._params()).text.strip()

    def tonies_json(self) -> List[Dict[str, Any]]:
        """The official catalogue teddyCloud keeps up to date: every tonie it can name."""
        return self._request("GET", "/api/toniesJson", params=self._params()).json()

    # --- custom tonie models ---------------------------------------------

    def custom_tonies(self) -> List[Dict[str, Any]]:
        """Read tonies.custom.json, the models added next to the shipped ones."""
        response = self._request("GET", "/api/toniesCustomJson", params=self._params())
        return response.json()

    def upsert_custom_tonie(self, model: Dict[str, Any]) -> None:
        """Create or replace one custom model, matched on its ``model`` field.

        ``model`` is a free-form identifier chosen by the caller; teddyCloud
        numbers nothing by itself. Reusing an id from the shipped tonies.json
        would shadow a real Tonie, so pick a namespace of your own.

        Filling ``audio_id`` and ``hash`` from a taf's header makes teddyCloud
        attach this model to any tag playing that taf on its own.
        """
        missing = [field for field in ("model", "series") if not model.get(field)]
        if missing:
            raise ValueError(f"custom model needs {' and '.join(missing)}")

        response = self._request(
            "POST", "/api/toniesCustomJsonUpsert", params=self._params(), json=model,
        )
        self._expect_ok(response, "/api/toniesCustomJsonUpsert")

    def delete_custom_tonies(self, models: Iterable[str]) -> None:
        """Remove custom models by id. Tags referencing them keep their audio."""
        response = self._request(
            "POST",
            "/api/toniesCustomJsonDelete",
            params=self._params(),
            json={"models": list(models)},
        )
        self._expect_ok(response, "/api/toniesCustomJsonDelete")

    # --- plumbing ---------------------------------------------------------

    def _params(self, **params: Optional[str]) -> Dict[str, str]:
        """Drop unset parameters and add the overlay, if one is configured."""
        out = {key: value for key, value in params.items() if value is not None}
        if self.overlay:
            out["overlay"] = self.overlay
        return out

    def _command(self, path: str, body: str, special: Optional[str]) -> None:
        """Call an endpoint whose whole request body is one path."""
        response = self._request(
            "POST", path, params=self._params(special=special), data=body,
        )
        self._expect_ok(response, path)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as error:
            raise TeddyCloudError(f"teddyCloud at {self.base_url} is unreachable: {error}") from error

        # teddyCloud serves UTF-8 throughout but declares no charset, and
        # requests then falls back to the HTTP default of latin-1. Every umlaut
        # in a file name, a series or an episode would arrive as mojibake, and
        # a path built from it would no longer match the file on disk.
        response.encoding = "utf-8"

        if not 200 <= response.status_code < 300:
            raise TeddyCloudError(
                f"teddyCloud answered {response.status_code} for {path}: {response.text[:200]}",
                status=response.status_code,
            )
        return response

    @staticmethod
    def _expect_ok(response: requests.Response, path: str) -> None:
        """Check the body of a command endpoint, which reports failure at 200."""
        body = (response.text or "").strip()
        if body != COMMAND_OK:
            raise TeddyCloudError(f"teddyCloud refused {path}: {body[:200] or 'empty answer'}")


def _multipart(filename: str, stream: BinaryIO) -> Tuple[bytes, str]:
    """The multipart body of one file upload and its content type, as requests builds it."""
    prepared = requests.Request("POST", "http://teddycloud.invalid/",
                                files={"file": (filename, stream)}).prepare()
    return prepared.body, prepared.headers["Content-Type"]


class _AfterHeadStart:
    """A request body that holds its first byte back for HEAD_START.

    File-like and with a length, so requests sends it with a Content-Length
    rather than in chunks.
    """

    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)
        self._length = len(body)
        self._started = False

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        if not self._started:
            self._started = True
            time.sleep(HEAD_START)
        return self._body.read(size)


def _form_value(value: Any) -> str:
    """Render a field value the way teddyCloud parses it.

    Booleans are read as the literal string ``true``; every other text counts
    as false, so the false case has to be spelled out rather than omitted.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
