#!/usr/bin/env python3
"""Run the app against a real teddyCloud in Docker and check what it really wrote.

The unit tests talk to tests/fake_teddycloud.py, which keeps teddyCloud's
rules but not its HTTP server. Bugs in how requests reach teddyCloud - an
upload body split so that teddyCloud ends the file early, a form encoded the
way the fake accepts and the server does not - only show against the real
thing. This script starts teddyCloud 0.7.0 in a container and the app on the
host, walks the app's pages the way a browser does, and after every flow:

- reads teddyCloud's log for "Json parse error", "runtime error", ERROR lines,
  and every upload that started ("file_save_start ... Writing to") without the
  matching "multipart_handle ... Received file";
- reads the app's log for WARNING, ERROR, tracebacks and unexpected 4xx/5xx
  answers, its own and teddyCloud's (the app is started through a small
  wrapper that logs every teddyCloud call it makes);
- looks in teddyCloud itself - tag entries, the library, tonies.custom.json,
  the files in custom_img - for what the flow should have left there.

Every flow runs twice: as is, and with the upload body slowed down (20-50 ms
after every piece the app hands to the socket), which is closer to what a
real network does to the timing.

Run from the repository root, with the app's virtualenv:

    .venv/bin/python scripts/e2e_teddycloud.py --work /some/scratch/dir

Needs Docker, ffmpeg and the teddyCloud image (see IMAGE). The first start
of a fresh teddyCloud generates its certificates, which takes minutes; they
are kept in the work directory and reused. Only 127.0.0.1 is used: teddyCloud
is published on TC_PORT, the app on APP_PORT, and teddyCloud's cloud access
is switched off and read back before anything else happens. Should that not
read back as off, nothing runs. Everything started is stopped at the end.

What this cannot cover: the Tonies cloud (imports, downloads of official
audio - the import page is only checked to refuse cleanly while the cloud is
off), the tonies.com shop, a real Toniebox reading stickers and playing, and
public podcast feeds (the app fetches only public addresses, so a feed served
here is refused by design).

Usage: python3 scripts/e2e_teddycloud.py [--work DIR] [--modes normal,slow]
                                         [--flows pages,upload,...] [--keep]
"""

from __future__ import annotations

import argparse
import html
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tonie_studio.album import album_id  # noqa: E402
from tonie_studio.teddycloud import TeddyCloudClient  # noqa: E402

IMAGE = "ghcr.io/toniebox-reverse-engineering/teddycloud:0.7.0"
CONTAINER = "tc-e2e"
TC_PORT = 18791
APP_PORT = 18753
TC_URL = f"http://127.0.0.1:{TC_PORT}"
APP_URL = f"http://127.0.0.1:{APP_PORT}"

# Names the Tonies cloud goes by. teddyCloud's own setting keeps it from
# asking them; pointing them at the container's loopback makes sure.
CLOUD_HOSTS = ("prod.de.tbs.toys", "tbs2.tonie.cloud", "rtnl.bxcl.de", "ici.tonie.cloud")

# Test stickers end like real ones. Each gets a content folder of its own:
# the tag index lists one sticker per folder.
RUID_TAIL = "500304e0"

# A fresh teddyCloud makes its certificates first: minutes, on a slow machine many.
TC_START_TIMEOUT = 900
APP_START_TIMEOUT = 60
# An encode of the large file takes ffmpeg a while; slowed, its upload too.
JOB_TIMEOUT = 1200

# Lines teddyCloud writes at every start about certificates it has not got:
# a client certificate for the real cloud, which this setup never has.
TC_NOISE = re.compile(r"tls_adapter\.c|certs/client/")

# What the slowed run sleeps after each piece of an upload body, in seconds.
SLOW_PIECE = (0.02, 0.05)

FLOWS = ("pages", "upload", "edit", "copy_move", "replace", "batch", "sheet", "library",
         "checkup", "backup", "radio", "podcasts", "import")

# Started as the app instead of tonie_studio.main:app. It logs every call the
# app makes to teddyCloud, and in the slowed run it slows the upload body.
WRAPPER = '''"""tonie-studio, instrumented for scripts/e2e_teddycloud.py."""
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

from tonie_studio.main import app  # noqa: E402,F401
'''


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
    dump: Path


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
    tone(upload / "01 Eins.flac", 5, 440, "Eins", "E2E Artist", "E2E Upload Album", 1)
    tone(upload / "02 Zwei.mp3", 4, 550, "Zwei", "E2E Artist", "E2E Upload Album", 2,
         ("-c:a", "libmp3lame", "-b:a", "128k"))
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
        tone(folder / "02 Zweite.mp3", 3, frequency + 50, "Zweite", "E2E Batch", name, 2,
             ("-c:a", "libmp3lame", "-b:a", "128k"))
        picture(folder / "cover.jpg", "testsrc2=size=500x500:rate=1")
        batch_albums.append(folder)
    for name, frequency in (("Spare One", 800), ("Spare Two", 900)):
        folder = archive / "E2E Spare" / name
        tone(folder / "01 Nur.flac", 3, frequency, "Nur", "E2E Spare", name, 1)
        spare_albums.append(folder)

    dump = media / "dumps" / "E2E0FFFF500304E0.nfc"
    if not dump.exists():
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text("Filetype: Flipper NFC device\nUID: E0 04 03 50 FF FF 0E 2E\n")
    return Media(
        upload_album=[(f"E2E Upload Album/{path.name}", path) for path in sorted(upload.iterdir())],
        upload_seconds=5 + 4 + 400,
        cover=cover, cover2=media / "cover2.jpg", archive=archive,
        batch_albums=batch_albums, spare_albums=spare_albums, dump=dump,
    )


# --- the processes --------------------------------------------------------


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


class TeddyCloud:
    """The container, its log, and direct reads of what it holds."""

    def __init__(self, certs: Path):
        self.certs = certs
        self.http = requests.Session()
        self.log_lines = 0

    def start(self) -> None:
        docker("rm", "-f", CONTAINER, check=False)
        if not port_free(TC_PORT):
            raise SystemExit(f"port {TC_PORT} is taken; stop whatever listens there")
        self.certs.mkdir(parents=True, exist_ok=True)
        hosts = [arg for host in CLOUD_HOSTS for arg in ("--add-host", f"{host}:127.0.0.1")]
        # Not --rm: should teddyCloud die, its log is the evidence.
        docker("run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{TC_PORT}:80",
               *hosts, "-v", f"{self.certs}:/teddycloud/certs", IMAGE)
        deadline = time.time() + TC_START_TIMEOUT
        while time.time() < deadline:
            try:
                if self.http.get(f"{TC_URL}/api/settings/get/cloud.enabled", timeout=3).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(2)
        else:
            raise SystemExit("teddyCloud did not answer in time")
        self.log_lines = len(self.full_log())

    def disable_cloud(self) -> None:
        """Switch teddyCloud's cloud access off and read it back; nothing runs otherwise."""
        # The answer to a set is empty even when it worked (0.7.0 declares a
        # length of 0), so only reading the value back tells.
        self.http.post(f"{TC_URL}/api/settings/set/cloud.enabled", data="false", timeout=10)
        value = self.http.get(f"{TC_URL}/api/settings/get/cloud.enabled", timeout=10).text.strip()
        if value != "false":
            raise SystemExit(f"cannot verify teddyCloud's cloud is off (reads {value!r}); "
                             "stopping before any test")

    def stop(self, save_to: Optional[Path]) -> None:
        if save_to is not None:
            try:
                save_to.write_text("\n".join(self.full_log()) + "\n")
            except (OSError, subprocess.SubprocessError):
                pass
        docker("stop", "-t", "5", CONTAINER, check=False)
        docker("rm", "-f", CONTAINER, check=False)

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


class App:
    """The app as a process on the host, its log, and a browser-like session."""

    def __init__(self, work: Path, archive: Path, slow: bool, src: Path):
        self.work = work
        self.src = src
        self.archive = archive
        self.slow = slow
        self.data = work / "data"
        self.log_path = work / "app.log"
        self.log_offset = 0
        self.process: Optional[subprocess.Popen] = None
        self.http = requests.Session()
        self.http.headers["Accept-Language"] = "en"

    def start(self) -> None:
        if not port_free(APP_PORT):
            raise SystemExit(f"port {APP_PORT} is taken; stop whatever listens there")
        self.data.mkdir(parents=True, exist_ok=True)
        (self.work / "tmp").mkdir(exist_ok=True)
        wrapper_dir = self.work / "wrapper"
        wrapper_dir.mkdir(exist_ok=True)
        (wrapper_dir / "e2e_app.py").write_text(WRAPPER)
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": os.pathsep.join([str(self.src), str(wrapper_dir)]),
            "TS_TEDDYCLOUD_URL": TC_URL,
            "TS_DATA_DIR": str(self.data),
            "TS_ARCHIVE_DIR": str(self.archive),
            "TMPDIR": str(self.work / "tmp"),
            "E2E_SLOW_UPLOAD": "1" if self.slow else "0",
            "E2E_SLOW_PIECE": ",".join(str(value) for value in SLOW_PIECE),
        })
        python = ROOT / ".venv" / "bin" / "python"
        self.process = subprocess.Popen(
            [str(python if python.exists() else sys.executable), "-m", "uvicorn", "--factory",
             "e2e_app:app", "--host", "127.0.0.1", "--port", str(APP_PORT)],
            cwd=ROOT, env=env, stdout=self.log_path.open("w"), stderr=subprocess.STDOUT)
        deadline = time.time() + APP_START_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"the app exited at start; see {self.log_path}")
            try:
                if self.http.get(f"{APP_URL}/health", timeout=3).ok:
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

    def new_log(self) -> List[str]:
        with self.log_path.open("rb") as handle:
            handle.seek(self.log_offset)
            data = handle.read()
        self.log_offset += len(data)
        return data.decode("utf-8", errors="replace").splitlines()

    def request(self, method: str, path: str, expect: Sequence[int] = (200,),
                **kwargs) -> requests.Response:
        kwargs.setdefault("allow_redirects", False)
        kwargs.setdefault("timeout", 600)
        response = self.http.request(method, f"{APP_URL}{path}", **kwargs)
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


def location(response: requests.Response) -> str:
    return response.headers.get("location", "")


# --- the run --------------------------------------------------------------


class Run:
    """One pass over the flows against one fresh teddyCloud and app."""

    def __init__(self, mode: str, work: Path, media: Media, tc: TeddyCloud, src: Path):
        self.mode = mode
        self.media = media
        self.tc = tc
        self.app = App(work, media.archive, slow=(mode == "slow"), src=src)
        self.client = TeddyCloudClient(TC_URL)
        self.stickers = 0
        # What one flow leaves for the next: stickers, targets, models.
        self.state: Dict[str, object] = {}
        self.results: List[Result] = []

    # Stickers, made as the box would leave them: a content folder with an
    # empty entry. Uploaded through the app's own client, so this is an
    # upload path that is checked too.

    def new_sticker(self) -> str:
        self.stickers += 1
        ruid = f"e2e{self.stickers:05x}{RUID_TAIL}"
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

    def run(self, flows: Sequence[str]) -> List[Result]:
        self.app.start()
        self.app.new_log()
        self.tc.new_log()
        try:
            for name in flows:
                self.flow(name, getattr(self, f"flow_{name}"))
        finally:
            self.app.stop()
        return self.results

    def flow(self, name: str, work: Callable[[Result], Optional[Sequence[Tuple[str, int]]]]) -> None:
        result = Result(self.mode, name)
        started = time.time()
        expected: Sequence[Tuple[str, int]] = ()
        try:
            expected = work(result) or ()
        except FlowFailed as failure:
            result.status = "FAIL"
            result.notes.append(str(failure))
        except Exception as error:  # a broken flow is a finding, not the end of the run
            result.status = "FAIL"
            result.notes.append(f"{type(error).__name__}: {error}")
            result.notes.append(traceback.format_exc(limit=4).strip().splitlines()[-3])
        # Let the job threads and teddyCloud's log catch up.
        time.sleep(1.5)
        self.inspect_logs(result, expected)
        result.seconds = time.time() - started
        self.results.append(result)
        print(f"  [{self.mode}] {name}: {result.status} ({result.seconds:.0f} s)", flush=True)
        for note in result.notes:
            print(f"      {note}", flush=True)

    def inspect_logs(self, result: Result, expected: Sequence[Tuple[str, int]]) -> None:
        """Turn what the two logs say about this flow into notes, and FAIL where it counts."""
        problems: List[str] = []
        warnings: List[str] = []

        def allowed(request: str, status: int) -> bool:
            return any(status == want and request == pattern for pattern, want in expected)

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
            if "Json parse error" in line or "runtime error" in line or line.startswith("ERROR"):
                problems.append(f"teddyCloud: {line.strip()}")
            elif line.startswith("WARN"):
                warnings.append(f"teddyCloud: {line.strip()}")
        for name in waiting:
            problems.append(f"teddyCloud: upload of {name} started (file_save_start) "
                            "and never finished (no 'Received file')")
        down = self.tc.gone()
        if down:
            problems.append(f"teddyCloud container is not running any more: {down}")

        for line in self.app.new_log():
            access = re.search(r'"(GET|POST) (\S+) HTTP/1\.1" (\d{3})', line)
            if access:
                request = f"{access.group(1)} {access.group(2).split('?')[0]}"
                status = int(access.group(3))
                if status >= 400 and not allowed(request, status):
                    problems.append(f"app answered {status} to {request}")
                continue
            if "E2E-TC" in line:
                call = re.search(r"E2E-TC (GET|POST) (\S+) -> (\d+)", line)
                if call and allowed(f"TC {call.group(1)} {call.group(2)}", int(call.group(3))):
                    continue
                if re.search(r"\bWARNING\b", line):
                    problems.append(f"app->teddyCloud: {line.split('E2E-TC', 1)[1].strip()}")
                continue
            if "Traceback" in line or re.search(r"\b(ERROR|WARNING|CRITICAL)\b", line):
                problems.append(f"app: {line.strip()}")

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

    def album_form(self, identifier: str) -> Dict[str, object]:
        """The album page's form as a browser would send it, every track ticked."""
        page = self.app.get(f"/album/{identifier}").text
        order = re.search(r'name="order" id="order" value="([^"]*)"', page)
        numbers = re.findall(r'name="selected" value="(\d+)"', page)
        titles = [html.unescape(value) for value in re.findall(r'name="titles" value="([^"]*)"', page)]
        if not numbers:
            raise FlowFailed(f"album page {identifier} shows no tracks")
        return {"order": order.group(1) if order else "", "selected": numbers, "titles": titles,
                "parts": "1", "cover_index": "0", "cover_zoom": "1.0", "cover_x": "0", "cover_y": "0"}

    def encode_album(self, identifier: str, series: str, episode: str,
                     replace: bool = False) -> Tuple[str, dict]:
        """Start the album page's encode and wait for it; returns the job id and its state."""
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
        state = self.app.wait_job(job_id)
        if state["state"] != "done":
            raise FlowFailed(f"encode job failed: {state.get('error')}")
        return job_id, state

    def archive_id(self, folder: Path) -> str:
        identifier = "a" + album_id(self.media.archive, folder)
        if identifier not in self.app.get("/").text:
            raise FlowFailed(f"album list does not show {folder.name} as {identifier}")
        return identifier

    # --- the flows ----------------------------------------------------------

    def flow_pages(self, result: Result):
        for path in ("/", "/tonies", "/library", "/sheet", "/checkup", "/backup", "/import",
                     "/import/names", "/podcasts", "/radio", "/listening"):
            self.app.get(path)

    def flow_upload(self, result: Result):
        ruid = self.new_sticker()
        files = [("files", (name, path.open("rb"))) for name, path in self.media.upload_album]
        try:
            response = self.app.post("/upload", expect=(200,), files=files,
                                     headers={"X-Upload": "1"})
        finally:
            for _field, (_name, handle) in files:
                handle.close()
        following = response.json()["next"]
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
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            jobs = self.app.get(f"/api/group/{group}").json()["jobs"]
            if all(job["state"] in ("done", "failed") for job in jobs):
                break
            time.sleep(1)
        else:
            raise FlowFailed("batch did not finish")
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
            made.append((holders[0], job["model"]))
            if not tags.get(holders[0], {}).get("exists"):
                result.notes.append(f"tag index says {holders[0]} audio does not exist")
        self.check_staging_empty()
        self.state["batch"] = made

    def flow_sheet(self, result: Result):
        self.app.get("/sheet")
        models = [str(self.state["upload_model"])] + [model for _ruid, model in self.state["batch"]]
        answer = self.app.post("/sheet", data={
            "picks": [f"1:fill:{model}" for model in models], "back": "1", "back_tracks": "1"})
        job_id = location(answer).rsplit("/", 1)[-1]
        self.app.get(f"/sheet/print/{job_id}")
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            state = self.app.get(f"/api/sheet/print/{job_id}").json()
            if state["state"] in ("done", "failed"):
                break
            time.sleep(1)
        if state["state"] != "done":
            raise FlowFailed(f"sheet failed: {state.get('problem')}")
        if state.get("skipped"):
            result.notes.append(f"sheet skipped {state['skipped']}")
            result.status = "FAIL"
        for path in (f"/sheet/print/{job_id}/pdf", "/sheet/calibration", "/sheet/overview"):
            pdf = self.app.get(path).content
            if not pdf.startswith(b"%PDF") or len(pdf) < 2000:
                raise FlowFailed(f"{path} is no PDF worth printing ({len(pdf)} bytes)")
            result.notes.append(f"{path}: {len(pdf)} bytes")

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
        self.state["library_ruid"] = ruid

    def flow_checkup(self, result: Result):
        loose = [str(self.state["upload_ruid"]), str(self.state["moved_ruid"]),
                 str(self.state["library_ruid"])]
        for ruid in loose:
            self.tc.set_tag(ruid, nocloud="false")
        orphan = "TS-9990"
        upsert = self.tc.http.post(f"{TC_URL}/api/toniesCustomJsonUpsert", timeout=30, json={
            "model": orphan, "series": "E2E Orphan", "episodes": "Left over", "tracks": []})
        if upsert.text.strip() != "OK":
            raise FlowFailed(f"cannot make an orphan model: {upsert.text!r}")
        page = self.app.get("/checkup").text
        offered = set(re.findall(r'name="fix" value="(\w+)">\s*<input type="hidden" '
                                 r'name="target" value="([^"]+)"', page))
        broken = [pair for pair in offered
                  if pair[0] in ("empty_sticker", "repair_source", "repair_model")]
        if broken:
            raise FlowFailed(f"checkup finds stickers or models broken by the flows: {broken}")
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
        ruid, model_id = self.state["batch"][0]
        model = self.tc.models()[model_id]
        cover_path = model.get("pic") or ""
        cover = self.tc.fetch(cover_path).content
        if f"covers/{cover_path.rsplit('/', 1)[-1]}" not in names:
            raise FlowFailed(f"backup has no cover for {model_id}: {names}")
        if not any(entry.get("model") == model_id for entry in saved_models):
            raise FlowFailed(f"backup has no model {model_id}")

        # What a lost volume would do to one tonie: model and cover gone.
        self.tc.http.post(f"{TC_URL}/api/toniesCustomJsonDelete", json={"models": [model_id]},
                          timeout=30)
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
        result.status = "N/T"
        result.notes.append("not testable locally: the local feed is refused before any fetch "
                            f"({page_text(answer.text)[:160]})")
        return [("POST /podcasts", 400)]

    def flow_import(self, result: Result):
        self.app.get("/import")
        answer = self.app.post("/import/files", expect=(400, 409), data={
            "per_hour": "10", "jitter": "10", "max_failures": "3"},
            files={"files": (f"dumps/{self.media.dump.name}", self.media.dump.read_bytes(),
                             "application/octet-stream")})
        state = self.app.get("/api/import").json()
        if state.get("phase") is not None:
            raise FlowFailed(f"an import run was created with the cloud off: {state}")
        result.notes.append(f"refused with {answer.status_code}: {page_text(answer.text)[:160]}")
        contacted = [line for line in self.tc.full_log()
                     if any(host in line for host in CLOUD_HOSTS) and "Connecting" in line]
        if contacted:
            raise FlowFailed(f"teddyCloud tried to reach the Tonies cloud: {contacted[:3]}")
        return [("POST /import/files", answer.status_code)]


# --- report ---------------------------------------------------------------


def report(results: List[Result]) -> int:
    width = max(len(result.flow) for result in results)
    print("\n" + "=" * 72)
    print(f"{'mode':<7} {'flow':<{width}}  status  secs  notes")
    print("-" * 72)
    for result in results:
        first = result.notes[0] if result.notes else ""
        print(f"{result.mode:<7} {result.flow:<{width}}  {result.status:<6} {result.seconds:5.0f}  "
              f"{first[:200]}")
        for note in result.notes[1:]:
            print(f"{'':<{8 + width + 2 + 6 + 7}}{note[:200]}")
    print("=" * 72)
    failed = sum(result.status == "FAIL" for result in results)
    print(f"{len(results)} flow runs, {failed} failed")
    return 1 if failed else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, help="scratch directory (kept); default: a temp dir")
    parser.add_argument("--modes", default="normal,slow")
    parser.add_argument("--flows", default=",".join(FLOWS))
    parser.add_argument("--src", type=Path, default=ROOT / "src",
                        help="the app's sources to run, such as an exported older release "
                             "(git archive v0.3.5 src | tar -x -C DIR); default: this checkout")
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
    tc = TeddyCloud(work / "certs")

    results: List[Result] = []
    modes = [mode for mode in args.modes.split(",") if mode]
    for number, mode in enumerate(modes):
        mode_dir = work / mode
        shutil.rmtree(mode_dir, ignore_errors=True)
        mode_dir.mkdir(parents=True)
        print(f"starting teddyCloud for the {mode} run", flush=True)
        tc.start()
        try:
            tc.disable_cloud()
            print("teddyCloud cloud access is off; running flows", flush=True)
            results += Run(mode, mode_dir, media, tc, args.src.resolve()).run(flows)
        finally:
            if not (args.keep and number == len(modes) - 1):
                tc.stop(save_to=mode_dir / "teddycloud.log")
    return report(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
