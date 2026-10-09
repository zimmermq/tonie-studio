"""Finds what is broken before someone at the box does.

Every check here answers a failure that has already happened once on a real
instance, and each of them was silent: teddyCloud stores whatever it is handed,
the box plays nothing rather than complaining, and nobody notices until a child
puts a tonie on and nothing happens.

- A tag pointing at a library path that does not exist plays nothing. This is
  how two tags broke when a path was read with the wrong encoding and written
  back with its umlaut mangled.
- A name stored mangled the same way shows up mangled in teddyCloud, on the
  box's announcements and on every printed sticker.
- A library tag without ``nocloud`` can make the box ask the real Tonies cloud
  for content it will never have.
- What nothing points at any more - a taf no tag plays, a model no tag names -
  costs space and makes every list longer than it needs to be.

The checks only report. Each finding carries the repair it would make and a
preview of what that writes, and the page applies one only when asked, after
running the checks again so a stale finding can never be acted on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .encode import list_tafs
from .library import is_library, played, same
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import MODEL_PREFIX, TonieTag, list_tags, model_label

log = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"
NOTE = "note"
SEVERITIES = (ERROR, WARNING, NOTE)

@dataclass(frozen=True)
class Finding:
    """One thing that is wrong, and what could be done about it."""

    kind: str
    severity: str
    # What it is about, as a person recognises it: a tonie's name, a file.
    subject: str
    # What is wrong and why it matters: a key of the reader's language, and
    # the values it names.
    detail: str
    # The ruid, model id or library path the repair acts on.
    target: str
    # The repair the page may offer, or None when only a person can decide.
    fix: Optional[str] = None
    # What that repair would write, shown before it runs. Either the exact
    # values written, which read the same in every language, or a key.
    preview: str = ""
    preview_key: str = ""
    params: Dict[str, object] = field(default_factory=dict)


def repaired(text: Optional[str]) -> Optional[str]:
    """The text as it was meant, if it is UTF-8 that was read as latin-1.

    Mangled text has a tell: encoded back to latin-1 it is valid UTF-8 again.
    Correct German never is - a lone "\u00e4" is not a complete UTF-8 sequence -
    and a decomposed umlaut cannot be encoded to latin-1 at all, so neither is
    ever mistaken for damage. Damage done twice is undone twice.
    """
    if not text or text.isascii():
        return None
    current = text
    for _ in range(3):
        try:
            candidate = current.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == current:
            break
        current = candidate
    return current if current != text else None


def examine(client: TeddyCloudClient, busy: Iterable[str] = (),
            busy_folders: Iterable[str] = ()) -> List[Finding]:
    """Every finding, errors first.

    ``busy`` names library paths that work in progress is about to use - an
    encode waiting for its sticker, say - and ``busy_folders`` whole folders of
    them. Nothing there is offered for deletion: played by nothing *yet* is not
    the same as left over.
    """
    tags = list_tags(client)
    models = client.custom_tonies()
    try:
        tafs: Optional[List[dict]] = list_tafs(client, strict=True)
    except TeddyCloudError as error:
        log.warning("checkup could not read the whole library: %s", error)
        tafs = None

    findings: List[Finding] = []
    findings += _tag_findings(tags, tafs)
    findings += _model_findings(models)
    findings += _leftovers(tags, models, tafs, {_same(path) for path in busy},
                           tuple(_same(folder).rstrip("/") + "/" for folder in busy_folders))
    if tafs is None:
        findings.append(Finding(
            kind="library_unreadable",
            severity=WARNING,
            subject="Library",
            detail="checkup.library_unreadable",
            target="/",
        ))
    return sorted(findings, key=lambda finding: SEVERITIES.index(finding.severity))


def _tag_findings(tags: List[TonieTag], tafs: Optional[List[dict]]) -> List[Finding]:
    findings = []
    present = played(taf["source"] for taf in tafs) if tafs is not None else None

    for tag in tags:
        source = tag.source
        if not is_library(source):
            continue
        ruid, name = tag.ruid, tag.label

        if present is not None and _same(source) not in present:
            fixed = repaired(source)
            if fixed and _same(fixed) in present:
                findings.append(Finding(
                    kind="missing_taf", severity=ERROR, subject=name, target=ruid,
                    detail="checkup.garbled_path",
                    fix="repair_source", preview=f"source: {source} \u2192 {fixed}",
                ))
            else:
                findings.append(Finding(
                    kind="missing_taf", severity=ERROR, subject=name, target=ruid,
                    detail="checkup.missing_taf", params={"source": source},
                ))

        if not tag.nocloud:
            findings.append(Finding(
                kind="cloud_fallback", severity=WARNING, subject=name, target=ruid,
                detail="checkup.cloud_fallback",
                fix="nocloud", preview="nocloud: false \u2192 true",
            ))

        if not tag.model:
            findings.append(Finding(
                kind="nameless", severity=NOTE, subject=name, target=ruid,
                detail="checkup.nameless",
            ))
    return findings


def _model_findings(models: List[dict]) -> List[Finding]:
    findings = []
    for model in models:
        changes = _model_repairs(model)
        if not changes:
            continue
        model_id = str(model.get("model", ""))
        findings.append(Finding(
            kind="garbled_model", severity=ERROR,
            subject=repaired(model.get("series")) or model.get("series") or model_id,
            target=model_id,
            detail="checkup.garbled_model",
            fix="repair_model",
            preview="; ".join(f"{name}: {before} \u2192 {after}"
                              for name, before, after in changes),
        ))
    return findings


def model_repairs(model: dict) -> Dict[str, object]:
    """The fields of a model as they should read, only those that change."""
    repairs: Dict[str, object] = {}
    for name in ("series", "episodes"):
        fixed = repaired(model.get(name))
        if fixed:
            repairs[name] = fixed
    tracks = [str(track) for track in model.get("tracks") or []]
    fixed_tracks = [repaired(track) or track for track in tracks]
    if fixed_tracks != tracks:
        repairs["tracks"] = fixed_tracks
    return repairs


def _model_repairs(model: dict) -> List[tuple]:
    changes = []
    for name, after in model_repairs(model).items():
        if name == "tracks":
            before_tracks = [str(track) for track in model.get("tracks") or []]
            for before, fixed in zip(before_tracks, after, strict=True):
                if before != fixed:
                    changes.append(("track", before, fixed))
        else:
            changes.append((name, model.get(name), after))
    return changes


def _leftovers(tags: List[TonieTag], models: List[dict], tafs: Optional[List[dict]],
               busy: set, busy_folders: tuple) -> List[Finding]:
    findings = []
    playing = played(tag.source for tag in tags)
    named = {tag.model.lower() for tag in tags}

    for taf in tafs or []:
        path = _same(taf["path"])
        if _same(taf["source"]) in playing or path in busy or path.startswith(busy_folders):
            continue
        findings.append(Finding(
            kind="orphan_taf", severity=NOTE, subject=taf["name"], target=taf["path"],
            detail="checkup.orphan_taf", params={"size": taf.get("size", 0), "path": taf["path"]},
            fix="delete_taf", preview_key="checkup.deletes_taf",
        ))

    for model in models:
        model_id = str(model.get("model", ""))
        if not model_id.upper().startswith(MODEL_PREFIX) or model_id.lower() in named:
            continue
        findings.append(Finding(
            kind="orphan_model", severity=NOTE,
            subject=model_label(model),
            target=model_id,
            detail="checkup.orphan_model", params={"model": model_id},
            fix="delete_model", preview_key="checkup.deletes_model",
        ))
    return findings


_same = same
