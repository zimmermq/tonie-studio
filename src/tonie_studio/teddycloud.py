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

import logging
import os
import re
from typing import Any, BinaryIO, Dict, Iterable, List, Optional

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

# A tag is addressed by its reversed UID: 16 hex digits, lower case as
# teddyCloud writes them into the content tree.
RUID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

# teddyCloud's answer to a command that did what it was told.
COMMAND_OK = "OK"


class TeddyCloudError(RuntimeError):
    """Raised when teddyCloud cannot be reached or refuses a call."""


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
        self.session.mount(
            "http://",
            HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=1, status_forcelist=[502, 503, 504],
                allowed_methods=["GET"],
            )),
        )

    @classmethod
    def from_env(cls) -> "TeddyCloudClient":
        """Build a client from TS_TEDDYCLOUD_URL and the optional TS_TEDDYCLOUD_OVERLAY."""
        base_url = os.environ.get("TS_TEDDYCLOUD_URL")
        if not base_url:
            raise TeddyCloudError("TS_TEDDYCLOUD_URL is not set")
        return cls(base_url, overlay=os.environ.get("TS_TEDDYCLOUD_OVERLAY") or None)

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

        response = self._request(
            "POST",
            "/api/fileUpload",
            params=self._params(path=path, special=special),
            files={"file": (filename, stream)},
            timeout=ENCODE_TIMEOUT,
        )
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

        if not 200 <= response.status_code < 300:
            raise TeddyCloudError(
                f"teddyCloud answered {response.status_code} for {path}: {response.text[:200]}"
            )
        return response

    @staticmethod
    def _expect_ok(response: requests.Response, path: str) -> None:
        """Check the body of a command endpoint, which reports failure at 200."""
        body = (response.text or "").strip()
        if body != COMMAND_OK:
            raise TeddyCloudError(f"teddyCloud refused {path}: {body[:200] or 'empty answer'}")


def _form_value(value: Any) -> str:
    """Render a field value the way teddyCloud parses it.

    Booleans are read as the literal string ``true``; every other text counts
    as false, so the false case has to be spelled out rather than omitted.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
