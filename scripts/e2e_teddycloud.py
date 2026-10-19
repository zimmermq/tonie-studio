#!/usr/bin/env python3
"""Run the app against a real teddyCloud in Docker and check what it really wrote.

The unit tests talk to tests/fake_teddycloud.py, which keeps teddyCloud's
rules but not its HTTP server. Bugs in how requests reach teddyCloud - an
upload body split so that teddyCloud ends the file early, a form encoded the
way the fake accepts and the server does not - only show against the real
thing. This script starts teddyCloud 0.7.0 in a container and the app on the
host, walks the app's pages the way a browser does, and after every flow:

- reads teddyCloud's log for "Json parse error", "runtime error", ERROR lines
  (a listing of a folder that does not exist is one), and every upload that
  started ("file_save_start ... Writing to") without the matching
  "multipart_handle ... Received file";
- reads the app's log for WARNING, ERROR, tracebacks and unexpected 4xx/5xx
  answers, its own and teddyCloud's (the app is started through a small
  wrapper that logs every teddyCloud call it makes);
- looks in teddyCloud itself - tag entries, the library, tonies.custom.json,
  the files in custom_img and the image cache - for what the flow should have
  left there.

Every flow runs twice: as is, and with the upload body slowed down (20-50 ms
after every piece the app hands to the socket), which is closer to what a
real network does to the timing. A flow builds on what the ones before it
made; run them in the order of FLOWS.

What is covered, by flow:

- startup: an abandoned staging folder is cleared at start; the app follows
  teddyCloud's event stream (/api/sse).
- pages: every main page.
- upload, edit, copy_move, replace, batch, library, checkup, backup, radio,
  delete: every way the app writes audio, tags, models and covers, with
  umlauts in names and paths; deleting a taf a sticker plays (after the
  page's confirmation), and a tonie with its model and audio.
- sheet: print PDF, calibration and overview, covers read back from teddyCloud.
- box: a Toniebox is played by sending teddyCloud the box's own RTNL log
  records (tag placed, tag removed, a blank sticker) on its port, which is
  how a box reports them. teddyCloud turns them into /api/sse events and files
  a placed sticker as a box would; /api/box and the batch's "place stickers
  on the box" mode are checked against that.
- covers: teddyCloud's image cache (tonie_json.cache_images): a picture hosted
  elsewhere, fetched by teddyCloud on first use, and a cover of this app's own,
  which teddyCloud answers with a redirect.
- errors: refusals reaching the app's error pages, an unknown tonie, a second
  custom model with the same audio (teddyCloud refuses it; the app has to
  avoid it), listing a folder that does not exist (teddyCloud fails it; the
  app must never do it - checked in every flow).
- podcast_inproc: a podcast refresh, encode and putting it on a sticker. The
  running app fetches only public feeds and that guard is not touched: this
  flow starts a second app instance through create_app's ``fetch`` parameter
  (the injection point the tests use), with a fetch that reads the feed and
  episodes from local files. Everything after the fetch is the app's own code
  against the real teddyCloud. podcasts checks the guard of the real app.
- import: an import from Flipper dumps through teddyCloud's real cloud code,
  with a stand-in for the Tonies cloud: a container on a private Docker
  network that answers /v2/content/<ruid> over TLS with a certificate of a CA
  made here, which teddyCloud is given as its cloud CA together with a client
  certificate of the same CA. teddyCloud's cloud host is set to the stand-in
  and the real cloud's names point at 127.0.0.1. The stand-in records the
  token each request carried, so the check sees that teddyCloud sent each
  dump's own token, that the audio lands in the library, that nocloud is
  switched on after, and that a 401 puts the dump on the skip list. Before and
  after this flow teddyCloud's cloud access is off and read back as off.
- names: the naming upkeep (/import/names) moving an old NFC- model into the
  app's namespace, and naming a freshly imported tonie.

Flows that guard against regressions found once; they build on each other
only where said:

- library_form: "on a sticker" from the library sent as the page's own form
  (its hidden path read from the page), for a decomposed umlaut too: the
  sticker names the file as the library lists it.
- upload_refused: an upload without audio is refused with a message right
  inside <main>, which the album list's script shows in its #refusal box.
- delete_cached: deleting an official tonie whose audio teddyCloud cached
  beside its content JSON frees the sticker and deletes that audio.
- restore_restart: a restore previewed, the app killed and started again,
  then confirmed from its page.
- replace_copy_move: a sticker copied and another moved while a replace has
  them on the copy beside the target. replace_recovery, after it: the app
  killed during a replace's final encode; the checkup spares the target, and
  the recovery at the next start points the stickers back.
- podcast_move: a podcast's tonie moved while a refresh runs (second app
  instance, as podcast_inproc); failed_jobs: dozens of failed podcast
  refreshes leave teddyCloud and the app answering.

What is not covered, and why:

- the Tonies cloud itself and the tonies.com shop: never contacted.
- a real Toniebox: playback, and anything the box does beyond the RTNL
  records above.
- public podcast feeds: the running app's fetch is not exercised past its
  guard (see podcast_inproc).
- teddyCloud overlays (TS_TEDDYCLOUD_OVERLAY): teddyCloud makes an overlay
  when a box connects with its client certificate; this setup has no box.
- an import from a GitHub repository: it needs GitHub, and only the reading of
  the dumps differs from an import of uploaded files.

Run from the repository root, with the app's virtualenv:

    .venv/bin/python scripts/e2e_teddycloud.py --work /some/scratch/dir

Needs Docker and ffmpeg; pulls the teddyCloud image (IMAGE) and a Python image
for the stand-in (STANDIN_IMAGE) if they are not there. The first start of a
fresh teddyCloud generates its certificates, which takes minutes (over ten
on a slow machine); they are kept in --certs (default WORK/certs) and reused,
so CI should cache that folder. teddyCloud is published on
127.0.0.1:TC_PORT, the app listens on 127.0.0.1:APP_PORT (and APP_PORT + 1
for podcast_inproc). teddyCloud's cloud access is switched off, its cloud host
pointed at the stand-in, and both read back before anything else happens;
should that not read back, nothing runs. Everything started is stopped at the
end. All logs - the app's, teddyCloud's, the stand-in's and its record of
requests - are written below --work/<mode>/. The exit status is 1 when any
flow failed.

Usage: python3 scripts/e2e_teddycloud.py [--work DIR] [--modes normal,slow]
                                         [--flows pages,upload,...] [--src DIR]
                                         [--certs DIR] [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import html.parser
import http.server
import io
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tonie_studio.album import album_id  # noqa: E402
from tonie_studio.teddycloud import TeddyCloudClient  # noqa: E402

IMAGE = "ghcr.io/toniebox-reverse-engineering/teddycloud:0.7.0"
STANDIN_IMAGE = "python:3.14-slim"
CONTAINER = "tc-e2e"
STANDIN = "tc-e2e-standin"
NETWORK = "tc-e2e-net"
# What teddyCloud calls the stand-in: its name on the private network.
STANDIN_HOST = "tonies-standin"
TC_PORT = 18791
APP_PORT = 18753
PODCAST_APP_PORT = APP_PORT + 1
TC_URL = f"http://127.0.0.1:{TC_PORT}"
APP_URL = f"http://127.0.0.1:{APP_PORT}"

# Names the Tonies cloud goes by. teddyCloud's own settings keep it from
# asking them; pointing them at the container's loopback makes sure.
CLOUD_HOSTS = ("prod.de.tbs.toys", "tbs2.tonie.cloud", "rtnl.bxcl.de", "ici.tonie.cloud")

# Test stickers end like real ones. Each gets a content folder of its own:
# the tag index lists one sticker per folder. "0000000..." would be a system tag.
RUID_TAIL = "500304e0"

# A fresh teddyCloud makes its certificates first: minutes, on a slow machine many.
TC_START_TIMEOUT = 900
APP_START_TIMEOUT = 60
# An encode of the large file takes ffmpeg a while; slowed, its upload too.
JOB_TIMEOUT = 1200
# The second dump of an import waits 30 s, the pace's least (120 an hour).
IMPORT_TIMEOUT = 240

# Lines teddyCloud writes at every start or settings reload about certificates:
# the client certificate here is not Boxine's, which teddyCloud only warns about.
TC_NOISE = re.compile(r"tls_adapter\.c|certs/client/|test_boxine_ca|mqtt_init_box")

# What the slowed run sleeps after each piece of an upload body, in seconds.
SLOW_PIECE = (0.02, 0.05)

FLOWS = ("startup", "pages", "upload", "edit", "copy_move", "replace", "batch", "sheet",
         "library", "checkup", "backup", "radio", "box", "covers", "errors", "delete",
         "podcasts", "podcast_inproc", "import", "names",
         "library_form", "upload_refused", "delete_cached", "restore_restart",
         "replace_copy_move", "replace_recovery", "podcast_move", "failed_jobs")

# The audio of the replace flows, in seconds: long enough that teddyCloud's
# final encode of a replace leaves time to act while the stickers play the
# copy beside the target.
LONG_SECONDS = 1200

# How many podcast refreshes failed_jobs makes fail: more than the about 29
# connections teddyCloud 0.7.0 serves at once.
FAILED_JOBS = 35
# How long an answer may take once those jobs have failed, in seconds.
QUICK_ANSWER = 15

# Shared by every app process this script starts. It logs every call the app
# makes to teddyCloud, and in the slowed run it slows the upload body.
INSTRUMENT = '''"""tonie-studio, instrumented for scripts/e2e_teddycloud.py."""
import logging
import os
import random
import time

from tonie_studio import teddycloud

log = logging.getLogger("e2e.tc")
_request = teddycloud.TeddyCloudClient._request


def _logged(self, method, path, **kwargs):
    started = time.monotonic()
    try:
        response = _request(self, method, path, **kwargs)
    except teddycloud.TeddyCloudError as error:
        log.warning("E2E-TC %s %s -> %s after %.2fs: %s", method, path,
                    error.status or "no answer", time.monotonic() - started, str(error)[:300])
        raise
    body = response.text[:80].strip() if method == "POST" else ""
    log.info("E2E-TC %s %s -> %s after %.2fs %r", method, path, response.status_code,
             time.monotonic() - started, body)
    return response


teddycloud.TeddyCloudClient._request = _logged

if os.environ.get("E2E_SLOW_UPLOAD") == "1":
    _read = teddycloud._AfterHeadStart.read
    low, high = (float(part) for part in os.environ["E2E_SLOW_PIECE"].split(","))

    def _slow_read(self, size=-1):
        piece = _read(self, size)
        if piece:
            time.sleep(random.uniform(low, high))
        return piece

    teddycloud._AfterHeadStart.read = _slow_read
'''

# The app as it runs in production, instrumented.
APP_MODULE = '''import e2e_instrument  # noqa: F401
from tonie_studio.main import app  # noqa: E402,F401
'''

# A second app whose podcast fetch reads local files instead of the network.
# Built through create_app's fetch parameter; the app's own download and its
# guard against private addresses are neither used nor changed.
PODCAST_APP_MODULE = '''import logging
import os
from pathlib import Path

import time

import requests

import e2e_instrument  # noqa: F401
from tonie_studio.settings import Settings
from tonie_studio.web import create_app

FEEDS = Path(os.environ["E2E_FEED_DIR"])


def fetch(url, limit=None, into=None):
    path = FEEDS / url.rsplit("/", 1)[-1]
    # A file "delay" in the feed folder holds the seconds every episode
    # download takes, for a flow that acts while a refresh runs.
    delay = FEEDS / "delay"
    if path.suffix != ".xml" and delay.is_file():
        time.sleep(float(delay.read_text()))
    if not path.is_file():
        raise requests.HTTPError(f"404 for {url}")
    data = path.read_bytes()
    if into is not None:
        Path(into).write_bytes(data)
        return b""
    return data


def app():
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return create_app(Settings.from_env(), fetch=fetch, background=False)
    except TypeError:  # an older release without the background switch
        return create_app(Settings.from_env(), fetch=fetch)
'''

# The stand-in for the Tonies cloud, and a plain web server for pictures. It
# writes every request it gets, with the token it carried, to requests.jsonl.
STANDIN_SERVER = '''import http.server, json, os, ssl, threading, time

ROOT = "/standin"
LOG = os.path.join(ROOT, "requests.jsonl")
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def record(self, status):
        cert = None
        try:
            cert = self.connection.getpeercert()
        except Exception:
            pass
        entry = {"at": time.time(), "port": self.server.server_address[1],
                 "method": self.command, "path": self.path,
                 "auth": self.headers.get("Authorization", ""),
                 "client_cert": dict(x[0] for x in cert.get("subject", ())) if cert else None,
                 "status": status}
        with LOCK, open(LOG, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")

    def send(self, status, body=b""):
        self.record(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        name = None
        if path.startswith("/v2/content/"):
            ruid = path.rsplit("/", 1)[-1].lower()
            if os.path.exists(os.path.join(ROOT, "refuse", ruid)):
                return self.send(401)
            name = os.path.join(ROOT, "files", ruid + ".taf")
        elif path.startswith("/files/"):
            name = os.path.join(ROOT, "files", os.path.basename(path))
        if name and os.path.isfile(name):
            with open(name, "rb") as handle:
                return self.send(200, handle.read())
        return self.send(404)


def serve(port, tls):
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(ROOT, "server.crt"), os.path.join(ROOT, "server.key"))
        context.load_verify_locations(os.path.join(ROOT, "ca.crt"))
        context.verify_mode = ssl.CERT_OPTIONAL
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


threading.Thread(target=serve, args=(80, False), daemon=True).start()
serve(443, True)
'''

# How the certificates for the stand-in are made, inside the teddyCloud
# image so the same OpenSSL runs everywhere. teddyCloud reads its cloud CA and
# client certificate as DER, the key as PKCS#1: a PKCS#8 key fails the TLS
# set-up with "Wrong encoding".
MAKE_CERTS = (
    "set -e; cd /out; "
    "openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 "
    "-subj '/CN=tonie-studio e2e CA' -addext 'basicConstraints=critical,CA:TRUE' "
    "-addext 'keyUsage=critical,keyCertSign,cRLSign'; "
    "openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr "
    f"-subj '/CN={STANDIN_HOST}'; "
    f"printf 'subjectAltName=DNS:{STANDIN_HOST}\\nextendedKeyUsage=serverAuth\\n' > server.ext; "
    "openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial "
    "-out server.crt -days 3650 -extfile server.ext; "
    "openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj '/CN=e2e-box'; "
    "openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial "
    "-out client.crt -days 3650; "
    "openssl x509 -in ca.crt -outform DER -out ca.der; "
    "openssl x509 -in client.crt -outform DER -out client.der; "
    "openssl rsa -in client.key -outform DER -traditional -out private.der; "
    "chmod 644 *"
)


class FlowFailed(Exception):
    """A flow could not do or did not find what it should have."""


@dataclass
class Result:
    mode: str
    flow: str
    status: str = "PASS"
    seconds: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class Media:
    """The audio and pictures the flows upload, made once with ffmpeg."""

    upload_album: List[Tuple[str, Path]]
    upload_seconds: float
    cover: Path
    cover2: Path
    archive: Path
    batch_albums: List[Path]
    spare_albums: List[Path]
    place_album: Path
    short_flac: Path
    episodes: List[Path]
    dump: Path
    long_album: Path


# What an expected answer or log line looks like, returned by a flow:
# ("GET /path", status) for the app, ("TC GET /path", status) for a call the
# app made to teddyCloud, ("LOG", regex) for an app log line and ("TCLOG",
# regex) for a teddyCloud log line that the flow provokes on purpose.
Expected = Sequence[Tuple[str, object]]


# --- media ----------------------------------------------------------------


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def tone(target: Path, seconds: float, frequency: int, title: str, artist: str, album: str,
         track: int, extra: Sequence[str] = ()) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration={seconds}",
           "-ac", "2", *extra,
           "-metadata", f"title={title}", "-metadata", f"artist={artist}",
           "-metadata", f"album={album}", "-metadata", f"track={track}", str(target))


def picture(target: Path, source: str) -> None:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", source, "-frames:v", "1", "-q:v", "3", str(target))


def make_media(work: Path) -> Media:
    media = work / "media"
    upload = media / "upload"
    mp3 = ("-c:a", "libmp3lame", "-b:a", "128k")
    tone(upload / "01 Eins.flac", 5, 440, "Eins", "E2E Artist", "E2E Upload Album", 1)
    tone(upload / "02 Zwei.mp3", 4, 550, "Zwei", "E2E Artist", "E2E Upload Album", 2, mp3)
    # About 70 MB: past every buffer and spool size on the way.
    tone(upload / "03 Lang.wav", 400, 330, "Lang", "E2E Artist", "E2E Upload Album", 3,
         ("-c:a", "pcm_s16le"))
    cover = media / "cover.jpg"
    picture(cover, "testsrc=size=600x600:rate=1")
    picture(media / "cover2.jpg", "smptebars=size=480x480:rate=1")
    picture(upload / "cover.jpg", "testsrc=size=600x600:rate=1")

    archive = media / "archive"
    batch_albums, spare_albums = [], []
    for name, frequency in (("Album A", 600), ("Album B", 700)):
        folder = archive / "E2E Batch" / name
        tone(folder / "01 Erste.flac", 3, frequency, "Erste", "E2E Batch", name, 1)
        tone(folder / "02 Zweite.mp3", 3, frequency + 50, "Zweite", "E2E Batch", name, 2, mp3)
        picture(folder / "cover.jpg", "testsrc2=size=500x500:rate=1")
        batch_albums.append(folder)
    for name, frequency in (("Spare One", 800), ("Spare Two", 900)):
        folder = archive / "E2E Spare" / name
        tone(folder / "01 Nur.flac", 3, frequency, "Nur", "E2E Spare", name, 1)
        spare_albums.append(folder)
    place = archive / "E2E Place" / "Album P"
    tone(place / "01 Platz.flac", 3, 1000, "Platz", "E2E Place", "Album P", 1)
    picture(place / "cover.jpg", "rgbtestsrc=size=400x400:rate=1")

    long_album = archive / "E2E Long" / "Album L"
    tone(long_album / "01 Lang.mp3", LONG_SECONDS, 300, "Lang", "E2E Long", "Album L", 1,
         ("-c:a", "libmp3lame", "-b:a", "64k"))

    short = media / "short.flac"
    tone(short, 3, 1200, "Kurz", "E2E", "E2E", 1)
    feed = media / "feed"
    episodes = [feed / "episode1.mp3", feed / "episode2.mp3"]
    tone(episodes[0], 4, 500, "Folge 1", "E2E Podcast", "E2E Podcast", 1, mp3)
    tone(episodes[1], 3, 650, "Folge 2", "E2E Podcast", "E2E Podcast", 2, mp3)
    picture(feed / "podcast.jpg", "testsrc2=size=300x300:rate=1")

    dump = media / "dumps" / "E2E0FFFF500304E0.nfc"
    if not dump.exists():
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(dump_text("e2e0ffff500304e0", "00" * 32))
    return Media(
        upload_album=[(f"E2E Upload Album/{path.name}", path) for path in sorted(upload.iterdir())],
        upload_seconds=5 + 4 + 400,
        cover=cover, cover2=media / "cover2.jpg", archive=archive,
        batch_albums=batch_albums, spare_albums=spare_albums, place_album=place,
        short_flac=short, episodes=episodes, dump=dump, long_album=long_album,
    )


def dump_text(ruid: str, token: str) -> str:
    """A Flipper Zero dump of the tonie ``ruid`` with the cloud token ``token`` (hex)."""
    uid = " ".join(f"{byte:02X}" for byte in bytes.fromhex(ruid)[::-1])
    data = " ".join(token[index:index + 2].upper() for index in range(0, len(token), 2))
    return f"Filetype: Flipper NFC device\nDevice type: ISO15693\nUID: {uid}\nData Content: {data}\n"


def feed_xml(episodes: Sequence[Tuple[str, str, str, int]], title: str = "E2E Podcast") -> bytes:
    """An RSS feed of (guid, title, file, seconds) episodes, newest first."""
    items = "".join(
        f"<item><guid>{guid}</guid><title>{title}</title>"
        f"<pubDate>Mon, 0{6 - number} Oct 2026 10:00:00 +0000</pubDate>"
        f"<itunes:duration>{seconds}</itunes:duration>"
        f'<enclosure url="https://podcast.e2e.invalid/{name}" type="audio/mpeg"/></item>'
        for number, (guid, title, name, seconds) in enumerate(episodes))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            f"<channel><title>{title}</title>"
            '<itunes:image href="https://podcast.e2e.invalid/podcast.jpg"/>'
            f"{items}</channel></rss>").encode("utf-8")


def rtnl_tag(ruid: str, function: int, uptime: int, sequence: int) -> bytes:
    """One RTNL log record about a tag, framed as a Toniebox sends it.

    A protobuf TonieRtnlRPC with a log2 entry (teddyCloud's
    proto/toniebox.pb.rtnl.proto) behind a four-byte big-endian length. The
    tag id goes in with its 4-byte halves swapped, as the box writes it.
    """
    def varint(value: int) -> bytes:
        out = b""
        while True:
            byte, value = value & 0x7F, value >> 7
            if not value:
                return out + bytes([byte])
            out += bytes([byte | 0x80])

    uid = bytes.fromhex(ruid)[::-1]
    field6 = uid[4:] + uid[:4]
    log2 = (b"\x09" + struct.pack("<Q", uptime) + b"\x10" + varint(sequence) + b"\x18\x01"
            + b"\x20" + varint(15) + b"\x28" + varint(function)
            + b"\x32" + varint(len(field6)) + field6)
    rpc = b"\x12" + varint(len(log2)) + log2
    return struct.pack(">I", len(rpc)) + rpc


# RTNL tag functions of an ESP32 box (teddyCloud's include/handler_rtnl.h).
RTNL_TAG_VALID, RTNL_TAG_REMOVED, RTNL_TAG_INVALID = 16065, 16067, 15452


# --- the processes --------------------------------------------------------


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def make_cloud_certs(work: Path) -> Path:
    """The CA, server and client certificates of the stand-in, made once per work dir."""
    folder = work / "cloud-ca"
    if not (folder / "private.der").is_file():
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        done = docker("run", "--rm", "--entrypoint", "sh", "-v", f"{folder}:/out", IMAGE,
                      "-c", MAKE_CERTS, check=False)
        if done.returncode != 0:
            raise SystemExit(f"cannot make the stand-in's certificates: {done.stderr[-500:]}")
    return folder


class TeddyCloud:
    """The containers, their logs, and direct reads of what teddyCloud holds."""

    def __init__(self, certs: Path, cloud_ca: Path):
        self.certs = certs
        self.cloud_ca = cloud_ca
        self.http = requests.Session()
        self.log_lines = 0
        self.standin_dir: Optional[Path] = None

    def start(self, mode_dir: Path) -> None:
        self.remove()
        if not port_free(TC_PORT):
            raise SystemExit(f"port {TC_PORT} is taken; stop whatever listens there")
        client = self.certs / "client"
        client.mkdir(parents=True, exist_ok=True)
        for name in ("ca.der", "client.der", "private.der"):
            shutil.copyfile(self.cloud_ca / name, client / name)

        self.standin_dir = mode_dir / "standin"
        for sub in ("files", "refuse"):
            (self.standin_dir / sub).mkdir(parents=True, exist_ok=True)
        (self.standin_dir / "server.py").write_text(STANDIN_SERVER)
        for name in ("ca.crt", "server.crt", "server.key"):
            shutil.copyfile(self.cloud_ca / name, self.standin_dir / name)
        docker("network", "create", NETWORK, check=False)
        docker("run", "-d", "--name", STANDIN, "--network", NETWORK,
               "--network-alias", STANDIN_HOST, "-v", f"{self.standin_dir}:/standin",
               STANDIN_IMAGE, "python", "/standin/server.py")

        hosts = [arg for host in CLOUD_HOSTS for arg in ("--add-host", f"{host}:127.0.0.1")]
        # Not --rm: should teddyCloud die, its log is the evidence.
        docker("run", "-d", "--name", CONTAINER, "--network", NETWORK,
               "-p", f"127.0.0.1:{TC_PORT}:80", *hosts,
               "-v", f"{self.certs}:/teddycloud/certs", IMAGE)
        began = time.time()
        deadline = began + TC_START_TIMEOUT
        while time.time() < deadline:
            try:
                if self.http.get(f"{TC_URL}/api/settings/get/cloud.enabled", timeout=3).ok:
                    print(f"teddyCloud answered after {time.time() - began:.0f} s", flush=True)
                    break
            except requests.RequestException:
                pass
            time.sleep(2)
        else:
            raise SystemExit("teddyCloud did not answer in time")
        self.log_lines = len(self.full_log())

    def setting(self, name: str) -> str:
        return self.http.get(f"{TC_URL}/api/settings/get/{name}", timeout=10).text.strip()

    def configure(self, **values: str) -> None:
        """Set teddyCloud settings, save them, and read them back.

        A set only changes the settings in memory, and teddyCloud reloads them
        from its file whenever that file is newer, as it is right after the
        start: an unsaved set is lost then. So they are written to the file
        too. The answer to a set is empty even when it worked, so only reading
        back tells.
        """
        names = {key.replace("__", "."): value for key, value in values.items()}
        for name, value in names.items():
            self.http.post(f"{TC_URL}/api/settings/set/{name}", data=value, timeout=10)
        self.http.get(f"{TC_URL}/api/triggerWriteConfig", timeout=10)
        # Past the next round of teddyCloud's check for a changed file.
        time.sleep(2.5)
        wrong = {name: self.setting(name) for name in names
                 if self.setting(name) != names[name]}
        if wrong:
            raise FlowFailed(f"teddyCloud settings do not read back as set: {wrong}")

    def lock_cloud(self) -> None:
        """Cloud access off, its host the stand-in; nothing runs unless that reads back."""
        try:
            self.configure(cloud__remote_hostname=STANDIN_HOST,
                           cloud__remote_hostname_tb2=STANDIN_HOST, cloud__enabled="false")
        except FlowFailed as error:
            raise SystemExit(f"cannot verify teddyCloud's cloud is off: {error}; "
                             "stopping before any test") from error

    def stop(self, save_to: Optional[Path]) -> None:
        if save_to is not None:
            try:
                (save_to / "teddycloud.log").write_text("\n".join(self.full_log()) + "\n")
                done = docker("logs", STANDIN, check=False)
                (save_to / "standin.log").write_text(done.stdout + done.stderr)
            except (OSError, subprocess.SubprocessError):
                pass
        self.remove()

    @staticmethod
    def remove() -> None:
        docker("rm", "-f", CONTAINER, STANDIN, check=False)
        docker("network", "rm", NETWORK, check=False)

    def gone(self) -> Optional[str]:
        """Why the container is not running, or None while it is."""
        done = docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}} {{.State.Error}}",
                      CONTAINER, check=False)
        state = done.stdout.strip()
        return None if state.startswith("true") else (state or done.stderr.strip())

    def full_log(self) -> List[str]:
        done = docker("logs", CONTAINER, check=False)
        return (done.stdout + done.stderr).splitlines()

    def new_log(self) -> List[str]:
        lines = self.full_log()
        new, self.log_lines = lines[self.log_lines:], len(lines)
        return new

    def standin_requests(self) -> List[dict]:
        path = self.standin_dir / "requests.jsonl" if self.standin_dir else None
        if path is None or not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # Reads straight from teddyCloud, not through the app.

    def get(self, path: str, **kwargs) -> requests.Response:
        """A GET whose text is read as UTF-8, which teddyCloud sends without saying so."""
        response = self.http.get(f"{TC_URL}{path}", timeout=30, **kwargs)
        response.encoding = "utf-8"
        return response

    def tags(self) -> Dict[str, dict]:
        index = self.get("/api/getTagIndex").json()
        return {entry["ruid"]: entry for entry in index.get("tags", [])}

    def content(self, ruid: str) -> dict:
        response = self.get(f"/content/json/get/{ruid}")
        return response.json() if response.ok else {}

    def set_tag(self, ruid: str, **fields: str) -> None:
        answer = self.http.post(f"{TC_URL}/content/json/set/{ruid}", data=fields, timeout=30)
        if answer.text.strip() != "OK":
            raise FlowFailed(f"teddyCloud refused setting {fields} on {ruid}: {answer.text!r}")

    def models(self) -> Dict[str, dict]:
        entries = self.get("/api/toniesCustomJson").json()
        return {str(entry.get("model")): entry for entry in entries}

    def upsert_model(self, model: dict) -> str:
        """teddyCloud's answer to writing ``model``: "OK", or why not."""
        return self.http.post(f"{TC_URL}/api/toniesCustomJsonUpsert", json=model,
                              timeout=30).text.strip()

    def delete_models(self, models: Sequence[str]) -> None:
        self.http.post(f"{TC_URL}/api/toniesCustomJsonDelete", json={"models": list(models)},
                       timeout=30)

    def listing(self, path: str, special: Optional[str] = "library") -> List[dict]:
        params = {"path": path}
        if special:
            params["special"] = special
        response = self.get("/api/fileIndexV2", params=params)
        return response.json().get("files", []) if response.ok else []

    def library_entry(self, path: str) -> Optional[dict]:
        folder, _slash, name = path.rpartition("/")
        return next((entry for entry in self.listing(folder or "/")
                     if entry.get("name") == name and not entry.get("isDir")), None)

    def fetch(self, path: str) -> requests.Response:
        return self.get(path)

    def disk_size(self, path: str) -> Optional[int]:
        done = docker("exec", CONTAINER, "stat", "-c", "%s", path, check=False)
        return int(done.stdout.strip()) if done.returncode == 0 else None

    def disk_list(self, path: str) -> List[str]:
        done = docker("exec", CONTAINER, "ls", "-1A", path, check=False)
        return done.stdout.split() if done.returncode == 0 else []

    def send_rtnl(self, records: Sequence[bytes]) -> None:
        """Send RTNL records the way a box does: raw, on teddyCloud's HTTP port."""
        with socket.create_connection(("127.0.0.1", TC_PORT), timeout=10) as connection:
            connection.sendall(b"".join(records))
            time.sleep(1)


class App:
    """An app process on the host, its log, and a browser-like session."""

    def __init__(self, work: Path, archive: Path, slow: bool, src: Path, port: int = APP_PORT,
                 module: str = "e2e_app", name: str = "app",
                 extra_env: Optional[Dict[str, str]] = None):
        self.work = work
        self.src = src
        self.archive = archive
        self.slow = slow
        self.port = port
        self.module = module
        self.name = name
        self.extra_env = extra_env or {}
        self.data = work / ("data" if name == "app" else f"{name}-data")
        self.log_path = work / f"{name}.log"
        self.log_offset = 0
        self.process: Optional[subprocess.Popen] = None
        self.http = requests.Session()
        self.http.headers["Accept-Language"] = "en"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if not port_free(self.port):
            raise SystemExit(f"port {self.port} is taken; stop whatever listens there")
        self.data.mkdir(parents=True, exist_ok=True)
        (self.work / "tmp").mkdir(exist_ok=True)
        wrapper_dir = self.work / "wrapper"
        wrapper_dir.mkdir(exist_ok=True)
        (wrapper_dir / "e2e_instrument.py").write_text(INSTRUMENT)
        (wrapper_dir / "e2e_app.py").write_text(APP_MODULE)
        (wrapper_dir / "e2e_podcast_app.py").write_text(PODCAST_APP_MODULE)
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": os.pathsep.join([str(self.src), str(wrapper_dir)]),
            "TS_TEDDYCLOUD_URL": TC_URL,
            "TS_DATA_DIR": str(self.data),
            "TS_ARCHIVE_DIR": str(self.archive),
            "TMPDIR": str(self.work / "tmp"),
            "E2E_SLOW_UPLOAD": "1" if self.slow else "0",
            "E2E_SLOW_PIECE": ",".join(str(value) for value in SLOW_PIECE),
            **self.extra_env,
        })
        python = ROOT / ".venv" / "bin" / "python"
        if self.process is None:
            # A restart reads on where the log was read up to.
            self.log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.process = subprocess.Popen(
            [str(python if python.exists() else sys.executable), "-m", "uvicorn", "--factory",
             f"{self.module}:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=self.log_path.open("a"), stderr=subprocess.STDOUT)
        deadline = time.time() + APP_START_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"the app exited at start; see {self.log_path}")
            try:
                if self.http.get(f"{self.url}/health", timeout=3).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise SystemExit("the app did not answer in time")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def kill(self) -> None:
        """End the process at once (SIGKILL), as a crash or the OOM killer does."""
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=20)

    def new_log(self) -> List[str]:
        if not self.log_path.exists():
            return []
        with self.log_path.open("rb") as handle:
            handle.seek(self.log_offset)
            data = handle.read()
        self.log_offset += len(data)
        return data.decode("utf-8", errors="replace").splitlines()

    def request(self, method: str, path: str, expect: Sequence[int] = (200,),
                **kwargs) -> requests.Response:
        kwargs.setdefault("allow_redirects", False)
        kwargs.setdefault("timeout", 600)
        response = self.http.request(method, f"{self.url}{path}", **kwargs)
        if response.status_code not in expect:
            raise FlowFailed(f"{method} {path} answered {response.status_code}, expected "
                             f"{'/'.join(map(str, expect))}: {page_text(response.text)[:400]}")
        return response

    def get(self, path: str, expect: Sequence[int] = (200,), **kwargs) -> requests.Response:
        return self.request("GET", path, expect, **kwargs)

    def post(self, path: str, expect: Sequence[int] = (303,), **kwargs) -> requests.Response:
        return self.request("POST", path, expect, **kwargs)

    def wait_job(self, job_id: str) -> dict:
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            state = self.get(f"/api/job/{job_id}").json()
            if state["state"] in ("done", "failed"):
                return state
            time.sleep(1)
        raise FlowFailed(f"job {job_id} did not finish in {JOB_TIMEOUT} s")

    def wait_group(self, group: str) -> List[dict]:
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            jobs = self.get(f"/api/group/{group}").json()["jobs"]
            if all(job["state"] in ("done", "failed") for job in jobs):
                return jobs
            time.sleep(1)
        raise FlowFailed(f"the jobs of {group} did not finish")

    def wait_page(self, path: str) -> requests.Response:
        """Follow a page that refreshes itself while its background run goes on."""
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            response = self.get(path, expect=(200, 303))
            if response.status_code == 303:
                path = response.headers["location"]
                continue
            if 'http-equiv="refresh"' not in response.text:
                return response
            time.sleep(1)
        raise FlowFailed(f"{path} still running after {JOB_TIMEOUT} s")


def page_text(markup: str) -> str:
    """What a page says, without its markup, for a failure message."""
    main = re.search(r"<main.*?</main>", markup, re.S)
    text = re.sub(r"<noscript>.*?</noscript>", " ", main.group(0) if main else markup, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class _FormReader(html.parser.HTMLParser):
    """The fields of one form of a page, read the way a browser sends them."""

    def __init__(self, action: str):
        super().__init__(convert_charrefs=True)
        self.action = action
        self.inside = False
        self.found = False
        self.fields: Dict[str, str] = {}
        self.choices: Dict[str, List[str]] = {}
        self.select: Optional[str] = None
        self.option: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        attr = {name: value or "" for name, value in attrs}
        if tag == "form":
            self.inside = attr.get("action") == self.action and not self.found
            self.found = self.found or self.inside
            return
        if not self.inside:
            return
        name = attr.get("name")
        if tag == "input" and name:
            kind = attr.get("type", "text")
            if kind == "radio":
                self.choices.setdefault(name, []).append(attr.get("value", "on"))
                if "checked" in attr:
                    self.fields[name] = attr.get("value", "on")
            elif kind == "checkbox":
                if "checked" in attr:
                    self.fields[name] = attr.get("value", "on")
            elif kind not in ("submit", "button", "file"):
                self.fields[name] = attr.get("value", "")
        elif tag == "select" and name:
            self.select = name
        elif tag == "option" and self.select:
            value = attr.get("value", "")
            if self.select not in self.fields or "selected" in attr:
                self.fields[self.select] = value

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self.select = None


def form_fields(page: str, action: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """What the form posting to ``action`` sends as it stands, and its radio choices."""
    reader = _FormReader(action)
    reader.feed(page)
    if not reader.found:
        raise FlowFailed(f"the page has no form posting to {action}: {page_text(page)[:300]}")
    return reader.fields, reader.choices


class _MessageReader(html.parser.HTMLParser):
    """The text of the elements with class "warn" right inside <main>."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.messages: List[str] = []
        self.depth: Optional[int] = None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        parent = self.stack[-1] if self.stack else ""
        if self.depth is None and parent == "main" and "warn" in classes:
            self.depth = len(self.stack)
            self.messages.append("")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        while self.stack:
            name = self.stack.pop()
            if self.depth is not None and len(self.stack) == self.depth:
                self.depth = None
            if name == tag:
                break

    def handle_data(self, data):
        if self.depth is not None:
            self.messages[-1] += data


def main_messages(page: str) -> List[str]:
    """What a page says in its messages: the ``main > .warn`` the album list's script reads."""
    reader = _MessageReader()
    reader.feed(page)
    return [re.sub(r"\s+", " ", text).strip() for text in reader.messages]


def same_text(text: str) -> str:
    """``text`` with its umlauts composed, for comparing two spellings of one name."""
    return unicodedata.normalize("NFC", text)


def location(response: requests.Response) -> str:
    return response.headers.get("location", "")


def wait_for(what: str, check: Callable[[], object], seconds: float = 20) -> object:
    """``check()`` once it is truthy; FlowFailed naming ``what`` after ``seconds``."""
    deadline = time.time() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.time() > deadline:
            raise FlowFailed(f"{what}: not within {seconds:.0f} s")
        time.sleep(0.5)


# --- the run --------------------------------------------------------------


class Run:
    """One pass over the flows against one fresh teddyCloud and app."""

    def __init__(self, mode: str, work: Path, media: Media, tc: TeddyCloud, src: Path):
        self.mode = mode
        self.work = work
        self.media = media
        self.tc = tc
        self.src = src
        self.app = App(work, media.archive, slow=(mode == "slow"), src=src)
        # Apps whose logs are read after every flow; the podcast one joins for its flow.
        self.apps: List[App] = [self.app]
        self.client = TeddyCloudClient(TC_URL)
        self._podcast_app: Optional[App] = None
        self.stickers = 0
        self.rtnl_sequence = 0
        # Expected answers noted while a flow runs (see expect), on top of
        # what the flow returns.
        self._expected: List[Tuple[str, object]] = []
        # What one flow leaves for the next: stickers, targets, models.
        self.state: Dict[str, object] = {}
        self.results: List[Result] = []

    # Stickers, made as the box would leave them: a content folder with an
    # empty entry. Uploaded through the app's own client, so this is an
    # upload path that is checked too. next_ruid alone leaves the filing to
    # teddyCloud, for a sticker the box flow places.

    def next_ruid(self) -> str:
        self.stickers += 1
        return f"e2e{self.stickers:05x}{RUID_TAIL}"

    def new_sticker(self) -> str:
        ruid = self.next_ruid()
        folder, name = ruid[:8].upper(), ruid[8:].upper()
        entry = {"live": False, "nocloud": False, "source": "", "skip_seconds": 0,
                 "cache": True, "cloud_ruid": "", "cloud_auth": "", "cloud_override": False,
                 "tonie_model": "", "hide": False, "claimed": False, "_version": 5}
        self.client.create_dir(f"/{folder}")
        self.client.upload(io.BytesIO(json.dumps(entry, indent=4).encode("utf-8")),
                           f"{name}.json", path=f"/{folder}")
        if ruid not in self.tc.tags():
            raise FlowFailed(f"test sticker {ruid} does not show in teddyCloud's tag index")
        return ruid

    def box(self, ruid: str, function: int) -> None:
        """A box reporting ``function`` (placed, removed, blank) for the tag ``ruid``."""
        self.rtnl_sequence += 1
        self.tc.send_rtnl([rtnl_tag(ruid, function, 100_000 + 1000 * self.rtnl_sequence,
                                    self.rtnl_sequence)])

    def run(self, flows: Sequence[str]) -> List[Result]:
        if "startup" in flows:
            # Left as an interrupted run would leave it, before the app starts.
            self.client.create_dir("/_tonie-studio-staging", special="library")
            self.client.create_dir("/_tonie-studio-staging/e2e0abandoned", special="library")
            with self.media.short_flac.open("rb") as stream:
                self.client.upload(stream, "001.flac", path="/_tonie-studio-staging/e2e0abandoned",
                                   special="library")
        self.app.start()
        try:
            for name in flows:
                self.flow(name, getattr(self, f"flow_{name}"))
        finally:
            for app in self.apps:
                app.stop()
            self.app.stop()
        return self.results

    def flow(self, name: str, work: Callable[[Result], Optional[Expected]]) -> None:
        result = Result(self.mode, name)
        started = time.time()
        expected: Expected = ()
        try:
            expected = work(result) or ()
        except FlowFailed as failure:
            result.status = "FAIL"
            result.notes.append(str(failure))
        except KeyError as missing:
            result.status = "FAIL"
            result.notes.append(f"needs {missing} from an earlier flow; run the flows in order")
        except Exception as error:  # a broken flow is a finding, not the end of the run
            result.status = "FAIL"
            result.notes.append(f"{type(error).__name__}: {error}")
            result.notes.append(traceback.format_exc(limit=4).strip().splitlines()[-3])
        # Let the job threads and teddyCloud's log catch up.
        time.sleep(1.5)
        self.inspect_logs(result, list(expected) + self._expected)
        self._expected = []
        if name != "import" and self.tc.setting("cloud.enabled") != "false":
            result.status = "FAIL"
            result.notes.append("teddyCloud's cloud access is on outside the import flow")
            self.tc.lock_cloud()
        result.seconds = time.time() - started
        self.results.append(result)
        print(f"  [{self.mode}] {name}: {result.status} ({result.seconds:.0f} s)", flush=True)
        for note in result.notes:
            print(f"      {note[:300]}", flush=True)

    def expect(self, *entries: Tuple[str, object]) -> None:
        """Expected answers and log lines, noted while the flow runs (see Expected)."""
        self._expected = self._expected + list(entries)

    def inspect_logs(self, result: Result, expected: Expected) -> None:
        """Turn what the logs say about this flow into notes, and FAIL where it counts."""
        problems: List[str] = []
        warnings: List[str] = []

        def allowed(request: str, status: int) -> bool:
            return any(status == want and request == pattern for pattern, want in expected)

        def tolerated(kind: str, line: str) -> bool:
            return any(pattern == kind and re.search(str(regex), line)
                       for pattern, regex in expected)

        waiting: List[str] = []
        for line in self.tc.new_log():
            if TC_NOISE.search(line):
                continue
            started = re.search(r"file_save_start\| (?:Writing to '([^']+)'|Filename '([^']+)' "
                                r"already exists)", line)
            if started:
                waiting.append(Path(started.group(1) or started.group(2)).name)
                continue
            received = re.search(r"multipart_handle\| Received file '([^']+)'", line)
            if received:
                name = received.group(1)
                if name in waiting:
                    waiting.remove(name)
                else:
                    problems.append(f"teddyCloud: received {name} without a file_save_start")
                continue
            missing = re.search(r">> (\d{3}) (\S+)", line)
            if missing and allowed(f"TC GET {missing.group(2)}", int(missing.group(1))):
                continue
            if tolerated("TCLOG", line):
                continue
            if "mutex_manager.c" in line:
                # teddyCloud timing its own lock on tonies.json, which it holds
                # while it reloads the 6 MB catalogue after every custom model
                # write. Past a second it says ERROR; that is its load, not a
                # fault the app caused.
                warnings.append(f"teddyCloud: {line.strip()}")
            elif "SSE Client with error" in line:
                # A client of the event stream went away, as the podcast
                # app's does when it stops; the app's own stream reconnects
                # and says so in its log, which is read below.
                warnings.append(f"teddyCloud: {line.strip()}")
            elif any(host in line for host in CLOUD_HOSTS) and "Connecting" in line:
                problems.append(f"teddyCloud tried to reach the Tonies cloud: {line.strip()}")
            elif "Json parse error" in line or "runtime error" in line or line.startswith("ERROR"):
                problems.append(f"teddyCloud: {line.strip()}")
            elif line.startswith("WARN"):
                warnings.append(f"teddyCloud: {line.strip()}")
        for name in waiting:
            problems.append(f"teddyCloud: upload of {name} started (file_save_start) "
                            "and never finished (no 'Received file')")
        down = self.tc.gone()
        if down:
            problems.append(f"teddyCloud container is not running any more: {down}")

        for app in self.apps:
            for line in app.new_log():
                access = re.search(r'"(GET|POST) (\S+) HTTP/1\.1" (\d{3})', line)
                if access:
                    request = f"{access.group(1)} {access.group(2).split('?')[0]}"
                    status = int(access.group(3))
                    if status >= 400 and not allowed(request, status):
                        problems.append(f"{app.name} answered {status} to {request}")
                    continue
                if "E2E-TC" in line:
                    call = re.search(r"E2E-TC (GET|POST) (\S+) -> (\d+)", line)
                    if call and allowed(f"TC {call.group(1)} {call.group(2)}", int(call.group(3))):
                        continue
                    if re.search(r"\bWARNING\b", line):
                        problems.append(f"{app.name}->teddyCloud: "
                                        f"{line.split('E2E-TC', 1)[1].strip()}")
                    continue
                if tolerated("LOG", line):
                    continue
                if "Traceback" in line or re.search(r"\b(ERROR|WARNING|CRITICAL)\b", line):
                    problems.append(f"{app.name}: {line.strip()}")

        if problems:
            result.status = "FAIL"
        elif warnings and result.status == "PASS":
            result.status = "WARN"
        result.notes.extend(dict.fromkeys(problems))
        result.notes.extend(dict.fromkeys(warnings))

    # --- checks shared by the flows ---------------------------------------

    def check_taf(self, target: str, chapters: int, seconds: Optional[float]) -> dict:
        """The taf at ``target`` is in teddyCloud's library, valid, and holds all the audio."""
        entry = self.tc.library_entry(target)
        if entry is None:
            raise FlowFailed(f"no taf at {target} in teddyCloud's library")
        header = entry.get("tafHeader") or {}
        if not header.get("valid"):
            raise FlowFailed(f"taf {target} is not valid: {header}")
        marks = header.get("trackSeconds") or []
        if len(marks) not in (chapters, chapters + 1):
            raise FlowFailed(f"taf {target} has chapter marks {marks}, expected {chapters} chapters")
        if seconds is not None:
            # The marks are where chapters start; the encoded size gives the length.
            estimated = (entry.get("size") or 0) / (96_000 / 8)
            if estimated < seconds * 0.85:
                raise FlowFailed(f"taf {target} holds about {estimated:.0f} s of audio, "
                                 f"expected {seconds:.0f} s: audio lost on the way")
        return header

    def check_tag(self, ruid: str, source: str, model: Optional[str] = None,
                  live: bool = False) -> dict:
        entry = self.tc.content(ruid)
        if entry.get("source") != source:
            raise FlowFailed(f"sticker {ruid} plays {entry.get('source')!r}, expected {source!r}")
        if not entry.get("nocloud"):
            raise FlowFailed(f"sticker {ruid} has nocloud off")
        if live and not entry.get("live"):
            raise FlowFailed(f"sticker {ruid} is not live")
        if model is not None and (entry.get("tonie_model") or "").lower() != model.lower():
            raise FlowFailed(f"sticker {ruid} names model {entry.get('tonie_model')!r}, "
                             f"expected {model!r}")
        return entry

    def check_empty(self, ruid: str) -> None:
        entry = self.tc.content(ruid)
        if entry.get("source") or entry.get("tonie_model"):
            raise FlowFailed(f"sticker {ruid} is not empty: source {entry.get('source')!r}, "
                             f"model {entry.get('tonie_model')!r}")

    def check_cover(self, picture: str, exact: Optional[bytes] = None) -> bytes:
        """The cover a model names is served whole, and is the file on disk."""
        if not picture.startswith("/custom_img/"):
            raise FlowFailed(f"model picture {picture!r} is not in custom_img")
        served = self.tc.fetch(picture)
        if not served.ok or not served.content.startswith(b"\xff\xd8"):
            raise FlowFailed(f"cover {picture} is not served as a JPEG ({served.status_code}, "
                             f"{len(served.content)} bytes)")
        if not served.content.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise FlowFailed(f"cover {picture} is cut short: no JPEG end marker "
                             f"({len(served.content)} bytes)")
        on_disk = self.tc.disk_size(f"/teddycloud/data/www{picture}")
        if on_disk != len(served.content):
            raise FlowFailed(f"cover {picture}: {on_disk} bytes on disk, {len(served.content)} served")
        if exact is not None and served.content != exact:
            raise FlowFailed(f"cover {picture} differs from the uploaded file "
                             f"({len(served.content)} vs {len(exact)} bytes)")
        return served.content

    def check_staging_empty(self) -> None:
        left = self.tc.disk_list("/teddycloud/data/library/_tonie-studio-staging")
        if left:
            raise FlowFailed(f"staging folders left in the library: {left}")

    def album_form(self, identifier: str, app: Optional[App] = None) -> Dict[str, object]:
        """The album page's form as a browser would send it, every track ticked."""
        page = (app or self.app).get(f"/album/{identifier}").text
        order = re.search(r'name="order" id="order" value="([^"]*)"', page)
        numbers = re.findall(r'name="selected" value="(\d+)"', page)
        titles = [html.unescape(value) for value in re.findall(r'name="titles" value="([^"]*)"', page)]
        if not numbers:
            raise FlowFailed(f"album page {identifier} shows no tracks")
        return {"order": order.group(1) if order else "", "selected": numbers, "titles": titles,
                "parts": "1", "cover_index": "0", "cover_zoom": "1.0", "cover_x": "0", "cover_y": "0"}

    def encode_album(self, identifier: str, series: str, episode: str,
                     replace: bool = False, wait: bool = True) -> Tuple[str, dict]:
        """Start the album page's encode and wait for it; returns the job id and its state.

        Without ``wait`` it returns once the job is started, with an empty state.
        """
        form = self.album_form(identifier)
        form.update({"series": series, "episode": episode})
        response = self.app.post(f"/album/{identifier}/encode", expect=(303, 200), data=form)
        if replace:
            if response.status_code != 200:
                raise FlowFailed("encoding onto an existing taf was not asked about first")
            value = re.search(r'name="replace" value="([^"]*)"', response.text)
            if not value:
                raise FlowFailed("the album page offers no replace button for the existing taf")
            form["replace"] = html.unescape(value.group(1))
            response = self.app.post(f"/album/{identifier}/encode", data=form)
        elif response.status_code != 303:
            raise FlowFailed(f"encode not started: {page_text(response.text)[:300]}")
        job_id = location(response).rsplit("/", 1)[-1]
        if not location(response).startswith("/job/"):
            raise FlowFailed(f"encode went to {location(response)!r}, not to its job")
        self.app.get(f"/job/{job_id}")
        if not wait:
            return job_id, {}
        state = self.app.wait_job(job_id)
        if state["state"] != "done":
            raise FlowFailed(f"encode job failed: {state.get('error')}")
        return job_id, state

    def archive_id(self, folder: Path) -> str:
        identifier = "a" + album_id(self.media.archive, folder)
        if identifier not in self.app.get("/").text:
            raise FlowFailed(f"album list does not show {folder.name} as {identifier}")
        return identifier

    def library_taf(self, target: str) -> dict:
        """A taf made here straight in teddyCloud, as another tool would; its header."""
        folder, _slash, name = target.rpartition("/")
        self.client.create_dir(folder, special="library")
        with self.media.short_flac.open("rb") as stream:
            self.client.upload(stream, f"{name}.flac", path=folder, special="library")
        self.client.encode(target, [f"{folder}/{name}.flac"])
        self.client.delete(f"{folder}/{name}.flac", special="library")
        return self.check_taf(target, chapters=1, seconds=3)

    def cover_links(self, page: str) -> List[str]:
        return sorted({html.unescape(link) for link in re.findall(r'src="(/cover/[^"]+)"', page)})

    def wait_box(self, what: str, check: Callable[[dict], bool]) -> dict:
        return wait_for(f"/api/box {what}", lambda: (lambda state: state if check(state) else None)(
            self.app.get("/api/box").json()))

    # --- the flows ----------------------------------------------------------

    def flow_startup(self, result: Result):
        wait_for("abandoned staging folder removed at start", lambda: "e2e0abandoned" not in
                 self.tc.disk_list("/teddycloud/data/library/_tonie-studio-staging"))
        state = self.wait_box("connected to teddyCloud's event stream",
                              lambda state: state.get("connected"))
        result.notes.append(f"/api/box: {state}")
        self.app.get("/health")

    def flow_pages(self, result: Result):
        for path in ("/", "/tonies", "/library", "/sheet", "/checkup", "/backup", "/import",
                     "/import/names", "/podcasts", "/radio", "/listening"):
            self.app.get(path)

    def flow_upload(self, result: Result):
        ruid = self.new_sticker()
        files = [("files", (name, path.open("rb"))) for name, path in self.media.upload_album]
        try:
            response = self.app.post("/upload", expect=(200, 303), files=files,
                                     headers={"X-Upload": "1"})
        finally:
            for _field, (_name, handle) in files:
                handle.close()
        # An older release does not know X-Upload and answers with the redirect.
        following = (response.json()["next"] if response.status_code == 200
                     else location(response))
        identifier = re.search(r"ids=(u[0-9a-f]+)", following).group(1)
        self.app.get(following)
        # The umlaut travels through every encoding on the way: multipart,
        # urlencoded forms, JSON, and teddyCloud's own file names.
        series, episode = "E2E Upload", f"Upload Album {self.mode} B\u00e4r"
        job_id, _state = self.encode_album(identifier, series, episode)
        self.app.get(f"/job/{job_id}/assign")
        answer = self.app.post(f"/job/{job_id}/assign", data={
            "ruid": ruid, "series": series, "episode": episode, "category": "audio-play"})
        if location(answer) != f"/tonies/{ruid}":
            raise FlowFailed(f"assign went to {location(answer)!r}")
        self.app.get(f"/tonies/{ruid}")

        target = f"/{series}/{episode}.taf"
        header = self.check_taf(target, chapters=3, seconds=self.media.upload_seconds)
        entry = self.check_tag(ruid, f"lib://{target.lstrip('/')}")
        model = self.tc.models().get(entry["tonie_model"])
        if model is None:
            raise FlowFailed(f"model {entry['tonie_model']} is not in tonies.custom.json")
        if model.get("episodes") != episode or len(model.get("tracks") or []) != 3:
            raise FlowFailed(f"model reads {model}")
        if str(header.get("audioId")) not in [str(value) for value in model.get("audio_id") or []]:
            raise FlowFailed(f"model audio_id {model.get('audio_id')} is not the taf's "
                             f"{header.get('audioId')}")
        self.check_cover(model.get("pic") or "")
        self.check_staging_empty()
        self.state.update(upload_ruid=ruid, upload_target=target, upload_album=identifier,
                          upload_model=entry["tonie_model"], upload_audio=header.get("audioId"),
                          upload_series=series, upload_episode=episode)

    def flow_edit(self, result: Result):
        ruid, model_id = self.state["upload_ruid"], self.state["upload_model"]
        cover = self.media.cover2.read_bytes()
        self.app.post(f"/tonies/{ruid}", data={
            "series": "E2E Edited \u00d6l", "episode": f"Edited {self.mode} \u00df",
            "category": "audio-book"},
            files={"cover": ("new cover.jpg", cover, "image/jpeg")})
        model = self.tc.models().get(model_id) or {}
        wanted = {"series": "E2E Edited \u00d6l", "episodes": f"Edited {self.mode} \u00df",
                  "category": "audio-book"}
        wrong = {key: model.get(key) for key, value in wanted.items() if model.get(key) != value}
        if wrong:
            raise FlowFailed(f"model {model_id} reads {wrong} after the edit")
        if str(self.state["upload_audio"]) not in [str(v) for v in model.get("audio_id") or []]:
            raise FlowFailed(f"the edit lost the model's audio_id: {model.get('audio_id')}")
        self.check_cover(model.get("pic") or "", exact=cover)
        tag = self.tc.tags().get(ruid, {})
        if (tag.get("tonieInfo") or {}).get("series") != "E2E Edited \u00d6l":
            raise FlowFailed(f"teddyCloud's tag index names the sticker {tag.get('tonieInfo')}")
        self.app.get("/tonies")

    def flow_copy_move(self, result: Result):
        ruid, model_id = self.state["upload_ruid"], self.state["upload_model"]
        source = f"lib://{str(self.state['upload_target']).lstrip('/')}"
        copy, moved = self.new_sticker(), self.new_sticker()
        answer = self.app.post(f"/tonies/{ruid}/copy", data={"target": copy})
        if location(answer) != "/tonies":
            raise FlowFailed(f"copy went to {location(answer)!r}")
        self.check_tag(copy, source, model_id)
        answer = self.app.post(f"/tonies/{copy}/copy", data={"target": moved, "move": "1"})
        if location(answer) != f"/tonies/{moved}":
            raise FlowFailed(f"move went to {location(answer)!r}")
        self.check_tag(moved, source, model_id)
        if copy in self.tc.tags():
            raise FlowFailed(f"moved-from sticker {copy} is still in the tag index")
        left = self.tc.disk_list(f"/teddycloud/data/content/default/{copy[:8].upper()}")
        if left:
            raise FlowFailed(f"moved-from sticker {copy} left files behind: {left}")
        self.app.get(f"/tonies/{moved}")
        self.state["moved_ruid"] = moved

    def flow_replace(self, result: Result):
        target = str(self.state["upload_target"])
        source = f"lib://{target.lstrip('/')}"
        before = self.state["upload_audio"]
        self.encode_album(str(self.state["upload_album"]), str(self.state["upload_series"]),
                          str(self.state["upload_episode"]), replace=True)
        header = self.check_taf(target, chapters=3, seconds=self.media.upload_seconds)
        if header.get("audioId") == before:
            raise FlowFailed(f"the taf at {target} still has the old audio id {before}")
        for ruid in (self.state["upload_ruid"], self.state["moved_ruid"]):
            self.check_tag(str(ruid), source)
        folder = target.rsplit("/", 1)[0]
        asides = [entry["name"] for entry in self.tc.listing(folder) if ".new-" in entry["name"]]
        if asides:
            raise FlowFailed(f"replace left its copy behind: {asides}")
        self.check_staging_empty()
        model = self.tc.models()[str(self.state["upload_model"])]
        matched = ((self.tc.library_entry(target) or {}).get("tonieInfo") or {}).get("model")
        if str(header.get("audioId")) not in [str(value) for value in model.get("audio_id") or []]:
            raise FlowFailed(
                f"model {model['model']} still carries audio_id {model.get('audio_id')} of the "
                f"replaced audio, the taf now has {header.get('audioId')}; teddyCloud matches "
                f"the new taf to model {matched!r}")

    def flow_batch(self, result: Result):
        stickers = {self.new_sticker() for _album in self.media.batch_albums}
        ids = [self.archive_id(folder) for folder in self.media.batch_albums]
        page = self.app.get("/batch?" + urlencode([("ids", value) for value in ids])).text
        series = [html.unescape(v) for v in re.findall(r'name="series" value="([^"]*)"', page)]
        episodes = [html.unescape(v) for v in re.findall(r'name="episodes" value="([^"]*)"', page)]
        shown = re.findall(r'name="ids" value="([^"]*)"', page)
        answer = self.app.post("/batch", data={"ids": shown, "series": series,
                                               "episodes": episodes, "stickers": "auto"})
        group = location(answer).rsplit("/", 1)[-1]
        self.app.get(f"/parts/{group}")
        jobs = self.app.wait_group(group)
        failed = [f"{job['target']}: {job.get('error') or job.get('note')}" for job in jobs
                  if job["state"] != "done" or not job["assigned"]]
        if failed:
            raise FlowFailed(f"batch jobs not done or not on a sticker: {failed}")
        tags = self.tc.tags()
        made = []
        for job in jobs:
            holders = [ruid for ruid in stickers
                       if (self.tc.content(ruid).get("tonie_model") or "").lower()
                       == job["model"].lower()]
            if len(holders) != 1:
                raise FlowFailed(f"model {job['model']} is on {holders}, expected one test sticker")
            self.check_tag(holders[0], f"lib://{job['target'].lstrip('/')}", job["model"])
            self.check_taf(job["target"], chapters=2, seconds=6)
            model = self.tc.models().get(job["model"]) or {}
            self.check_cover(model.get("pic") or "")
            made.append((holders[0], job["model"], job["target"]))
            if not tags.get(holders[0], {}).get("exists"):
                result.notes.append(f"tag index says {holders[0]} audio does not exist")
        self.check_staging_empty()
        self.state["batch"] = made

    def flow_sheet(self, result: Result):
        self.app.get("/sheet")
        models = [str(self.state["upload_model"])] + [model for _r, model, _t in self.state["batch"]]
        self.print_sheet(result, models)
        for path in ("/sheet/calibration", "/sheet/overview"):
            pdf = self.app.get(path).content
            if not pdf.startswith(b"%PDF") or len(pdf) < 2000:
                raise FlowFailed(f"{path} is no PDF worth printing ({len(pdf)} bytes)")
            result.notes.append(f"{path}: {len(pdf)} bytes")
        back = self.app.get("/api/sheet/back?" + urlencode({"model": models[0]})).json()
        if not back.get("tracks"):
            raise FlowFailed(f"/api/sheet/back has no text side for {models[0]}: {back}")

    def print_sheet(self, result: Result, models: Sequence[str]) -> None:
        answer = self.app.post("/sheet", data={
            "picks": [f"1:fill:{model}" for model in models], "back": "1", "back_tracks": "1"})
        job_id = location(answer).rsplit("/", 1)[-1]
        self.app.get(f"/sheet/print/{job_id}")
        state = wait_for("sheet made", lambda: (lambda s: s if s["state"] in ("done", "failed")
                                                else None)(
            self.app.get(f"/api/sheet/print/{job_id}").json()), JOB_TIMEOUT)
        if state["state"] != "done":
            raise FlowFailed(f"sheet failed: {state.get('problem')}")
        if state.get("skipped"):
            raise FlowFailed(f"sheet skipped {state['skipped']}")
        pdf = self.app.get(f"/sheet/print/{job_id}/pdf").content
        if not pdf.startswith(b"%PDF") or len(pdf) < 2000:
            raise FlowFailed(f"sheet PDF is no PDF worth printing ({len(pdf)} bytes)")
        result.notes.append(f"sheet of {len(models)} model(s): {len(pdf)} bytes")

    def flow_library(self, result: Result):
        targets = []
        for folder in self.media.spare_albums:
            identifier = self.archive_id(folder)
            self.encode_album(identifier, "E2E Spare", f"{folder.name} {self.mode}")
            targets.append(f"/E2E Spare/{folder.name} {self.mode}.taf")
            self.check_taf(targets[-1], chapters=1, seconds=3)
        page = self.app.get("/library").text
        for target in targets:
            if html.escape(target) not in page and target not in page:
                raise FlowFailed(f"library page does not list {target}")
        self.app.post("/library/delete", data={"path": targets[0], "players": ""})
        if self.tc.library_entry(targets[0]) is not None:
            raise FlowFailed(f"{targets[0]} is still in teddyCloud after the delete")
        ruid = self.new_sticker()
        self.app.get("/library/assign?" + urlencode({"path": targets[1]}))
        answer = self.app.post("/library/assign", data={
            "path": targets[1], "ruid": ruid, "series": "E2E Spare",
            "episode": "Assigned from library", "category": "music"})
        if location(answer) != f"/tonies/{ruid}":
            raise FlowFailed(f"library assign went to {location(answer)!r}")
        entry = self.check_tag(ruid, f"lib://{targets[1].lstrip('/')}")
        model = self.tc.models().get(entry.get("tonie_model") or "")
        if not model or model.get("episodes") != "Assigned from library":
            raise FlowFailed(f"library assign made model {model}")
        self.state.update(library_ruid=ruid, library_target=targets[1],
                          library_model=entry.get("tonie_model"))

    def checkup_offers(self) -> set:
        page = self.app.get("/checkup").text
        return set(re.findall(r'name="fix" value="(\w+)">\s*<input type="hidden" '
                              r'name="target" value="([^"]+)"', page))

    def flow_checkup(self, result: Result):
        loose = [str(self.state["upload_ruid"]), str(self.state["moved_ruid"]),
                 str(self.state["library_ruid"])]
        for ruid in loose:
            self.tc.set_tag(ruid, nocloud="false")
        orphan = "TS-9990"
        upsert = self.tc.upsert_model({"model": orphan, "series": "E2E Orphan",
                                       "episodes": "Left over", "tracks": []})
        if upsert != "OK":
            raise FlowFailed(f"cannot make an orphan model: {upsert!r}")
        offered = self.checkup_offers()
        broken = [pair for pair in offered
                  if pair[0] in ("empty_sticker", "repair_source", "repair_model")]
        if broken:
            raise FlowFailed(f"checkup finds stickers or models broken by the flows: {broken}")

        # Damage as it happened once: UTF-8 read as latin-1 and written back,
        # into a sticker's source and a model's names.
        moved, target = str(self.state["moved_ruid"]), str(self.state["upload_target"])
        source = f"lib://{target.lstrip('/')}"
        garbled = source.encode("utf-8").decode("latin-1")
        self.tc.set_tag(moved, source=garbled)
        mangled = "TS-9991"
        name = "E2E K\u00e4se"
        upsert = self.tc.upsert_model({"model": mangled, "series": name.encode().decode("latin-1"),
                                       "episodes": "Folge", "tracks": []})
        if upsert != "OK":
            raise FlowFailed(f"cannot make a garbled model: {upsert!r}")
        sticker = self.new_sticker()
        self.tc.set_tag(sticker, source=source, tonie_model=mangled, nocloud="true")
        # Audio nothing plays.
        orphan_taf = "/E2E Checkup/Unused.taf"
        self.library_taf(orphan_taf)
        offered = self.checkup_offers()
        for fix, target_of in (("repair_source", moved), ("repair_model", mangled),
                               ("delete_taf", orphan_taf)):
            if (fix, target_of) not in offered:
                raise FlowFailed(f"checkup does not offer {fix} for {target_of}: {sorted(offered)}")
            self.app.post("/checkup/fix", data={"fix": fix, "target": target_of})
        if self.tc.content(moved).get("source") != source:
            raise FlowFailed(f"repaired source reads {self.tc.content(moved).get('source')!r}")
        if self.tc.models().get(mangled, {}).get("series") != name:
            raise FlowFailed(f"repaired model reads {self.tc.models().get(mangled)}")
        if self.tc.library_entry(orphan_taf) is not None:
            raise FlowFailed(f"{orphan_taf} is still there after its fix")
        offered = self.checkup_offers()
        missing = [ruid for ruid in loose if ("nocloud", ruid) not in offered]
        if missing or ("delete_model", orphan) not in offered:
            raise FlowFailed(f"checkup does not offer the expected fixes; offers {sorted(offered)}")
        answer = self.app.post("/checkup/fix", data={"fix": "nocloud", "target": loose[0]})
        if not location(answer).startswith("/checkup?done="):
            raise FlowFailed(f"single fix went to {location(answer)!r}")
        if not self.tc.content(loose[0]).get("nocloud"):
            raise FlowFailed(f"single fix left nocloud off on {loose[0]}")
        self.app.post("/checkup/fix", data={"fix": "delete_model", "target": orphan})
        if orphan in self.tc.models():
            raise FlowFailed(f"orphan model {orphan} is still there after its fix")
        answer = self.app.post("/checkup/fix_all", data={"fix": "nocloud"})
        if "running=" not in location(answer):
            raise FlowFailed(f"fix for all went to {location(answer)!r}")
        final = self.app.wait_page(location(answer))
        if "state-failed" in final.text:
            raise FlowFailed(f"fix for all failed: {page_text(final.text)[:300]}")
        still = [ruid for ruid in loose if not self.tc.content(ruid).get("nocloud")]
        if still:
            raise FlowFailed(f"fix for all left nocloud off on {still}")

    def flow_backup(self, result: Result):
        self.app.get("/backup")
        backup = self.app.get("/backup/download").content
        with zipfile.ZipFile(io.BytesIO(backup)) as archive:
            names = archive.namelist()
            saved_models = json.loads(archive.read("models.json"))
        ruid, model_id, _target = self.state["batch"][0]
        model = self.tc.models()[model_id]
        cover_path = model.get("pic") or ""
        cover = self.tc.fetch(cover_path).content
        if f"covers/{cover_path.rsplit('/', 1)[-1]}" not in names:
            raise FlowFailed(f"backup has no cover for {model_id}: {names}")
        if not any(entry.get("model") == model_id for entry in saved_models):
            raise FlowFailed(f"backup has no model {model_id}")

        # What a lost volume would do to one tonie: model and cover gone.
        self.tc.delete_models([model_id])
        self.tc.set_tag(ruid, tonie_model="")
        self.client.delete(cover_path.rsplit("/", 1)[-1], special="custom_img")
        if model_id in self.tc.models() or self.tc.fetch(cover_path).ok:
            raise FlowFailed("could not remove the model and cover to restore them")

        preview = self.app.post("/restore", expect=(200,),
                                files={"file": ("backup.zip", backup, "application/zip")})
        token = re.search(r'action="/restore/([0-9A-Za-z]+)"', preview.text)
        if not token:
            raise FlowFailed(f"restore preview offers no confirmation: {page_text(preview.text)[:300]}")
        answer = self.app.post(f"/restore/{token.group(1)}")
        done = self.app.wait_page(location(answer))
        marked = [text.strip() for text in re.findall(r'class="state-failed"[^>]*>([^<]*)<',
                                                      done.text) if text.strip()]
        if marked:
            result.notes.append(f"restore page marks as failed/skipped/kept: {marked}")
        result.notes.append("expected: the restore asks teddyCloud for the deleted cover "
                            "and gets 404")
        restored = self.tc.models().get(model_id)
        if restored != model:
            raise FlowFailed(f"model {model_id} after restore: {restored}, before: {model}")
        back = self.tc.fetch(cover_path)
        if back.content != cover:
            raise FlowFailed(f"cover {cover_path} after restore differs "
                             f"({len(back.content)} vs {len(cover)} bytes)")
        self.check_cover(cover_path, exact=cover)
        self.check_tag(ruid, self.tc.content(ruid).get("source"), model_id)
        return [(f"TC GET {cover_path}", 404)]

    def flow_radio(self, result: Result):
        public, private = self.new_sticker(), self.new_sticker()
        url = "https://stream.example.org/e2e/live.mp3"
        self.app.post("/radio", data={"ruid": public, "url": url, "series": "E2E Radio",
                                      "episode": "Live", "category": "music"},
                      files={"cover": ("radio.jpg", self.media.cover.read_bytes(), "image/jpeg")})
        entry = self.check_tag(public, url, live=True)
        model = self.tc.models().get(entry.get("tonie_model") or "")
        if not model:
            raise FlowFailed(f"radio sticker names no model: {entry}")
        self.check_cover(model.get("pic") or "", exact=self.media.cover.read_bytes())
        local = "http://192.168.1.50:8000/stream"
        answer = self.app.post("/radio", expect=(303, 400),
                               data={"ruid": private, "url": local, "series": "E2E Radio LAN"})
        if answer.status_code == 303:
            self.check_tag(private, local, live=True)
            result.notes.append(f"a private stream address ({local}) is accepted and written")
        else:
            result.notes.append(f"a private stream address is refused: {page_text(answer.text)[:200]}")
        self.app.get("/radio")
        return [("POST /radio", 400)]

    def flow_box(self, result: Result):
        # A blank sticker held against the box: teddyCloud does not know it.
        blank = self.next_ruid()
        self.box(blank, RTNL_TAG_INVALID)
        self.wait_box("names the blank sticker as unknown",
                      lambda state: state.get("unknown_ruid") == blank)
        if blank in self.tc.tags():
            raise FlowFailed(f"teddyCloud filed the blank sticker {blank}")

        # A tonie the app made, placed and lifted.
        known = str(self.state["upload_ruid"])
        self.box(known, RTNL_TAG_VALID)
        state = self.wait_box("shows the placed tonie",
                              lambda state: state.get("ruid") == known and state.get("on_box"))
        if not state.get("label"):
            raise FlowFailed(f"/api/box has no name for the tonie on the box: {state}")
        self.box(known, RTNL_TAG_REMOVED)
        self.wait_box("shows the tonie lifted", lambda state: not state.get("on_box"))

        # A batch handing out its sticker by placing: a new sticker read by the
        # box, which teddyCloud files on its own, takes the encode.
        identifier = self.archive_id(self.media.place_album)
        page = self.app.get("/batch?" + urlencode({"ids": identifier})).text
        series = [html.unescape(v) for v in re.findall(r'name="series" value="([^"]*)"', page)]
        episodes = [html.unescape(v) for v in re.findall(r'name="episodes" value="([^"]*)"', page)]
        answer = self.app.post("/batch", data={"ids": [identifier], "series": series,
                                               "episodes": episodes, "stickers": "place"})
        group = location(answer).rsplit("/", 1)[-1]
        self.app.get(f"/parts/{group}")
        jobs = self.app.wait_group(group)
        if any(job["state"] != "done" for job in jobs):
            raise FlowFailed(f"place-mode encode failed: {jobs}")
        fresh = self.next_ruid()
        self.box(fresh, RTNL_TAG_VALID)
        wait_for("teddyCloud files the sticker the box read", lambda: fresh in self.tc.tags())
        self.wait_box("shows the fresh sticker", lambda state: state.get("ruid") == fresh)
        placed = self.app.post(f"/api/batch/{group}/place", expect=(200,),
                               data={"ruid": fresh}).json()
        if placed.get("status") != "assigned":
            raise FlowFailed(f"placing the sticker answered {placed}")
        job = self.app.get(f"/api/group/{group}").json()["jobs"][0]
        self.check_tag(fresh, f"lib://{job['target'].lstrip('/')}", job["model"])
        again = self.app.post(f"/api/batch/{group}/place", expect=(200,),
                              data={"ruid": fresh}).json()
        if again.get("status") not in ("assigned", "complete"):
            raise FlowFailed(f"placing the same sticker again answered {again}")
        self.box(fresh, RTNL_TAG_REMOVED)
        self.wait_box("shows the fresh sticker lifted", lambda state: not state.get("on_box"))
        self.app.get("/listening")
        self.state["placed_ruid"] = fresh

    def flow_covers(self, result: Result):
        if self.tc.setting("tonie_json.cache_preload") != "false":
            raise FlowFailed("tonie_json.cache_preload is on: teddyCloud would fetch every "
                             "picture of the catalogue from the internet")
        self.tc.configure(tonie_json__cache_images="true")
        try:
            picture_url = f"http://{STANDIN_HOST}/files/e2e-pic.jpg"
            pic_bytes = self.media.cover2.read_bytes()
            (self.tc.standin_dir / "files" / "e2e-pic.jpg").write_bytes(pic_bytes)
            ruid, _model, target = self.state["batch"][0]
            sticker = self.new_sticker()
            hosted = "TS-9980"
            answer = self.tc.upsert_model({"model": hosted, "series": "E2E Cached",
                                           "episodes": "Hosted picture", "tracks": ["Eins"],
                                           "pic": picture_url})
            if answer != "OK":
                raise FlowFailed(f"cannot write the model with a hosted picture: {answer!r}")
            self.tc.set_tag(sticker, source=f"lib://{target.lstrip('/')}", tonie_model=hosted,
                            nocloud="true")

            name = hashlib.sha256(picture_url.encode()).hexdigest().upper() + ".jpg"
            first = self.app.get(f"/cover/cache/{name}")
            if first.content != pic_bytes:
                raise FlowFailed(f"/cover/cache/{name} is not the hosted picture "
                                 f"({len(first.content)} vs {len(pic_bytes)} bytes)")
            asked = [entry for entry in self.tc.standin_requests()
                     if entry["path"] == "/files/e2e-pic.jpg"]
            self.app.get(f"/cover/cache/{name}?size=64")
            again = [entry for entry in self.tc.standin_requests()
                     if entry["path"] == "/files/e2e-pic.jpg"]
            if len(asked) != 1 or len(again) != 1:
                raise FlowFailed(f"teddyCloud fetched the hosted picture {len(asked)} then "
                                 f"{len(again)} time(s), expected once")
            if f"{name}" not in self.tc.disk_list("/teddycloud/data/cache"):
                raise FlowFailed(f"teddyCloud's cache has no {name}")

            # A cover of this app's own through the cache: teddyCloud cannot
            # fetch a bare path and answers with a redirect to custom_img.
            own = (self.tc.models()[str(self.state["upload_model"])].get("pic") or "")
            own_name = hashlib.sha256(own.encode()).hexdigest().upper() + ".jpg"
            redirected = self.app.get(f"/cover/cache/{own_name}")
            if redirected.content != self.tc.fetch(own).content:
                raise FlowFailed(f"/cover/cache for {own} is not the cover")
            self.app.get(f"/cover/custom/{own.rsplit('/', 1)[-1]}?mode=fill")

            pages = {path: self.app.get(path).text for path in
                     ("/tonies", f"/tonies/{sticker}", "/sheet", "/library", "/")}
            links = sorted({link for page in pages.values() for link in self.cover_links(page)})
            broken = []
            for link in links:
                response = self.app.get(link, expect=(200, 404, 502, 503))
                if response.status_code != 200 or not response.content:
                    broken.append(f"{link}: {response.status_code}")
            if broken:
                raise FlowFailed(f"cover links on the pages that do not load: {broken}")
            result.notes.append(f"{len(links)} cover link(s) on the pages load")
            self.print_sheet(result, [hosted])
            self.state["cached_ruid"] = sticker
        finally:
            self.tc.configure(tonie_json__cache_images="false")
        return [("TCLOG", r"web\.c.*(Unknown protocol|Failed to parse URL)"),
                ("TCLOG", r"Failed to fetch, redirecting instead")]

    def flow_errors(self, result: Result):
        unknown = "e2effffff00304e0"
        self.expect(("GET /tonies/" + unknown, 404), ("POST /tonies/" + unknown + "/copy", 400),
                    ("GET /library/assign", 404), ("GET /cover/custom/e2e-missing.jpg", 502),
                    ("TC GET /custom_img/e2e-missing.jpg", 404),
                    ("LOG", r"teddyCloud refused while serving /cover/custom/e2e-missing\.jpg"),
                    ("TCLOG", r"Failed to open dir '.*/E2E-no-such-folder"))
        self.app.get(f"/tonies/{unknown}", expect=(404,))
        self.app.post(f"/tonies/{unknown}/copy", expect=(400,),
                      data={"target": str(self.state["upload_ruid"])})
        self.app.get("/library/assign?" + urlencode({"path": "/E2E Nope/none.taf"}),
                     expect=(404,))
        refused = self.app.get("/cover/custom/e2e-missing.jpg", expect=(502,))
        if "teddyCloud" not in page_text(refused.text):
            raise FlowFailed(f"a refusal from teddyCloud reads {page_text(refused.text)[:200]}")

        # teddyCloud refuses a second model with the same audio; the app has
        # to give the second sticker a model without it.
        ruid, model_id, target = self.state["batch"][0]
        original = self.tc.models()[model_id]
        duplicate = dict(original, model="TS-9970", series="E2E Duplicate")
        answer = self.tc.upsert_model(duplicate)
        if answer == "OK":
            self.tc.delete_models(["TS-9970"])
            result.notes.append("teddyCloud accepted a second model with the same audio_id "
                                "and hash; the fake's unique_audio rule does not hold")
        else:
            result.notes.append(f"teddyCloud refuses a duplicate audio model: {answer[:80]!r}")
        second = self.new_sticker()
        self.app.post("/library/assign", data={
            "path": target, "ruid": second, "series": "E2E Second", "episode": "Same audio",
            "category": "music"})
        entry = self.check_tag(second, f"lib://{target.lstrip('/')}")
        made = self.tc.models().get(entry.get("tonie_model") or "") or {}
        if made.get("audio_id") or made.get("episodes") != "Same audio":
            raise FlowFailed(f"the second sticker's model is {made}")
        if self.tc.models().get(model_id) != original:
            raise FlowFailed(f"the first model {model_id} changed")

        # What the app must never do, shown once: teddyCloud answers a listing
        # of a folder that is not there by dropping the connection, and logs
        # an ERROR. Every flow fails on that log line.
        try:
            dropped = not self.tc.get("/api/fileIndexV2", params={
                "path": "/E2E-no-such-folder", "special": "library"}).ok
        except requests.RequestException:
            dropped = True
        result.notes.append(f"a listing of a missing folder: "
                            f"{'connection dropped / refused' if dropped else 'answered'}")
        self.state["second_ruid"] = second

    def flow_delete(self, result: Result):
        self.expect(("POST /library/delete", 409))
        # A taf a sticker plays: refused unless the page named that sticker.
        # Made here rather than by an encode job: the checkup spares every
        # path an encode job of this process wrote (see the report).
        target = "/E2E Delete/Played.taf"
        self.library_taf(target)
        ruid = self.new_sticker()
        self.app.post("/library/assign", data={"path": target, "ruid": ruid,
                                               "series": "E2E Delete", "episode": "Played"})
        model_id = self.check_tag(ruid, f"lib://{target.lstrip('/')}").get("tonie_model")
        self.app.post("/library/delete", expect=(409,), data={"path": target, "players": ""})
        if self.tc.library_entry(target) is None:
            raise FlowFailed(f"{target} was deleted although a sticker plays it")
        self.app.post("/library/delete", data={"path": target, "players": ruid})
        if self.tc.library_entry(target) is not None:
            raise FlowFailed(f"{target} is still there after the confirmed delete")
        offered = self.checkup_offers()
        if ("empty_sticker", ruid) not in offered:
            raise FlowFailed(f"checkup does not offer emptying {ruid}: {sorted(offered)}")
        self.app.post("/checkup/fix", data={"fix": "empty_sticker", "target": ruid})
        self.check_empty(ruid)
        if ("delete_model", model_id) not in self.checkup_offers():
            raise FlowFailed(f"checkup does not offer deleting the orphaned model {model_id}")
        self.app.post("/checkup/fix", data={"fix": "delete_model", "target": model_id})
        if model_id in self.tc.models():
            raise FlowFailed(f"model {model_id} is still there after its fix")

        # The same for a taf an encode job of this process made: checked and
        # reported, not failed, as it is the app's long-standing behaviour.
        batch_ruid, _model, batch_target = self.state["batch"][1]
        self.app.post("/library/delete", data={"path": batch_target, "players": batch_ruid})
        if ("empty_sticker", batch_ruid) not in self.checkup_offers():
            result.notes.append(f"checkup does not report {batch_ruid}, whose taf "
                                f"{batch_target} is gone, while the encode job that made it "
                                "is still in memory (it counts as work in progress)")
            self.app.post("/checkup/fix", expect=(303,),
                          data={"fix": "empty_sticker", "target": batch_ruid})

        # A tonie deleted from its page takes its own model and audio along.
        ruid = str(self.state["library_ruid"])
        model_id, target = str(self.state["library_model"]), str(self.state["library_target"])
        page = self.app.get(f"/tonies/{ruid}").text
        if f"/tonies/{ruid}/delete" not in page:
            raise FlowFailed("the tonie page offers no delete")
        self.app.post(f"/tonies/{ruid}/delete")
        self.check_empty(ruid)
        if model_id in self.tc.models():
            raise FlowFailed(f"model {model_id} is still there after deleting its tonie")
        if self.tc.library_entry(target) is not None:
            raise FlowFailed(f"{target} is still there after deleting its only tonie")

    def flow_podcasts(self, result: Result):
        self.app.get("/podcasts")
        ruid = self.new_sticker()
        hits: List[str] = []

        class Feed(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the name http.server calls
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Feed)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            feed = f"http://127.0.0.1:{server.server_address[1]}/feed.xml"
            answer = self.app.post("/podcasts", expect=(303, 400),
                                   data={"feed": feed, "ruid": ruid, "keep": "1"})
        finally:
            server.shutdown()
        if answer.status_code == 303:
            raise FlowFailed(f"a feed on 127.0.0.1 was accepted (fetched {hits})")
        if hits:
            raise FlowFailed(f"the refused local feed was fetched anyway: {hits}")
        result.notes.append("the running app refuses a feed on 127.0.0.1 before any fetch "
                            f"({page_text(answer.text)[:120]}); the rest is podcast_inproc")
        self.state["podcast_ruid"] = ruid
        return [("POST /podcasts", 400)]

    def podcast_app(self) -> Tuple[App, Path]:
        """The second app instance with a local fetch (see PODCAST_APP_MODULE), not started.

        One for the whole run, so its log is read on from flow to flow; and
        the folder its fetch reads feeds and episodes from.
        """
        feeds = self.work / "feeds"
        feeds.mkdir(exist_ok=True)
        for path in self.media.episodes + [self.media.episodes[0].parent / "podcast.jpg"]:
            shutil.copyfile(path, feeds / path.name)
        if self._podcast_app is None:
            self._podcast_app = App(self.work, self.media.archive, slow=(self.mode == "slow"),
                                    src=self.src, port=PODCAST_APP_PORT, module="e2e_podcast_app",
                                    name="podcast-app", extra_env={"E2E_FEED_DIR": str(feeds)})
            self.apps.append(self._podcast_app)
        return self._podcast_app, feeds

    def flow_podcast_inproc(self, result: Result):
        app, feeds = self.podcast_app()
        (feeds / "feed.xml").write_bytes(feed_xml([("e2e-ep1", "Folge 1", "episode1.mp3", 4)]))
        app.start()
        try:
            ruid = str(self.state.get("podcast_ruid") or self.new_sticker())
            answer = app.post("/podcasts", data={
                "feed": "https://podcast.e2e.invalid/feed.xml", "ruid": ruid, "keep": "2"})
            if not location(answer).startswith("/job/"):
                raise FlowFailed(f"subscribing went to {location(answer)!r}")
            state = app.wait_job(location(answer).rsplit("/", 1)[-1])
            if state["state"] != "done":
                raise FlowFailed(f"first podcast refresh failed: {state.get('error')}")
            first = self.tc.content(ruid)
            source = first.get("source") or ""
            self.check_tag(ruid, source)
            self.check_taf("/" + source[len("lib://"):], chapters=1, seconds=4)
            model = self.tc.models().get(first.get("tonie_model") or "") or {}
            if model.get("series") != "E2E Podcast" or model.get("tracks") != ["Folge 1"]:
                raise FlowFailed(f"podcast model reads {model}")
            self.check_cover(model.get("pic") or "",
                             exact=(feeds / "podcast.jpg").read_bytes())

            # A new episode: a new taf, the sticker moved to it, the old one gone.
            (feeds / "feed.xml").write_bytes(feed_xml([("e2e-ep2", "Folge 2", "episode2.mp3", 3),
                                                       ("e2e-ep1", "Folge 1", "episode1.mp3", 4)]))
            page = app.get("/podcasts").text
            subscription = re.search(r'action="/podcasts/([^/"]+)/refresh"', page)
            if not subscription:
                raise FlowFailed("the podcast page offers no refresh")
            answer = app.post(f"/podcasts/{subscription.group(1)}/refresh")
            state = app.wait_job(location(answer).rsplit("/", 1)[-1])
            if state["state"] != "done":
                raise FlowFailed(f"second podcast refresh failed: {state.get('error')}")
            second = self.tc.content(ruid)
            if second.get("source") == source:
                raise FlowFailed("the refresh with a new episode left the sticker as it was")
            self.check_taf("/" + second["source"][len("lib://"):], chapters=2, seconds=7)
            if self.tc.library_entry("/" + source[len("lib://"):]) is not None:
                raise FlowFailed(f"the old podcast taf {source} is still there")
            renewed = self.tc.models().get(second.get("tonie_model") or "") or {}
            if renewed.get("model") != model.get("model") or len(renewed.get("tracks") or []) != 2:
                raise FlowFailed(f"podcast model after the refresh reads {renewed}")
            self.check_staging_empty()
        finally:
            app.stop()
        result.notes.append("second app instance with a local fetch (create_app(fetch=...)); "
                            "the production fetch and its guard are not exercised here")

    def flow_import(self, result: Result):
        # With the cloud off, starting is refused and nothing is asked.
        self.app.get("/import")
        answer = self.app.post("/import/files", expect=(400, 409), data={
            "per_hour": "10", "jitter": "10", "max_failures": "3"},
            files={"files": (f"dumps/{self.media.dump.name}", self.media.dump.read_bytes(),
                             "application/octet-stream")})
        if self.app.get("/api/import").json().get("phase") is not None:
            raise FlowFailed("an import run was created with the cloud off")
        result.notes.append(f"cloud off: refused with {answer.status_code}")

        # The audio the stand-in hands out: a taf teddyCloud made, which no
        # model knows yet.
        header = self.library_taf("/E2E Import Source/source.taf")
        files = self.tc.standin_dir / "files"
        done = docker("cp", f"{CONTAINER}:/teddycloud/data/library/E2E Import Source/source.taf",
                      str(files / "source.taf"), check=False)
        if done.returncode != 0:
            raise FlowFailed(f"cannot copy the stand-in's taf: {done.stderr}")
        self.client.delete("/E2E Import Source/source.taf", special="library")
        good, bad = f"e2e0ff01{RUID_TAIL}", f"e2e0ff02{RUID_TAIL}"
        shutil.copyfile(files / "source.taf", files / f"{good}.taf")
        (self.tc.standin_dir / "refuse" / bad).write_text("")
        tokens = {good: secrets.token_hex(32).upper(), bad: secrets.token_hex(32).upper()}
        dumps = [("files", ("E2E Import/Folge Eins.nfc", dump_text(good, tokens[good]))),
                 ("files", ("E2E Import/Folge Zwei.nfc", dump_text(bad, tokens[bad])))]

        self.tc.configure(cloud__remote_hostname=STANDIN_HOST, cloud__remote_hostname_tb2=STANDIN_HOST)
        self.tc.configure(cloud__enabled="true")
        try:
            self.app.post("/import/files", data={"per_hour": "120", "jitter": "0",
                                                 "max_failures": "5"}, files=dumps)
            state = wait_for("import run finished", lambda: (lambda s: s if s.get("phase") in (
                "finished", "given_up", "stopped") else None)(self.app.get("/api/import").json()),
                IMPORT_TIMEOUT)
        finally:
            self.tc.lock_cloud()
        result.notes.append(f"run: {state.get('phase')} {state.get('counts')}")

        asked = [entry for entry in self.tc.standin_requests()
                 if entry["path"].startswith("/v2/content/")]
        by_ruid: Dict[str, List[dict]] = {}
        for entry in asked:
            by_ruid.setdefault(entry["path"].rsplit("/", 1)[-1].lower(), []).append(entry)
        wrong = [entry for ruid, entries in by_ruid.items() for entry in entries
                 if entry["auth"] != f"BD {tokens.get(ruid, '?')}"]
        if wrong:
            raise FlowFailed(f"teddyCloud sent the stand-in a token not of that dump: {wrong}")
        if set(by_ruid) != {good, bad} or any(len(entries) != 1 for entries in by_ruid.values()):
            raise FlowFailed(f"the stand-in was asked {[(r, len(e)) for r, e in by_ruid.items()]}, "
                             "expected each dump once")
        if any(not entry.get("client_cert") for entry in asked):
            raise FlowFailed("teddyCloud came to the stand-in without its client certificate")

        entry = self.tc.content(good)
        if entry.get("cloud_ruid") != good or (entry.get("cloud_auth") or "").upper() \
                != tokens[good] or entry.get("_version") != 5:
            raise FlowFailed(f"the imported tonie's entry reads {entry}")
        source = entry.get("source") or ""
        if not source.startswith("lib://by/audioID/") or not entry.get("nocloud"):
            raise FlowFailed(f"imported tonie: source {source!r}, nocloud {entry.get('nocloud')}")
        imported = self.check_taf("/" + source[len("lib://"):], chapters=1, seconds=3)
        if imported.get("audioId") != header.get("audioId"):
            raise FlowFailed(f"the library holds audio {imported.get('audioId')}, the stand-in "
                             f"handed out {header.get('audioId')}")
        skips = json.loads((self.app.data / "import-skip.json").read_text()) \
            if (self.app.data / "import-skip.json").is_file() else []
        if not any(skip.get("ruid") == bad for skip in skips):
            raise FlowFailed(f"the refused dump is not on the skip list: {skips}")
        if self.tc.content(bad).get("nocloud"):
            raise FlowFailed("the refused tonie had its cloud access switched off")
        if bad not in self.app.get("/import").text.lower():
            raise FlowFailed("the import page does not show the skipped dump")

        # Named right after the import: not in the catalogue, so a model of its own.
        named = wait_for("imported tonie named", lambda: (lambda model: model if model.startswith(
            "TS-") else None)(self.tc.content(good).get("tonie_model") or ""))
        model = self.tc.models().get(named) or {}
        if (model.get("series"), model.get("episodes")) != ("E2E Import", "Folge Eins") or \
                str(imported.get("audioId")) not in [str(v) for v in model.get("audio_id") or []]:
            raise FlowFailed(f"the imported tonie's model reads {model}")
        other = self.new_sticker()
        self.app.get("/library/assign?" + urlencode({"path": "/" + source[len("lib://"):]}))
        self.app.post("/library/assign", data={"path": "/" + source[len("lib://"):],
                                               "ruid": other})
        assigned = self.check_tag(other, source)
        if assigned.get("tonie_model"):
            raise FlowFailed(f"imported audio on a second sticker got model "
                             f"{assigned.get('tonie_model')!r}; teddyCloud names it itself")
        self.state.update(import_ruid=good, import_model=named)
        return [(f"TC GET /content/json/get/{good}", 404), (f"TC GET /content/json/get/{bad}", 404),
                ("LOG", r"import of .*Folge Zwei\.nfc failed: .*401"),
                ("POST /import/files", answer.status_code)]

    def flow_names(self, result: Result):
        # A tonie named by an earlier tool, under its NFC- prefix.
        header = self.library_taf("/E2E Names/Altes Modell.taf")
        sticker = self.new_sticker()
        old = "NFC-E2E01"
        answer = self.tc.upsert_model({
            "model": old, "series": "E2E Alt", "episodes": "Altes Modell", "tracks": ["Eins"],
            "language": "de-de", "category": "audio-play",
            "audio_id": [str(header["audioId"])], "hash": [str(header["sha1Hash"]).upper()]})
        if answer != "OK":
            raise FlowFailed(f"cannot make the NFC- model: {answer!r}")
        self.tc.set_tag(sticker, source="lib://E2E Names/Altes Modell.taf", tonie_model=old,
                        nocloud="true")

        page = self.app.get("/import/names").text
        plan = re.search(r'name="plan" value="([^"]*)"', page)
        if not plan or old not in page:
            raise FlowFailed(f"the names page plans nothing for {old}: {page_text(page)[:300]}")
        answer = self.app.post("/import/names", data={"plan": html.unescape(plan.group(1))})
        self.app.wait_page(location(answer))
        if old in self.tc.models():
            raise FlowFailed(f"{old} is still there after the upkeep")
        renamed = self.tc.content(sticker).get("tonie_model") or ""
        model = self.tc.models().get(renamed) or {}
        if not renamed.startswith("TS-") or (model.get("series"), model.get("episodes")) != \
                ("E2E Alt", "Altes Modell"):
            raise FlowFailed(f"the sticker now names {renamed!r}: {model}")
        if str(header["audioId"]) not in [str(v) for v in model.get("audio_id") or []]:
            raise FlowFailed(f"the renamed model lost its audio: {model}")

    # --- flows guarding regressions found once -------------------------------

    def assign_through_form(self, link: str, ruid: str) -> Dict[str, str]:
        """Put a library taf on ``ruid`` the way the page does: open ``link``, send its form."""
        page = self.app.get(link).text
        fields, choices = form_fields(page, "/library/assign")
        if ruid not in choices.get("ruid", []):
            raise FlowFailed(f"the assign form does not offer sticker {ruid}")
        fields["ruid"] = ruid
        answer = self.app.post("/library/assign", data=fields)
        if location(answer) != f"/tonies/{ruid}":
            raise FlowFailed(f"library assign went to {location(answer)!r}")
        return fields

    def flow_library_form(self, result: Result):
        # Every field from the page itself: its hidden path once carried the
        # page's own address, which only a form read from the page shows.
        plain = "/E2E Form/Ohne Umlaut.taf"
        # The archive share hands out decomposed umlauts, and teddyCloud keeps
        # the name as it was given.
        decomposed = "/E2E Form Umlaut/Ba\u0308renlied.taf"
        composed = unicodedata.normalize("NFC", decomposed)
        for target in (plain, decomposed):
            self.library_taf(target)
        listed = [entry["name"] for entry in self.tc.listing("/E2E Form Umlaut")]
        if decomposed.rsplit("/", 1)[1] not in listed:
            raise FlowFailed(f"teddyCloud does not list the decomposed name: {listed}")
        links = {unicodedata.normalize("NFC", unquote(html.unescape(link).split("path=", 1)[1])):
                 html.unescape(link) for link in re.findall(
                     r'href="(/library/assign\?path=[^"]+)"', self.app.get("/library").text)}
        stickers = []
        # From the library page's own links, and the umlaut once more as typed.
        for wanted, link in ((plain, links.get(plain)), (decomposed, links.get(composed)),
                             (decomposed, "/library/assign?" + urlencode({"path": composed}))):
            if link is None:
                raise FlowFailed(f"the library page offers no 'on a sticker' for {wanted}")
            ruid = self.new_sticker()
            fields = self.assign_through_form(link, ruid)
            if same_text(fields.get("path", "")) != same_text(wanted):
                raise FlowFailed(f"the form's hidden path is {fields.get('path')!r}, not {wanted!r}")
            entry = self.check_tag(ruid, f"lib://{wanted.lstrip('/')}")
            tag = self.tc.tags().get(ruid, {})
            if not tag.get("exists") or not tag.get("valid", True):
                raise FlowFailed(f"teddyCloud's tag index says the sticker {ruid} plays nothing: "
                                 f"exists {tag.get('exists')}, valid {tag.get('valid')}")
            model = self.tc.models().get(entry.get("tonie_model") or "") or {}
            if model.get("episodes") != fields.get("episode"):
                raise FlowFailed(f"the form's episode {fields.get('episode')!r} made model {model}")
            stickers.append(ruid)
        result.notes.append(f"assigned through the form: {', '.join(stickers)}")

    def flow_upload_refused(self, result: Result):
        # The album list's script shows a refusal in its own box, reading the
        # message the refusal page puts right inside <main>.
        page = self.app.get("/").text
        if 'id="refusal"' not in page:
            raise FlowFailed("the album list has no place (#refusal) to show a refused upload")
        answer = self.app.post("/upload", expect=(400,), headers={"X-Upload": "1"}, files=[
            ("files", ("E2E Notes/notes.txt", b"not audio\n", "text/plain")),
            ("files", ("E2E Notes/cover.jpg", self.media.cover.read_bytes(), "image/jpeg"))])
        messages = [text for text in main_messages(answer.text) if text]
        wanted = "None of those files is audio teddyCloud can encode."
        if messages[:1] != [wanted]:
            raise FlowFailed(f"the refusal says {messages}, expected {wanted!r} in main > .warn")
        return [("POST /upload", 400)]

    def flow_delete_cached(self, result: Result):
        # An official tonie: teddyCloud keeps its audio from the cloud in the
        # sticker's content folder, named after the second half of its id.
        catalogue = self.tc.get("/api/toniesJson").json()
        official = next((entry for entry in catalogue if entry.get("model")
                         and entry.get("audio_id")), None)
        if official is None:
            raise FlowFailed("teddyCloud's tonies.json lists no tonie to take a model from")
        header = self.library_taf("/E2E Cached/source.taf")
        local = self.work / "cached-source.taf"
        done = docker("cp", f"{CONTAINER}:/teddycloud/data/library/E2E Cached/source.taf",
                      str(local), check=False)
        if done.returncode != 0:
            raise FlowFailed(f"cannot copy the taf out of teddyCloud: {done.stderr}")
        self.client.delete("/E2E Cached/source.taf", special="library")
        ruid = self.new_sticker()
        folder, name = ruid[:8].upper(), ruid[8:].upper()
        with local.open("rb") as stream:
            self.client.upload(stream, name, path=f"/{folder}")
        self.tc.set_tag(ruid, tonie_model=str(official["model"]), nocloud="true")
        tag = self.tc.tags().get(ruid, {})
        if not tag.get("exists"):
            raise FlowFailed(f"teddyCloud does not see the cached audio of {ruid}: {tag}")
        page = self.app.get(f"/tonies/{ruid}").text
        if f"/tonies/{ruid}/delete" not in page:
            raise FlowFailed("the page of the official tonie offers no delete")
        self.app.post(f"/tonies/{ruid}/delete")
        left = self.tc.disk_list(f"/teddycloud/data/content/default/{folder}")
        if name in left:
            raise FlowFailed(f"the cached audio {folder}/{name} is still there: {left}")
        tag = self.tc.tags().get(ruid)
        if tag is not None and tag.get("exists"):
            raise FlowFailed(f"teddyCloud still sees audio on the deleted tonie {ruid}: {tag}")
        self.check_empty(ruid)
        result.notes.append(f"official model {official['model']}, cached audio "
                            f"{header.get('audioId')} deleted with the tonie")

    def flow_restore_restart(self, result: Result):
        model_id = "TS-9960"
        before = {"model": model_id, "series": "E2E Restore", "episodes": "Vorher", "tracks": []}
        if self.tc.upsert_model(before) != "OK":
            raise FlowFailed("cannot make the model the restore brings back")
        backup = self.app.get("/backup/download").content
        changed = dict(before, series="E2E Restore changed")
        if self.tc.upsert_model(changed) != "OK":
            raise FlowFailed("cannot change the model")
        preview = self.app.post("/restore", expect=(200,),
                                files={"file": ("backup.zip", backup, "application/zip")})
        token = re.search(r'action="/restore/([0-9A-Za-z]+)"', preview.text)
        if not token:
            raise FlowFailed(f"restore preview offers no confirmation: {page_text(preview.text)[:300]}")
        # Restarted between the preview and the confirmation, as an update
        # of the app would do it.
        self.crash_app()
        again = self.app.get(f"/restore/{token.group(1)}/done", expect=(200, 404))
        if again.status_code != 200:
            self.expect((f"GET /restore/{token.group(1)}/done", 404))
            raise FlowFailed(f"after a restart the restore page answers {again.status_code}: "
                             f"{page_text(again.text)[:200]}")
        if f'action="/restore/{token.group(1)}"' not in again.text:
            raise FlowFailed(f"after a restart the restore page offers no confirmation: "
                             f"{page_text(again.text)[:300]}")
        answer = self.app.post(f"/restore/{token.group(1)}")
        done = self.app.wait_page(location(answer))
        if "state-failed" in done.text:
            result.notes.append(f"restore page marks: {page_text(done.text)[:200]}")
        restored = self.tc.models().get(model_id) or {}
        if restored.get("series") != "E2E Restore":
            raise FlowFailed(f"model {model_id} after the restore: {restored}")
        self.tc.delete_models([model_id])

    def crash_app(self) -> None:
        """Kill the app (SIGKILL) and start it again."""
        self.app.kill()
        # Its event stream breaks off, which teddyCloud logs as an error.
        self.expect(("TCLOG", r"SSE Client with error"))
        self.app.start()

    def start_replace(self, identifier: str, series: str, episode: str) -> str:
        return self.encode_album(identifier, series, episode, replace=True, wait=False)[0]

    def wait_aside(self, ruid: str, job_id: str) -> str:
        """The copy beside the target the sticker plays once the replace's final encode runs."""
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            source = self.tc.content(ruid).get("source") or ""
            if ".new-" in source:
                return source
            if self.app.get(f"/api/job/{job_id}").json()["state"] in ("done", "failed"):
                raise FlowFailed(f"the replace ended before sticker {ruid} was seen on its copy")
            time.sleep(0.1)
        raise FlowFailed(f"sticker {ruid} never played the replace's copy")

    def still_running(self, job_id: str, what: str) -> None:
        if self.app.get(f"/api/job/{job_id}").json()["state"] != "running":
            raise FlowFailed(f"the replace ended before {what}; the check proves nothing "
                             "(make LONG_SECONDS larger)")

    def flow_replace_copy_move(self, result: Result):
        identifier = self.archive_id(self.media.long_album)
        series, episode = "E2E Long", f"Long {self.mode}"
        target = f"/{series}/{episode}.taf"
        source = f"lib://{target.lstrip('/')}"
        first, third = self.new_sticker(), self.new_sticker()
        job_id, _state = self.encode_album(identifier, series, episode)
        self.app.post(f"/job/{job_id}/assign", data={
            "ruid": first, "series": series, "episode": episode, "category": "audio-play"})
        self.app.post(f"/tonies/{first}/copy", data={"target": third})
        model_id = self.check_tag(first, source).get("tonie_model")
        self.check_tag(third, source, model_id)
        # What replace_recovery needs, should the checks below fail.
        self.state.update(long_album=identifier, long_series=series, long_episode=episode,
                          long_stickers=[first])

        copy, moved = self.new_sticker(), self.new_sticker()
        job_id = self.start_replace(identifier, series, episode)
        began = time.time()
        aside = self.wait_aside(first, job_id)
        self.app.post(f"/tonies/{first}/copy", data={"target": copy})
        self.app.post(f"/tonies/{third}/copy", data={"target": moved, "move": "1"})
        self.still_running(job_id, "the copy and the move were done")
        if self.tc.content(copy).get("source") != aside:
            raise FlowFailed(f"the copy {copy} plays {self.tc.content(copy).get('source')!r}, "
                             f"not the replace's copy {aside!r}")
        state = self.app.wait_job(job_id)
        if state["state"] != "done":
            raise FlowFailed(f"the replace failed: {state.get('error')}")
        result.notes.append(f"copy and move done while the stickers played {aside.rsplit('/', 1)[-1]}; "
                            f"replace took {time.time() - began:.0f} s")
        for ruid in (first, copy, moved):
            self.check_tag(ruid, source, model_id)
        if third in self.tc.tags():
            raise FlowFailed(f"the moved-from sticker {third} is in teddyCloud's tag index again")
        asides = [entry["name"] for entry in self.tc.listing(f"/{series}") if ".new-" in entry["name"]]
        if asides:
            raise FlowFailed(f"the replace left copies behind: {asides}")
        self.check_taf(target, chapters=1, seconds=LONG_SECONDS)
        self.check_staging_empty()
        self.state["long_stickers"] = [first, copy, moved]

    def flow_replace_recovery(self, result: Result):
        identifier, series = str(self.state["long_album"]), str(self.state["long_series"])
        episode = str(self.state["long_episode"])
        stickers = [str(ruid) for ruid in self.state["long_stickers"]]
        target = f"/{series}/{episode}.taf"
        source = f"lib://{target.lstrip('/')}"
        job_id = self.start_replace(identifier, series, episode)
        aside = self.wait_aside(stickers[0], job_id)
        killed = time.time()
        # teddyCloud goes on with the encode it was asked for.
        self.crash_app()
        self.expect(("LOG", r"is not complete; its stickers stay on"))
        on_aside = [ruid for ruid in stickers if self.tc.content(ruid).get("source") == aside]
        if on_aside:
            offered = self.checkup_offers()
            if ("delete_taf", target) in offered:
                raise FlowFailed(f"the checkup offers deleting {target}, which the stickers on "
                                 f"its copy are meant to play: {sorted(offered)}")
        else:
            result.notes.append("teddyCloud finished the target before the app was back; "
                                "the checkup was not looked at with the stickers on the copy")
        aside_size = (self.tc.library_entry("/" + aside[len("lib://"):]) or {}).get("size") or 0

        def finished():
            entry = self.tc.library_entry(target) or {}
            return ((entry.get("tafHeader") or {}).get("valid")
                    and (entry.get("size") or 0) >= aside_size)
        wait_for("teddyCloud finishes the target", finished, 300)
        result.notes.append(f"teddyCloud finished the target {time.time() - killed:.0f} s after "
                            "the app was killed")
        # Restarted once more: the recovery at start settles what is left.
        self.app.stop()
        self.app.start()
        wait_for("the stickers play the target again", lambda: all(
            self.tc.content(ruid).get("source") == source for ruid in stickers), 30)
        for ruid in stickers:
            self.check_tag(ruid, source)
        wait_for("the copy beside the target deleted", lambda: not any(
            ".new-" in entry["name"] for entry in self.tc.listing(f"/{series}")), 30)
        self.check_taf(target, chapters=1, seconds=LONG_SECONDS)
        wait_for("staging cleared at start", lambda: not self.tc.disk_list(
            "/teddycloud/data/library/_tonie-studio-staging"))
        if ("delete_taf", target) in self.checkup_offers():
            raise FlowFailed(f"the checkup offers deleting {target} after the recovery")

    def podcast_subscription(self, app: App, feed: str) -> dict:
        found = [entry for entry in json.loads((app.data / "podcasts.json").read_text())
                 if entry.get("feed") == feed]
        if len(found) != 1:
            raise FlowFailed(f"{len(found)} subscriptions to {feed}")
        return found[0]

    def flow_podcast_move(self, result: Result):
        app, feeds = self.podcast_app()
        shutil.copyfile(self.media.episodes[0], feeds / "episode3.mp3")
        title, feed = "E2E Podcast Move", "https://podcast.e2e.invalid/move.xml"
        episodes = [("e2e-move3", "Folge 3", "episode3.mp3", 4),
                    ("e2e-move2", "Folge 2", "episode2.mp3", 3),
                    ("e2e-move1", "Folge 1", "episode1.mp3", 4)]
        (feeds / "move.xml").write_bytes(feed_xml(episodes[2:], title))
        (feeds / "delay").unlink(missing_ok=True)
        app.start()
        try:
            old = self.new_sticker()
            answer = app.post("/podcasts", data={"feed": feed, "ruid": old, "keep": "2"})
            state = app.wait_job(location(answer).rsplit("/", 1)[-1])
            if state["state"] != "done":
                raise FlowFailed(f"first refresh failed: {state.get('error')}")
            subscription = self.podcast_subscription(app, feed)

            # A new episode, fetched slowly; the tonie moved meanwhile.
            (feeds / "move.xml").write_bytes(feed_xml(episodes[1:], title))
            (feeds / "delay").write_text("3")
            answer = app.post(f"/podcasts/{subscription['id']}/refresh")
            job_id = location(answer).rsplit("/", 1)[-1]
            wait_for("refresh running", lambda: app.get(f"/api/job/{job_id}").json()["state"]
                     != "queued")
            new = self.new_sticker()
            moved = app.post(f"/tonies/{old}/copy", data={"target": new, "move": "1"})
            if location(moved) != f"/tonies/{new}":
                raise FlowFailed(f"move went to {location(moved)!r}")
            if app.get(f"/api/job/{job_id}").json()["state"] != "running":
                raise FlowFailed("the refresh ended before the move; the check proves nothing")
            (feeds / "delay").unlink()
            state = app.wait_job(job_id)
            if state["state"] != "done":
                raise FlowFailed(f"the refresh during the move failed: {state.get('error')}")
            stored = self.podcast_subscription(app, feed)
            if stored["ruid"] != new:
                raise FlowFailed(f"the subscription names sticker {stored['ruid']}, the tonie "
                                 f"was moved to {new}")
            playing = self.tc.content(new).get("source") or ""
            self.check_tag(new, playing)
            self.check_taf("/" + playing[len("lib://"):], chapters=2, seconds=7)
            if old in self.tc.tags():
                raise FlowFailed(f"the moved-from sticker {old} is in teddyCloud's tag index again")

            # And the next refresh goes to the new sticker as well.
            (feeds / "move.xml").write_bytes(feed_xml(episodes, title))
            answer = app.post(f"/podcasts/{subscription['id']}/refresh")
            state = app.wait_job(location(answer).rsplit("/", 1)[-1])
            if state["state"] != "done":
                raise FlowFailed(f"the refresh after the move failed: {state.get('error')}")
            again = self.tc.content(new).get("source") or ""
            if again == playing:
                raise FlowFailed("the refresh after the move left the sticker as it was")
            self.check_taf("/" + again[len("lib://"):], chapters=2, seconds=7)
            if old in self.tc.tags():
                raise FlowFailed(f"the refresh after the move brought {old} back")
            self.check_staging_empty()
            self.state.update(move_subscription=subscription["id"], move_ruid=new)
        finally:
            (feeds / "delay").unlink(missing_ok=True)
            app.stop()

    def flow_failed_jobs(self, result: Result):
        app, feeds = self.podcast_app()
        feed = "https://podcast.e2e.invalid/failing.xml"
        (feeds / "failing.xml").write_bytes(feed_xml([("e2e-fail1", "Folge 1", "episode1.mp3", 4)],
                                                     "E2E Podcast Failing"))
        app.start()
        ruid, playing = self.new_sticker(), ""
        try:
            answer = app.post("/podcasts", data={"feed": feed, "ruid": ruid, "keep": "1"})
            state = app.wait_job(location(answer).rsplit("/", 1)[-1])
            if state["state"] != "done":
                raise FlowFailed(f"first refresh failed: {state.get('error')}")
            subscription = self.podcast_subscription(app, feed)["id"]
            playing = self.tc.content(ruid).get("source") or ""
            # Given other audio: every refresh refuses the sticker, after
            # asking teddyCloud about it.
            self.tc.set_tag(ruid, source="lib://E2E Elsewhere/other.taf")
            self.expect(("LOG", r"job \w+ \(E2E Podcast Failing\) failed"), ("LOG", r"^Traceback"))
            began = time.time()
            for number in range(FAILED_JOBS):
                answer = app.post(f"/podcasts/{subscription}/refresh", timeout=60)
                job_id = location(answer).rsplit("/", 1)[-1]
                deadline = time.time() + 60
                while app.get(f"/api/job/{job_id}", timeout=60).json()["state"] not in (
                        "done", "failed"):
                    if time.time() > deadline:
                        raise FlowFailed(f"refresh {number + 1} did not end within 60 s")
                    time.sleep(0.2)
                state = app.get(f"/api/job/{job_id}", timeout=60).json()
                if state["state"] != "failed":
                    raise FlowFailed(f"refresh {number + 1} did not fail: {state}")
            result.notes.append(f"{FAILED_JOBS} refreshes failed in {time.time() - began:.0f} s")
            for name, ask in (("teddyCloud", lambda: self.tc.http.get(
                                  f"{TC_URL}/api/getTagIndex", timeout=QUICK_ANSWER)),
                              ("the podcast app", lambda: app.get("/tonies", timeout=QUICK_ANSWER)),
                              ("the app", lambda: self.app.get("/tonies", timeout=QUICK_ANSWER))):
                started = time.time()
                try:
                    ask()
                except requests.RequestException as error:
                    raise FlowFailed(f"{name} does not answer within {QUICK_ANSWER} s after "
                                     f"{FAILED_JOBS} failed jobs: {type(error).__name__}") from error
                result.notes.append(f"{name} answered in {time.time() - started:.1f} s")
            app.post(f"/podcasts/{subscription}/delete")
        finally:
            app.stop()
            if playing:
                self.tc.set_tag(ruid, source=playing)


# --- report ---------------------------------------------------------------


def report(results: List[Result]) -> int:
    width = max(len(result.flow) for result in results)
    print("\n" + "=" * 78)
    print(f"{'mode':<7} {'flow':<{width}}  status  secs  notes")
    print("-" * 78)
    for result in results:
        first = result.notes[0] if result.notes else ""
        print(f"{result.mode:<7} {result.flow:<{width}}  {result.status:<6} {result.seconds:5.0f}  "
              f"{first[:200]}")
        for note in result.notes[1:]:
            print(f"{'':<{8 + width + 2 + 6 + 7}}{note[:200]}")
    print("=" * 78)
    failed = sum(result.status == "FAIL" for result in results)
    total = sum(result.seconds for result in results)
    print(f"{len(results)} flow runs, {failed} failed, {total:.0f} s in flows")
    return 1 if failed else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, help="scratch directory (kept); default: a temp dir")
    parser.add_argument("--modes", default="normal,slow")
    parser.add_argument("--flows", default=",".join(FLOWS))
    parser.add_argument("--src", type=Path, default=ROOT / "src",
                        help="the app's sources to run, such as an exported older release "
                             "(git archive v0.3.5 src | tar -x -C DIR); default: this checkout")
    parser.add_argument("--certs", type=Path,
                        help="where teddyCloud keeps its certificates between runs; making them "
                             "takes minutes, so CI should cache this folder; default: WORK/certs")
    parser.add_argument("--keep", action="store_true",
                        help="leave teddyCloud running after the last mode, for a look")
    args = parser.parse_args(argv)

    flows = [name for name in args.flows.split(",") if name]
    unknown = [name for name in flows if name not in FLOWS]
    if unknown:
        parser.error(f"unknown flows {unknown}; known: {', '.join(FLOWS)}")
    for tool in ("docker", "ffmpeg"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} is needed")

    work = (args.work or Path(tempfile.mkdtemp(prefix="tonie-e2e-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    print(f"work directory: {work}", flush=True)
    media = make_media(work)
    tc = TeddyCloud((args.certs or work / "certs").resolve(), make_cloud_certs(work))

    results: List[Result] = []
    modes = [mode for mode in args.modes.split(",") if mode]
    try:
        for number, mode in enumerate(modes):
            mode_dir = work / mode
            shutil.rmtree(mode_dir, ignore_errors=True)
            mode_dir.mkdir(parents=True)
            print(f"starting teddyCloud for the {mode} run", flush=True)
            tc.start(mode_dir)
            try:
                tc.lock_cloud()
                print("teddyCloud cloud access is off, its cloud host the stand-in; running flows",
                      flush=True)
                results += Run(mode, mode_dir, media, tc, args.src.resolve()).run(flows)
            finally:
                if not (args.keep and number == len(modes) - 1):
                    tc.stop(save_to=mode_dir)
    finally:
        if not args.keep:
            TeddyCloud.remove()
    return report(results) if results else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
