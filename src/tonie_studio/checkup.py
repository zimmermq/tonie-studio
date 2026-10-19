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
- A library tag without ``nocloud`` lets teddyCloud ask the real Tonies cloud
  about it: for a homemade tonie about content the cloud does not have, for an
  imported one about audio teddyCloud already keeps.
- What nothing points at any more - a taf no tag plays, a model no tag names -
  costs space and makes every list longer than it needs to be.

The checks only report. Each finding carries the repair it would make and a
preview of what that writes, and the page applies one only when asked, after
running the checks again so a stale finding can never be acted on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .encode import Taf, aside_of, delete_taf, list_tafs
from .library import is_library, listed_path, path_of, played, same, source_of
from .models import MODEL_PREFIX, WRITING, Model, find_model, list_models, update_model
from .tags import TonieError, TonieTag, find_tag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError
from .tonie import delete_model

# The repairs offered for every finding at once. Only one that flips a flag
# belongs here: deleting and emptying stay one at a time, each shown first.
BULK_FIXES = frozenset({"nocloud"})

# Findings whose target is a sticker's ruid, so the page can lead to its tonie.
TAG_KINDS = frozenset({"missing_taf", "cloud_fallback", "nameless"})

log = logging.getLogger(__name__)

# teddyCloud's catalogue names the box's own sounds - its startup jingle and
# the like - after the box: "box-en-us-02-00000000". teddyCloud caches them
# from the cloud like any audio; the box plays them, never a sticker.
BOX_SOUND_PREFIX = "box-"

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
    """The text as it was meant, if it is UTF-8 that was read as latin-1 or Windows-1252.

    Mangled text has a tell: turned back into the bytes it was read from it
    is valid UTF-8 again. Correct German never is - a lone "\u00e4" is not a
    complete UTF-8 sequence, and neither are typographic quotes - and a
    decomposed umlaut has no byte in either code page at all, so neither is
    ever mistaken for damage. Damage done twice is undone twice.
    """
    if not text or text.isascii():
        return None
    current = text
    for _ in range(3):
        try:
            candidate = _bytes_read(current).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == current:
            break
        current = candidate
    return current if current != text else None


def _bytes_read(text: str) -> bytes:
    """The bytes ``text`` was decoded from, had they been read as latin-1 or Windows-1252.

    The two agree on every byte but 0x80-0x9F, where latin-1 has control
    characters and Windows-1252 letters and punctuation ("\u00c3\u201e" is
    "\u00c4" read the Windows way). Text read the Windows way still holds the
    control characters for the five bytes Windows-1252 leaves undefined, so
    one string can hold both; each character is mapped back on its own.
    Raises UnicodeEncodeError for a character neither code page has.
    """
    return b"".join(bytes([ord(character)]) if ord(character) < 256
                    else character.encode("cp1252") for character in text)


def examine(client: TeddyCloudClient, busy: Iterable[str] = (),
            busy_folders: Iterable[str] = ()) -> List[Finding]:
    """Every finding, errors first.

    ``busy`` names library paths that work in progress is about to use - an
    encode waiting for its sticker, say - and ``busy_folders`` whole folders of
    them. Nothing there is offered for deletion: played by nothing *yet* is not
    the same as left over.
    """
    tags = list_tags(client)
    models = list_models(client)
    try:
        tafs: Optional[List[Taf]] = list_tafs(client, strict=True)
    except TeddyCloudError as error:
        log.warning("checkup could not read the whole library: %s", error)
        tafs = None

    findings: List[Finding] = []
    spared = {same(path) for path in busy}
    findings += _tag_findings(tags, tafs, spared)
    findings += _model_findings(models)
    findings += _leftovers(tags, models, tafs, spared,
                           tuple(same(folder).rstrip("/") + "/" for folder in busy_folders))
    if tafs is None:
        findings.append(Finding(
            kind="library_unreadable",
            severity=WARNING,
            subject="Library",
            detail="checkup.library_unreadable",
            target="/",
        ))
    return sorted(findings, key=lambda finding: SEVERITIES.index(finding.severity))


def _tag_findings(tags: List[TonieTag], tafs: Optional[List[Taf]], busy: set) -> List[Finding]:
    findings = []
    # A sticker plays a file only under the exact name the library lists;
    # see library. The same file under the other spelling of an umlaut is
    # what a repair points the sticker at instead.
    present = {taf.source for taf in tafs} if tafs is not None else None
    spellings = {same(taf.source): taf.source for taf in tafs or []}

    for tag in tags:
        source = tag.source
        if not is_library(source):
            continue
        ruid, name = tag.ruid, tag.label

        # Audio an encode is about to write - a rebuild of this very tonie,
        # say - is on its way rather than missing.
        missing = present is not None and source not in present
        if missing and same(path_of(source)) not in busy:
            fixed = repaired(source)
            listed = spellings.get(same(fixed)) if fixed else spellings.get(same(source))
            if listed:
                findings.append(Finding(
                    kind="missing_taf", severity=ERROR, subject=name, target=ruid,
                    detail="checkup.garbled_path" if fixed else "checkup.other_spelling",
                    fix="repair_source", preview=f"source: {source} \u2192 {listed}",
                ))
            else:
                findings.append(Finding(
                    kind="missing_taf", severity=ERROR, subject=name, target=ruid,
                    detail="checkup.missing_taf", params={"source": source, "model": tag.model},
                    fix="empty_sticker", preview_key="checkup.empties_sticker",
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
        findings.append(Finding(
            kind="garbled_model", severity=ERROR,
            subject=repaired(model.series) or model.series or model.id,
            target=model.id,
            detail="checkup.garbled_model",
            fix="repair_model",
            preview="; ".join(f"{name}: {before} \u2192 {after}"
                              for name, before, after in changes),
        ))
    return findings


def model_repairs(model: Model) -> Dict[str, object]:
    """The fields of a model as they should read, only those that change.

    The keys name the model's fields: series, episode and tracks.
    """
    repairs: Dict[str, object] = {}
    for name in ("series", "episode"):
        fixed = repaired(getattr(model, name))
        if fixed:
            repairs[name] = fixed
    fixed_tracks = [repaired(track) or track for track in model.tracks]
    if fixed_tracks != model.tracks:
        repairs["tracks"] = fixed_tracks
    return repairs


def _model_repairs(model: Model) -> List[tuple]:
    changes = []
    for name, after in model_repairs(model).items():
        if name == "tracks":
            for before, fixed in zip(model.tracks, after, strict=True):
                if before != fixed:
                    changes.append(("track", before, fixed))
        else:
            changes.append((name, getattr(model, name), after))
    return changes


def _leftovers(tags: List[TonieTag], models: List[Model], tafs: Optional[List[Taf]],
               busy: set, busy_folders: tuple) -> List[Finding]:
    findings = []
    playing = played(tag.source for tag in tags)
    named = {tag.model.lower() for tag in tags}
    # Targets of replaces stopped before they pointed their stickers back:
    # the stickers play the copy, and the target is the audio meant for them.
    meant = {same(aside_of(taf.path) or "") for taf in tafs or []
             if same(taf.source) in playing}

    for taf in tafs or []:
        path = same(taf.path)
        if same(taf.source) in playing or path in busy or path.startswith(busy_folders):
            continue
        if path in meant:
            continue
        # The copy a running replace encodes beside its target is in use too.
        if same(aside_of(taf.path) or "") in busy:
            continue
        if taf.model.lower().startswith(BOX_SOUND_PREFIX):
            continue
        findings.append(Finding(
            kind="orphan_taf", severity=NOTE, subject=taf.name, target=taf.path,
            detail="checkup.orphan_taf", params={"size": taf.size, "path": taf.path},
            fix="delete_taf", preview_key="checkup.deletes_taf",
        ))

    for model in models:
        if not model.id.upper().startswith(MODEL_PREFIX) or model.id.lower() in named:
            continue
        findings.append(Finding(
            kind="orphan_model", severity=NOTE,
            subject=model.label,
            target=model.id,
            detail="checkup.orphan_model", params={"model": model.id},
            fix="delete_model", preview_key="checkup.deletes_model",
        ))
    return findings



def apply_fix(client: TeddyCloudClient, data_dir: Path, fix: str, target: str) -> None:
    """Make one repair a finding offered.

    The caller runs the checks again first, or a repair could undo something
    done in the meantime. What would lose something checks once more under
    the lock every model and tag write shares, right before it writes: a
    sticker given audio since must not be emptied, and a taf or model given
    to a sticker since must not be deleted. Flipping ``nocloud`` and
    repairing a name only ever make a sticker or model more right, and a
    repaired source is worked out from the sticker as it is now.
    """
    with WRITING:
        if fix == "nocloud":
            client.set_tag(target, nocloud=True)
        elif fix == "repair_source":
            client.set_tag(target, source=_repaired_source(client, target))
        elif fix == "repair_model":
            model = find_model(client, target)
            if model is None:
                raise TonieError("tonie.model_missing", model=target)
            changes = model_repairs(model)
            update_model(client, target, series=changes.get("series"),
                         episode=changes.get("episode"), tracks=changes.get("tracks"))
        elif fix == "empty_sticker":
            # Only while it still names audio the library lacks: since the
            # checks looked, the sticker may have been given a tonie again,
            # and emptying would throw that away. The link to the model goes
            # too: without it, the empty sticker would still carry the old
            # name and cover in teddyCloud. That link is also what a rebuild
            # finds the sticker by, so the page offers making the tonie
            # again before this.
            tag = find_tag(client, target)
            if tag is None or not _plays_nothing(client, tag.source):
                raise TonieError("checkup.now_playing", target=target)
            client.set_tag(target, source="", tonie_model="")
        elif fix == "delete_taf":
            if any(same(tag.source) == same(source_of(target)) for tag in list_tags(client)):
                raise TonieError("checkup.now_used", target=target)
            delete_taf(client, target)
        elif fix == "delete_model":
            if any((tag.model or "").lower() == target.lower() for tag in list_tags(client)):
                raise TonieError("checkup.now_used", target=target)
            delete_model(client, target, data_dir)
        else:
            raise ValueError(f"unknown repair {fix!r}")


def _plays_nothing(client: TeddyCloudClient, source: str) -> bool:
    """Whether ``source`` names library audio that is not there in that exact spelling."""
    return is_library(source) and listed_path(client, path_of(source)) != path_of(source)


def _repaired_source(client: TeddyCloudClient, ruid: str) -> str:
    """The source the sticker ``ruid`` should have: its own, as the library spells it.

    Read again here rather than taken from the finding, so a sticker given
    other audio since is repaired to that, or left alone.
    """
    tag = find_tag(client, ruid)
    source = tag.source if tag else ""
    if is_library(source) and not _plays_nothing(client, source):
        raise TonieError("checkup.nothing_to_repair", target=ruid)
    for candidate in (source, repaired(source)):
        if candidate and is_library(candidate):
            listed = listed_path(client, path_of(candidate))
            if listed and source_of(listed) != source:
                return source_of(listed)
    # Mangled, and the library does not have the repaired path either: the
    # repair is still the one the finding showed, and the checkup then
    # reports the sticker as missing its audio.
    fixed = repaired(source)
    if not fixed:
        raise TonieError("checkup.nothing_to_repair", target=ruid)
    return fixed
