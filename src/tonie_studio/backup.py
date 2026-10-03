"""Backs up and restores what makes a tonie a tonie.

teddyCloud keeps the custom models, their covers and every tag's assignment in
its own storage, and this app keeps the playing times and the listening log on
its volume. Lose either and the audio is still there to rebuild, but every name
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
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .encode import list_tafs
from .listening import FILE_NAME as LISTENING_FILE
from .runtimes import FILE_NAME as TIMES_FILE
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import SYSTEM_RUID

log = logging.getLogger(__name__)

FORMAT = 1
CUSTOM_IMG = "/custom_img/"

# Far above any real backup - a few hundred kilobytes of covers per tonie -
# and far below what would hurt the process holding it in memory.
LARGEST_BACKUP = 64 * 1024 * 1024

SAME = "gleich"
NEW = "neu"
CHANGES = "ändert"
REPLACES = "ersetzt"
SKIPPED = "übersprungen"
KEPT = "behalten"


class BackupError(ValueError):
    """A file that is not a backup this app can restore."""


@dataclass
class Backup:
    manifest: Dict
    models: List[Dict]
    tags: List[Dict]
    covers: Dict[str, bytes]
    times: Optional[bytes]
    listening: Optional[bytes]


@dataclass(frozen=True)
class Step:
    """One thing a restore would do, as the confirmation page lists it."""

    what: str
    name: str
    action: str
    note: str = ""


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
    tags = [_tag_record(entry) for entry in client.tags() if _is_tag(entry)]
    wanted = {_cover_name(model) for model in models} - {None}
    listed = {entry.get("name"): int(entry.get("size") or 0)
              for entry in client.list_dir("/", special="custom_img")
              if not entry.get("isDir")}
    covers = [listed[name] for name in wanted if name in listed]

    parts = [
        Part("Modelle", len(models), len(_json(models).encode("utf-8"))),
        Part("Sticker", len(tags), len(_json(tags).encode("utf-8"))),
        Part("Cover", len(covers), sum(covers)),
    ]
    times = data_dir / TIMES_FILE
    parts.append(Part("Spielzeiten", len(_entries(times, json_object=True)), _size(times)))
    listening = data_dir / LISTENING_FILE
    parts.append(Part("Hörprotokoll", len(_entries(listening)), _size(listening)))
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
    tags = [_tag_record(entry) for entry in client.tags() if _is_tag(entry)]

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

        for name in sorted({_cover_name(model) for model in models} - {None}):
            try:
                archive.writestr(f"covers/{name}", client.fetch(f"{CUSTOM_IMG}{name}"))
            except TeddyCloudError as error:
                # A model whose picture is gone still has a name worth keeping.
                log.warning("backup: cover %s cannot be read: %s", name, error)

        for file_name in (TIMES_FILE, LISTENING_FILE):
            path = data_dir / file_name
            if path.is_file():
                archive.write(path, file_name)
    return buffer.getvalue()


def read_backup(data: bytes) -> Backup:
    """Unpack a backup, refusing anything that is not one."""
    if len(data) > LARGEST_BACKUP:
        raise BackupError("that file is far larger than any backup")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise BackupError("that is not a zip file") from error

    with archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise BackupError("that zip is not a tonie-studio backup")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            models = json.loads(archive.read("models.json").decode("utf-8"))
            tags = json.loads(archive.read("tags.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise BackupError(f"the backup is damaged: {error}") from error
        if manifest.get("format") != FORMAT:
            raise BackupError(f"backup format {manifest.get('format')} is not one this app knows")

        covers = {}
        for name in names:
            if not name.startswith("covers/"):
                continue
            base = name[len("covers/"):]
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
        )


def plan_restore(client: TeddyCloudClient, backup: Backup, data_dir: Path) -> List[Step]:
    """What a restore would do, without doing any of it."""
    return _restore(client, backup, data_dir, write=False)


def apply_restore(client: TeddyCloudClient, backup: Backup, data_dir: Path) -> List[Step]:
    """Restore, and return what was done."""
    return _restore(client, backup, data_dir, write=True)


def _restore(client: TeddyCloudClient, backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    steps: List[Step] = []

    current_models = {str(model.get("model", "")).lower(): model
                      for model in client.custom_tonies()}
    for model in backup.models:
        existing = current_models.get(str(model["model"]).lower())
        action = NEW if existing is None else (SAME if existing == model else CHANGES)
        steps.append(_attempt(write and action != SAME, "model", _model_label(model), action,
                              lambda model=model: client.upsert_custom_tonie(model)))

    for name, data in sorted(backup.covers.items()):
        try:
            existing_cover: Optional[bytes] = client.fetch(f"{CUSTOM_IMG}{name}")
        except TeddyCloudError:
            existing_cover = None
        action = NEW if existing_cover is None else (SAME if existing_cover == data else REPLACES)
        steps.append(_attempt(
            write and action != SAME, "cover", name, action,
            lambda name=name, data=data: client.upload(io.BytesIO(data), name, path="/",
                                                       special="custom_img")))

    known = {entry.get("ruid"): entry for entry in client.tags() if _is_tag(entry)}
    present = _library_sources(client)
    for record in backup.tags:
        ruid = record["ruid"]
        label = record.get("label") or ruid
        entry = known.get(ruid)
        if entry is None:
            steps.append(Step("tag", label, SKIPPED,
                              "Die Box hat diesen Sticker noch nicht gelesen. Einmal auflegen, "
                              "dann noch einmal wiederherstellen."))
            continue

        wanted = _restorable(record)
        current = _tag_record(entry)
        changed = {field: value for field, value in wanted.items() if current.get(field) != value}
        note = _missing_note(record.get("source") or "", present)
        steps.append(_attempt(write and bool(changed), "tag", label,
                              CHANGES if changed else SAME,
                              lambda ruid=ruid, changed=changed: client.set_tag(ruid, **changed),
                              note))

    steps += _restore_times(backup, data_dir, write)
    steps += _restore_listening(backup, data_dir, write)
    if write:
        log.info("restored a backup from %s", backup.manifest.get("created"))
    return steps


FAILED = "fehlgeschlagen"


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
        return Step(what, name, FAILED, str(error))
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
        return [Step("zeiten", TIMES_FILE, SKIPPED, "In der Sicherung unlesbar.")]
    path = data_dir / TIMES_FILE
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        current = {}

    merged = dict(saved)
    merged.update(current)
    if merged == current:
        return [Step("zeiten", TIMES_FILE, SAME)]
    if write:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")
    return [Step("zeiten", TIMES_FILE, NEW if not current else CHANGES)]


def _restore_listening(backup: Backup, data_dir: Path, write: bool) -> List[Step]:
    """The log comes back only where there is none: an older log over a newer
    one would throw away whatever was heard in between."""
    if backup.listening is None:
        return []
    path = data_dir / LISTENING_FILE
    current = path.read_bytes() if path.is_file() else b""
    if current == backup.listening:
        return [Step("protokoll", LISTENING_FILE, SAME)]
    if current.strip():
        return [Step("protokoll", LISTENING_FILE, KEPT,
                     "Hier läuft schon ein Protokoll; es wird nicht überschrieben.")]
    if write:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(backup.listening)
    return [Step("protokoll", LISTENING_FILE, NEW)]


def _library_sources(client: TeddyCloudClient) -> Optional[set]:
    try:
        return {taf["source"] for taf in list_tafs(client, strict=True)}
    except TeddyCloudError:
        return None


def _missing_note(source: str, present: Optional[set]) -> str:
    if not source.startswith("lib://"):
        return ""
    if present is None:
        return "Library nicht lesbar; ob das Taf da ist, ist unbekannt."
    if source not in present:
        return "Das Taf fehlt noch. Der Tonie bleibt stumm, bis es neu encodiert ist."
    return ""


def _tag_record(entry: Dict) -> Dict:
    info = entry.get("tonieInfo") or {}
    series, episode = info.get("series") or "", info.get("episode") or ""
    return {
        "ruid": entry.get("ruid"),
        "label": f"{series} - {episode}".strip(" -") or entry.get("source") or entry.get("ruid"),
        "source": entry.get("source") or "",
        "tonie_model": info.get("model") or "",
        "nocloud": bool(entry.get("nocloud")),
        "live": bool(entry.get("live")),
    }


def _is_tag(entry: Dict) -> bool:
    return entry.get("ruid") != SYSTEM_RUID and entry.get("type") != "system"


def _cover_name(model: Dict) -> Optional[str]:
    picture = str(model.get("pic") or "")
    if not picture.startswith(CUSTOM_IMG):
        return None
    name = picture[len(CUSTOM_IMG):]
    return name if name and "/" not in name else None


def _model_label(model: Dict) -> str:
    return f"{model.get('model')}: {model.get('series', '')} - {model.get('episodes', '')}".strip(" -")


def _json(value) -> str:
    # Umlauts written as themselves: a backup someone opens to check should
    # read "Siebenschläfer", and the zip stores UTF-8 either way.
    return json.dumps(value, ensure_ascii=False, indent=1)
