"""Backs up and restores what makes a tonie a tonie.

teddyCloud keeps the custom models, their covers and every tag's assignment in
its own storage, and this app keeps the playing times, the listening log and
which album each tonie was made from on its volume. Lose either and the audio is still there to rebuild, but every name
typed, every cover cropped and every sticker paired with its album is gone.

A backup is one zip: the models and tag assignments as JSON, the covers as the
image files they are, and the app's own records. The tafs are deliberately not
in it. They are rebuilt from the archive, and including them would turn a file
of a few megabytes into gigabytes nobody wants to download.

Restoring is additive and shown before it runs. Models and covers in the backup
are written back, tags the box knows are pointed where they pointed, and
nothing that exists now but is missing from the backup is deleted: a restore
should undo a loss, not cause a second one.
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .albumnotes import MADE_FILE, add_made
from .calibration import FILE_NAME as CALIBRATION_FILE
from .encode import list_tafs
from .i18n import Problem
from .library import is_library, played, same
from .listening import FILE_NAME as LISTENING_FILE
from .models import WRITING, Model, fetch_cover, is_cover_name
from .podcast import FILE_NAME as PODCASTS_FILE
from .podcast import Subscription, add_subscription, load_subscriptions
from .runtimes import FILE_NAME as TIMES_FILE
from .runtimes import add_missing
from .store import from_record, sweep
from .tags import TonieTag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

FORMAT = 1

# Far above any real backup - a few hundred kilobytes of covers per tonie -
# and far below what would hurt the process holding it in memory.
LARGEST_BACKUP = 64 * 1024 * 1024

# Where an uploaded backup waits between its preview and the confirmation,
# and how long before housekeeping takes one nobody confirmed.
PARKED_FOLDER = "restore"
PARKED_FOR = 86400.0
# The same for the backup unpacked; its covers do not shrink when packed, so
# a real one is hardly larger than its file.
LARGEST_UNPACKED = 256 * 1024 * 1024

# What a restore step does, and what it does it to. The values name texts in
# the language files (restore.action.*, restore.what.*); changing one means
# renaming its text there as well.
SAME = "same"
NEW = "new"
CHANGES = "changes"
REPLACES = "replaces"
SKIPPED = "skipped"
KEPT = "kept"
FAILED = "failed"
ACTIONS = (SAME, NEW, CHANGES, REPLACES, SKIPPED, KEPT, FAILED)

MODEL, COVER, TAG, TIMES, LISTENING, MADE = "model", "cover", "tag", "times", "listening", "made"
PODCASTS, CALIBRATION = "podcasts", "calibration"
KINDS = (MODEL, COVER, TAG, TIMES, LISTENING, MADE, PODCASTS, CALIBRATION)

# The parts of a backup as the backup page lists them; texts in backup.part.*.
PART_MODELS, PART_TAGS, PART_COVERS = "models", "tags", "covers"
PARTS = (PART_MODELS, PART_TAGS, PART_COVERS, TIMES, LISTENING, MADE, PODCASTS, CALIBRATION)


class BackupError(Problem, ValueError):
    """A file that is not a backup this app can restore."""


@dataclass
class Backup:
    manifest: Dict
    models: List[Dict]
    tags: List[Dict]
    covers: Dict[str, bytes]
    times: Optional[bytes]
    listening: Optional[bytes]
    # Which album each tonie was made from; absent from backups made before.
    made: Optional[bytes] = None
    # Podcast subscriptions and the printer calibration; absent from older backups.
    podcasts: Optional[bytes] = None
    calibration: Optional[bytes] = None


@dataclass(frozen=True)
class Step:
    """One thing a restore would do, as the confirmation page lists it."""

    what: str
    name: str
    action: str
    note: str = ""      # key of a text explaining the action, if it needs one
    detail: str = ""    # what went wrong, as the failing call said it; not translated


@dataclass(frozen=True)
class Part:
    """One kind of content in a backup, as the backup page lists it."""

    what: str
    count: int
    size: int


def describe_backup(client: TeddyCloudClient, data_dir: Path) -> List[Part]:
    """What a backup made now would hold, and about how large each part is.

    Cover sizes come from teddyCloud's listing of custom_img rather than from
    the pictures themselves: they are most of the backup, and fetching them to
    measure them would move it on every page view. Pictures do not shrink in
    the zip and the JSON parts are small, so the sum is close to the file.
    """
    models = client.custom_tonies()
    tags = [_tag_record(tag) for tag in list_tags(client)]
    wanted = {Model.from_json(model).cover for model in models} - {None}
    listed = {entry.get("name"): int(entry.get("size") or 0)
              for entry in client.list_dir("/", special="custom_img")
              if not entry.get("isDir")}
    covers = [listed[name] for name in wanted if name in listed]

    parts = [
        Part(PART_MODELS, len(models), len(_json(models).encode("utf-8"))),
        Part(PART_TAGS, len(tags), len(_json(tags).encode("utf-8"))),
        Part(PART_COVERS, len(covers), sum(covers)),
    ]
    times = data_dir / TIMES_FILE
    parts.append(Part(TIMES, len(_entries(times, json_object=True)), _size(times)))
    listening = data_dir / LISTENING_FILE
    parts.append(Part(LISTENING, len(_entries(listening)), _size(listening)))
    made = data_dir / MADE_FILE
    parts.append(Part(MADE, len(_entries(made, json_object=True)), _size(made)))
    subscriptions = _saved_subscriptions(data_dir)
    parts.append(Part(PODCASTS, len(subscriptions),
                      len(_json(subscriptions).encode("utf-8")) if subscriptions else 0))
    calibration = data_dir / CALIBRATION_FILE
    parts.append(Part(CALIBRATION, 1 if calibration.is_file() else 0, _size(calibration)))
    return parts


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _entries(path: Path, json_object: bool = False) -> list:
    """The entries of a data file: keys of a JSON object, or non-empty lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if json_object:
        try:
            loaded = json.loads(text)
        except ValueError:
            return []
        return list(loaded) if isinstance(loaded, dict) else []
    return [line for line in text.splitlines() if line.strip()]


def create_backup(client: TeddyCloudClient, data_dir: Path) -> bytes:
    """Everything except the audio, as the bytes of a zip file."""
    models = client.custom_tonies()
    tags = [_tag_record(tag) for tag in list_tags(client)]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", _json({
            "format": FORMAT,
            "app": "tonie-studio",
            "version": os.environ.get("TS_VERSION", "dev"),
            "created": datetime.now(timezone.utc).isoformat(),
            "models": len(models),
            "tags": len(tags),
        }))
        archive.writestr("models.json", _json(models))
        archive.writestr("tags.json", _json(tags))

        names = sorted({Model.from_json(model).cover for model in models} - {None})
        for name, data in zip(names, _fetch_covers(client, names), strict=True):
            if data is not None:
                archive.writestr(f"covers/{name}", data)

        for file_name in (TIMES_FILE, LISTENING_FILE, MADE_FILE, CALIBRATION_FILE):
            path = data_dir / file_name
            if path.is_file():
                archive.write(path, file_name)
        subscriptions = _saved_subscriptions(data_dir)
        if subscriptions:
            archive.writestr(PODCASTS_FILE, _json(subscriptions))
    return buffer.getvalue()


def _saved_subscriptions(data_dir: Path) -> List[Dict]:
    """The subscriptions as a backup keeps them: without how the last refresh went."""
    return [{key: value for key, value in asdict(subscription).items()
             if key not in ("error", "error_key", "error_params")}
            for subscription in load_subscriptions(data_dir)]


# How many covers are fetched at once for a backup. One after another,
# hundreds of them outlast the gateway's wait for the download to start;
# teddyCloud serves a few at a time without trouble.
COVER_FETCHES = 8


def _fetch_covers(client: TeddyCloudClient, names: List[str]) -> List[Optional[bytes]]:
    """Each cover's bytes, in the order of ``names``; None for one that cannot be read."""
    def fetch(name: str) -> Optional[bytes]:
        try:
            return fetch_cover(client, name)
        except TeddyCloudError as error:
            # A model whose picture is gone still has a name worth keeping.
            log.warning("backup: cover %s cannot be read: %s", name, error)
            return None

    with ThreadPoolExecutor(max_workers=COVER_FETCHES, thread_name_prefix="backup") as pool:
        return list(pool.map(fetch, names))


def read_backup(data: bytes) -> Backup:
    """Unpack a backup, refusing anything that is not one."""
    if len(data) > LARGEST_BACKUP:
        raise BackupError("backup.too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise BackupError("backup.not_zip") from error

    with archive:
        # Refused before anything is read: the size limit above is on the
        # packed file, and a small zip can unpack to gigabytes in memory.
        if sum(info.file_size for info in archive.infolist()) > LARGEST_UNPACKED:
            raise BackupError("backup.too_large")
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise BackupError("backup.not_ours")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            models = json.loads(archive.read("models.json").decode("utf-8"))
            tags = json.loads(archive.read("tags.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise BackupError("backup.damaged", detail=str(error)) from error
        if not (isinstance(manifest, dict) and isinstance(models, list)
                and isinstance(tags, list)):
            raise BackupError("backup.damaged", detail="unexpected structure")
        if manifest.get("format") != FORMAT:
            raise BackupError("backup.unknown_format", format=manifest.get("format"))

        covers = {}
        for name in names:
            if not name.startswith("covers/"):
                continue
            base = name[len("covers/"):]
            if not is_cover_name(base):
                continue
            # A name with a path in it would write somewhere other than
            # custom_img; a backup has no reason to contain one.
            if not base or "/" in base or "\\" in base or base.startswith("."):
                continue
            covers[base] = archive.read(name)

        return Backup(
            manifest=manifest,
            models=[model for model in models if isinstance(model, dict) and model.get("model")],
            tags=[tag for tag in tags if isinstance(tag, dict) and tag.get("ruid")],
            covers=covers,
            times=archive.read(TIMES_FILE) if TIMES_FILE in names else None,
            listening=archive.read(LISTENING_FILE) if LISTENING_FILE in names else None,
            made=archive.read(MADE_FILE) if MADE_FILE in names else None,
            podcasts=archive.read(PODCASTS_FILE) if PODCASTS_FILE in names else None,
            calibration=archive.read(CALIBRATION_FILE) if CALIBRATION_FILE in names else None,
        )


def plan_restore(client: TeddyCloudClient, backup: Backup, data_dir: Path) -> List[Step]:
    """What a restore would do, without doing any of it."""
    return _restore(client, backup, data_dir, write=False)


def apply_restore(client: TeddyCloudClient, backup: Backup, data_dir: Path) -> List[Step]:
    """Restore, and return what was done.

    Under the lock every model write shares: a model is written whole, and
    one changed meanwhile would be overwritten with what was read before.
    """
    with WRITING:
        return _restore(client, backup, data_dir, write=True)


def _restore(client: TeddyCloudClient, backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    steps: List[Step] = []

    current_models = {str(model.get("model", "")).lower(): model
                      for model in client.custom_tonies()}
    for model in backup.models:
        existing = current_models.get(str(model["model"]).lower())
        action = NEW if existing is None else (SAME if existing == model else CHANGES)
        steps.append(_attempt(write and action != SAME, MODEL, _model_label(model), action,
                              lambda model=model: client.upsert_custom_tonie(model)))

    for name, data in sorted(backup.covers.items()):
        try:
            existing_cover: Optional[bytes] = fetch_cover(client, name)
        except TeddyCloudError:
            existing_cover = None
        action = NEW if existing_cover is None else (SAME if existing_cover == data else REPLACES)
        steps.append(_attempt(
            write and action != SAME, COVER, name, action,
            lambda name=name, data=data: client.upload(io.BytesIO(data), name, path="/",
                                                       special="custom_img")))

    known = {tag.ruid: tag for tag in list_tags(client)}
    present = _library_sources(client)
    for record in backup.tags:
        ruid = record["ruid"]
        label = record.get("label") or ruid
        tag = known.get(ruid)
        if tag is None:
            steps.append(Step(TAG, label, SKIPPED, "restore.note.tag_unread"))
            continue

        wanted = _restorable(record)
        current = _tag_record(tag)
        changed = {field: value for field, value in wanted.items() if current.get(field) != value}
        note = _missing_note(record.get("source") or "", present)
        steps.append(_attempt(write and bool(changed), TAG, label,
                              CHANGES if changed else SAME,
                              lambda ruid=ruid, changed=changed: client.set_tag(ruid, **changed),
                              note))

    steps += _restore_times(backup, data_dir, write)
    steps += _restore_listening(backup, data_dir, write)
    steps += _restore_made(backup, data_dir, write)
    steps += _restore_podcasts(backup, data_dir, write)
    steps += _restore_only_where_none(backup.calibration, data_dir, CALIBRATION,
                                      CALIBRATION_FILE, write)
    if write:
        log.info("restored a backup from %s", backup.manifest.get("created"))
    return steps


def _attempt(write: bool, what: str, name: str, action: str, apply, note: str = "") -> Step:
    """Do one restore step, and report a failure as a step rather than raising.

    Every item stands on its own. A restore that stops at the first refusal
    leaves nobody knowing what came back; this one goes on and lists it.
    """
    if not write:
        return Step(what, name, action, note)
    try:
        apply()
    except (TeddyCloudError, ValueError) as error:
        log.warning("restore: %s %s failed: %s", what, name, error)
        return Step(what, name, FAILED, "restore.note.failed", str(error))
    return Step(what, name, action, note)


def _restorable(record: Dict) -> Dict:
    """The fields of a saved tag that a restore may write.

    Only what the backup actually holds: an empty source, a missing model or a
    cloud fallback that was still on are not things to bring back, and writing
    them would empty a sticker, take away its name or let it ask the cloud
    again - a restore causing the very loss it is meant to undo.
    """
    wanted: Dict = {}
    if record.get("source"):
        wanted["source"] = record["source"]
        wanted["live"] = bool(record.get("live"))
    if record.get("tonie_model"):
        wanted["tonie_model"] = record["tonie_model"]
    if record.get("nocloud"):
        wanted["nocloud"] = True
    return wanted


def _restore_times(backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    """Playing times the backup knows and this volume does not are added."""
    if backup.times is None:
        return []
    try:
        saved = json.loads(backup.times.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return [Step(TIMES, TIMES_FILE, SKIPPED, "restore.note.unreadable")]
    changed, had_times = add_missing(data_dir, saved, write=write)
    if not changed:
        return [Step(TIMES, TIMES_FILE, SAME)]
    return [Step(TIMES, TIMES_FILE, CHANGES if had_times else NEW)]


def _restore_made(backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    """Album records the backup knows and this volume does not are added."""
    if backup.made is None:
        return []
    try:
        saved = json.loads(backup.made.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return [Step(MADE, MADE_FILE, SKIPPED, "restore.note.unreadable")]
    changed, had_records = add_made(data_dir, saved, write=write)
    if not changed:
        return [Step(MADE, MADE_FILE, SAME)]
    return [Step(MADE, MADE_FILE, CHANGES if had_records else NEW)]


def _restore_podcasts(backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    """Subscriptions the backup has and this volume does not are added.

    One here with the same id stays as it is: it was set up or changed since.
    """
    if backup.podcasts is None:
        return []
    try:
        saved = [from_record(Subscription, entry)
                 for entry in json.loads(backup.podcasts.decode("utf-8"))
                 if isinstance(entry, dict) and entry.get("id") and entry.get("feed")]
    except (ValueError, UnicodeDecodeError, TypeError):
        return [Step(PODCASTS, PODCASTS_FILE, SKIPPED, "restore.note.unreadable")]
    here = {subscription.id for subscription in load_subscriptions(data_dir)}
    missing = [subscription for subscription in saved if subscription.id not in here]
    if not missing:
        return [Step(PODCASTS, PODCASTS_FILE, SAME)]
    if write:
        for subscription in missing:
            add_subscription(data_dir, subscription)
    return [Step(PODCASTS, PODCASTS_FILE, CHANGES if here else NEW)]


def _restore_only_where_none(saved: Optional[bytes], data_dir: Path, what: str,
                             file_name: str, write: bool) -> List[Step]:
    """A record that comes back only where there is none: the one here is newer."""
    if saved is None:
        return []
    path = data_dir / file_name
    if path.is_file():
        return [Step(what, file_name, SAME if path.read_bytes() == saved else KEPT)]
    if write:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(saved)
    return [Step(what, file_name, NEW)]


def _restore_listening(backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    """The log comes back only where there is none: an older log over a newer
    one would throw away whatever was heard in between."""
    if backup.listening is None:
        return []
    path = data_dir / LISTENING_FILE
    current = path.read_bytes() if path.is_file() else b""
    if current == backup.listening:
        return [Step(LISTENING, LISTENING_FILE, SAME)]
    if current.strip():
        return [Step(LISTENING, LISTENING_FILE, KEPT, "restore.note.log_kept")]
    if write:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(backup.listening)
    return [Step(LISTENING, LISTENING_FILE, NEW)]


def _library_sources(client: TeddyCloudClient) -> Optional[set]:
    try:
        return played(taf.source for taf in list_tafs(client, strict=True))
    except TeddyCloudError:
        return None


def _missing_note(source: str, present: Optional[set]) -> str:
    if not is_library(source):
        return ""
    if present is None:
        return "restore.note.library_unreadable"
    if same(source) not in present:
        return "restore.note.taf_missing"
    return ""


def _tag_record(tag: TonieTag) -> Dict:
    """A tag as the backup keeps it; the field names are the backup format."""
    return {
        "ruid": tag.ruid,
        "label": tag.label,
        "source": tag.source,
        "tonie_model": tag.model,
        "nocloud": tag.nocloud,
        "live": tag.live,
    }


def _model_label(model: Dict) -> str:
    described = Model.from_json(model)
    return f"{described.id}: {described.label}"


def _json(value) -> str:
    # Umlauts written as themselves: a backup someone opens to check should
    # read "Siebenschl\u00e4fer", and the zip stores UTF-8 either way.
    return json.dumps(value, ensure_ascii=False, indent=1)


def park(data_dir: Path, data: bytes) -> str:
    """Keep an uploaded backup until it is confirmed; returns the token naming it.

    The confirmation then acts on exactly the file that was previewed.
    """
    token = uuid.uuid4().hex
    folder = data_dir / PARKED_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{token}.zip").write_bytes(data)
    return token


def take_parked(data_dir: Path, token: str) -> Optional[bytes]:
    """The backup parked under ``token``, removed as it is handed out; None if gone.

    Taken rather than read, so a restore can only be applied once.
    """
    if not token.isalnum():
        return None
    parked = data_dir / PARKED_FOLDER / f"{token}.zip"
    try:
        return parked.read_bytes()
    except FileNotFoundError:
        return None
    finally:
        parked.unlink(missing_ok=True)


def clear_parked(data_dir: Path, older_than: float = PARKED_FOR) -> None:
    """Drop backups uploaded for a preview that was never confirmed."""
    sweep(data_dir / PARKED_FOLDER, "*.zip", older_than)
