"""Naming imported tonies when teddyCloud cannot.

teddyCloud shows a tonie's name and cover from tonies.json, the catalogue it
keeps up to date itself, when it finds the tonie's audio there. Most imported
tonies are named that way and need nothing from here. Three kinds are not:

- audio the Tonies cloud handed out under an id the catalogue does not list
  (it re-issues recordings), which teddyCloud shows as an unknown tonie;
- a figure of a set, which the catalogue lists as one entry for all figures,
  so every figure would be called after the set;
- a stub, an entry with the audio but no name, shown as a bare number.

For those a custom model is made, in this app's namespace, named after where
the dump came from: its folder is the series and its file the episode, with
the catalogue's spelling of the series where it knows it, and its category
and cover where they are unambiguous. The model carries the taf's audio id
and hash, so a second tonie with the same audio reuses it.

Two upkeep tasks keep that right later. A custom model whose audio the
catalogue has since listed is replaced by the official model, which describes
the recording at least as well and brings the official cover and track names.
And models an earlier tool made under the NFC- prefix are moved into this
app's namespace, so every model it did not get from the catalogue is one it
looks after.

Names are matched with umlauts folded to ae, oe, ue and ss, since dump folders
spell them out that way. An episode that matches more than one model is not
guessed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .cache import TtlCache
from .models import CATEGORIES, MODEL_PREFIX, Model, list_models
from .tags import TonieTag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

LANGUAGE = "de-de"

# Where teddyCloud keeps the tafs it fetched from the cloud, named <audioId>.taf.
LIBRARY_PREFIX = "lib://by/audioID/"
LIBRARY_FOLDER = "/by/audioID"

# The prefix of the models an earlier command-line tool made for imported tonies.
OLD_PREFIX = "NFC-"

# "Tolle Berufe - Set", "Wuselwesen Set", with an en dash too; not "Sunset".
SET_SUFFIX = re.compile(r"(^|[\s\-\u2013])set\s*$", re.IGNORECASE)

# What a step does to a tonie.
OFFICIAL = "official"  # link to the catalogue's model
NEW = "new"            # make a custom model and link to it
REUSE = "reuse"        # link to the custom model that already carries the audio
PROMOTE = "promote"    # replace a custom model the catalogue now covers
RENAME = "rename"      # move an NFC- model into this app's namespace
RETITLE = "retitle"    # give a model of ours the better name the rules find now

# How long a copy of the catalogue is used before it is fetched again. It
# changes a few times a week and is several megabytes.
CATALOG_TTL = 3600.0

# How often the upkeep runs on its own, in seconds.
UPKEEP_EVERY = 86400.0


def norm(text: str) -> str:
    """``text`` reduced to what two spellings of the same name share."""
    text = text.lower()
    for umlaut, spelled in (("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"), ("\u00df", "ss")):
        text = text.replace(umlaut, spelled)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass(frozen=True)
class Name:
    model: Optional[str]  # the official model to link to, None when a custom one is needed
    series: str
    episode: str
    category: str = ""  # only when every official entry of the series agrees
    pic: str = ""       # only when the series is a single official entry


class Catalog:
    """The catalogue's entries of one language, looked up by normalised name."""

    def __init__(self, entries: Iterable[Mapping[str, Any]], language: str = LANGUAGE):
        entries = list(entries)
        self.by_model = {entry["model"]: entry for entry in entries if entry.get("model")}
        self.by_series: Dict[str, List[Mapping[str, Any]]] = {}
        for entry in entries:
            if entry.get("language") == language and entry.get("model") and entry.get("series"):
                self.by_series.setdefault(norm(entry["series"]), []).append(entry)

    def describe(self, path: str) -> Name:
        """The name of the tonie dumped at ``path``, ``<...>/<series>/<episode>.nfc``."""
        dump = PurePosixPath(path)
        series, episode = dump.parent.name, dump.stem
        entries = self.by_series.get(norm(series))
        if not entries:
            return Name(None, series, episode)
        wanted = norm(episode)
        models = {entry["model"] for entry in entries
                  if wanted in (norm(entry.get("episodes") or ""), norm(entry.get("title") or ""))}
        official_series = entries[0]["series"]
        if len(models) == 1:
            return Name(models.pop(), official_series, episode)
        categories = {entry.get("category") or "" for entry in entries}
        category = categories.pop() if len(categories) == 1 else ""
        # Another episode's cover would be the wrong picture; a series that is
        # one product, such as a set, shows what this tonie belongs to.
        pic = (entries[0].get("pic") or "") if len(entries) == 1 else ""
        return Name(None, official_series, episode, category, pic)

    def is_set(self, model_id: str) -> bool:
        entry = self.by_model.get(model_id) or {}
        return any(SET_SUFFIX.search(entry.get(key) or "") for key in ("episodes", "title"))

    def is_stub(self, model_id: str) -> bool:
        return model_id in self.by_model and not self.by_model[model_id].get("series")

    def unsuitable(self, model_id: str) -> bool:
        """Whether a tonie named after this official model is better off with its own."""
        return self.is_set(model_id) or self.is_stub(model_id)

    def figure(self, path: str, set_model: str) -> Name:
        """The name of one figure of a set: the set's track its file name matches.

        The catalogue lists a set's figures as its tracks, often as "<series> -
        <figure>", while a dump is called "<series> Set - <figure>", so a track
        is also matched by what follows its series. The figure's name is then
        that part, in the catalogue's spelling.
        """
        entry = self.by_model[set_model]
        series = entry.get("series") or ""
        dump = PurePosixPath(path)
        stem = norm(dump.stem)
        found = []
        for track in entry.get("tracks") or []:
            head, separator, rest = track.partition(" - ")
            figure = rest if separator and norm(head) == norm(series) and rest.strip() else track
            whole, own = norm(track), norm(figure)
            if (whole and (whole in stem or stem in whole)) or (own and own in stem):
                found.append(figure.strip())
        episode = found[0] if len(found) == 1 else dump.stem
        return Name(None, series or dump.parent.name, episode,
                    entry.get("category") or "", entry.get("pic") or "")


class AudioIndex:
    """Which catalogue model an audio id and hash belong to.

    teddyCloud accepts any of an entry's audio ids together with any of its
    hashes. Every such pair as a key of its own grows with the square of an
    entry's length: the catalogue lists a daily podcast as one entry with a
    thousand ids and hashes, a million pairs and some 400 MB. Each id points
    to the entries listing it instead, with their hashes as a set. Where two
    entries list the same audio, the later one names it.
    """

    def __init__(self, entries: Iterable[Mapping[str, Any]]):
        self._by_id: Dict[str, List[Tuple[frozenset, str]]] = {}
        for entry in entries:
            if not entry.get("model"):
                continue
            hashes = frozenset(str(sha1).upper() for sha1 in entry.get("hash") or [])
            for audio_id in entry.get("audio_id") or []:
                self._by_id.setdefault(str(audio_id), []).append((hashes, entry["model"]))

    def get(self, key: Tuple[str, str], default: Optional[str] = None) -> Optional[str]:
        """The model for ``(audio id, HASH)``, or ``default``; the hash in capitals."""
        audio_id, sha1 = key
        for hashes, model in reversed(self._by_id.get(audio_id, [])):
            if sha1 in hashes:
                return model
        return default

    def __getitem__(self, key: Tuple[str, str]) -> str:
        model = self.get(key)
        if model is None:
            raise KeyError(key)
        return model


def official_by_audio(entries: Iterable[Mapping[str, Any]]) -> AudioIndex:
    """The catalogue's models by ``(audio id, HASH)``; see AudioIndex."""
    return AudioIndex(entries)


def promotions(models: Iterable[Model], by_audio: AudioIndex,
               unsuitable: Callable[[str], bool]) -> Dict[str, str]:
    """``{custom model: official model}`` for the custom models the catalogue now covers.

    A set or a stub is never a target: it is what the custom model replaced.
    Models this app made from albums carry audio it encoded itself, which no
    catalogue lists, so they are never matched.
    """
    found = {}
    for model in models:
        for audio_id in model.audio_ids:
            for sha1 in model.hashes:
                official = by_audio.get((str(audio_id), str(sha1).upper()))
                if official and not unsuitable(official):
                    found[model.id] = official
    return found


@dataclass
class Step:
    kind: str
    ruid: str
    model_id: str
    name: Optional[Name] = None
    path: str = ""  # the dump the name comes from, when there is one
    label: str = ""  # what teddyCloud calls the tonie now
    new_model: Optional[Model] = None  # written before the tonie is linked to it
    replaces: str = ""  # the model the tonie is named after now, when it has one


@dataclass
class Plan:
    steps: List[Step] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (dump, why)
    retired: List[str] = field(default_factory=list)  # deleted once every tonie points away

    @property
    def empty(self) -> bool:
        return not self.steps and not self.retired


class PlanChanged(Exception):
    """The plan is no longer the one a person looked at before applying it."""


def fingerprint(todo: Plan) -> str:
    """A short digest of everything a plan would write, to tell two plans apart."""
    steps = [[step.kind, step.ruid, step.model_id, step.replaces,
              step.new_model.to_json() if step.new_model else None] for step in todo.steps]
    text = json.dumps([steps, todo.retired], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


def plan(tags: Iterable[TonieTag], dumps: Mapping[str, str], catalog: Catalog,
         by_audio: AudioIndex, models: List[Model],
         headers: Mapping[str, Mapping[str, Any]], migrate: bool = False,
         relink: bool = True, retitle: bool = False) -> Plan:
    """What naming the tonies in ``dumps`` (``{ruid: dump path}``) takes.

    ``tags`` is teddyCloud's tag index, whose model is the one teddyCloud names
    the tonie after now, from its content JSON or found by its audio.
    ``headers`` holds the taf headers of the library's ``by/audioID`` folder by
    file name. With ``relink`` every tonie on a model the catalogue now covers
    is moved to the official one; with ``migrate`` the NFC- models move into
    this app's namespace too; with ``retitle`` a model of this app named after
    a dump in ``dumps`` is renamed when the rules now find a better name, such
    as a set figure's official title.
    """
    tags = list(tags)
    by_ruid = {tag.ruid: tag for tag in tags}
    promoted = promotions(models, by_audio, catalog.unsuitable) if relink else {}
    numbered = [model.id for model in models]

    def next_id() -> str:
        used = [int(model_id[len(MODEL_PREFIX):]) for model_id in numbered
                if model_id.startswith(MODEL_PREFIX) and model_id[len(MODEL_PREFIX):].isdigit()]
        model_id = f"{MODEL_PREFIX}{max(used, default=0) + 1:04d}"
        numbered.append(model_id)
        return model_id

    renamed: Dict[str, Model] = {}
    if migrate:
        for model in models:
            if model.id.upper().startswith(OLD_PREFIX) and model.id not in promoted:
                renamed[model.id] = replace(model, id=next_id())
    available = [model for model in models
                 if model.id not in promoted and model.id not in renamed] + list(renamed.values())

    result = Plan(retired=sorted(set(promoted) | set(renamed)))
    handled = set()
    written = set()
    for tag in tags:
        if tag.model in promoted:
            result.steps.append(Step(PROMOTE, tag.ruid, promoted[tag.model], replaces=tag.model,
                                     path=dumps.get(tag.ruid, ""), label=tag.label))
            handled.add(tag.ruid)
        elif tag.model in renamed:
            new = renamed[tag.model]
            result.steps.append(Step(RENAME, tag.ruid, new.id, replaces=tag.model,
                                     new_model=None if new.id in written else new,
                                     path=dumps.get(tag.ruid, ""), label=tag.label))
            written.add(new.id)
            handled.add(tag.ruid)

    for ruid, path in dumps.items():
        tag = by_ruid.get(ruid)
        if ruid in handled or tag is None or not tag.source:
            continue
        current = tag.model
        if current and not catalog.unsuitable(current):
            continue
        header = headers.get(tag.source[len(LIBRARY_PREFIX):]) \
            if tag.source.startswith(LIBRARY_PREFIX) else None
        if not header:
            result.skipped.append((path, tag.source))
            continue
        audio = (str(header["audioId"]), str(header["sha1Hash"]).upper())

        official = current or by_audio.get(audio)
        if official and catalog.is_stub(official):
            official = None
        if official and catalog.is_set(official):
            name = catalog.figure(path, official)
        elif official:
            result.steps.append(Step(OFFICIAL, ruid, official, path=path))
            continue
        else:
            name = catalog.describe(path)
            if name.model:
                result.steps.append(Step(OFFICIAL, ruid, name.model, name=name, path=path))
                continue

        existing = next((model for model in available if audio[0] in map(str, model.audio_ids)
                         and audio[1] in [sha1.upper() for sha1 in model.hashes]), None)
        if existing:
            result.steps.append(Step(REUSE, ruid, existing.id, name=name, path=path,
                                     replaces=current))
            continue
        model = Model(id=next_id(), series=name.series, episode=name.episode, language=LANGUAGE,
                      category=name.category if name.category in CATEGORIES else "",
                      picture=name.pic, audio_ids=[audio[0]], hashes=[audio[1]])
        available.append(model)
        result.steps.append(Step(NEW, ruid, model.id, name=name, path=path, new_model=model,
                                 replaces=current))
        handled.add(ruid)

    if retitle:
        result.steps.extend(_retitles(by_ruid, dumps, handled, catalog, by_audio, models, headers))
    return result


def _retitles(by_ruid: Mapping[str, TonieTag], dumps: Mapping[str, str], handled: set,
              catalog: Catalog, by_audio: AudioIndex, models: List[Model],
              headers: Mapping[str, Mapping[str, Any]]) -> List[Step]:
    """Better names for this app's models of imported tonies.

    Only tonies with a dump are looked at, so a model this app made from an
    album is never renamed: no dump names it.
    """
    ours = {model.id: model for model in models if model.id.startswith(MODEL_PREFIX)}
    steps, done = [], set()
    for ruid, path in dumps.items():
        tag = by_ruid.get(ruid)
        if ruid in handled or tag is None or tag.model not in ours or tag.model in done:
            continue
        header = headers.get(tag.source[len(LIBRARY_PREFIX):]) \
            if tag.source.startswith(LIBRARY_PREFIX) else None
        if not header:
            continue
        official = by_audio.get((str(header["audioId"]), str(header["sha1Hash"]).upper()))
        if official and catalog.is_set(official):
            name = catalog.figure(path, official)
        elif official:
            continue  # the catalogue names it; promotion moves it there
        else:
            name = catalog.describe(path)
            if name.model:
                continue
        model = ours[tag.model]
        if (model.series, model.episode) == (name.series, name.episode):
            continue
        done.add(model.id)
        steps.append(Step(RETITLE, ruid, model.id, name=name, path=path,
                          new_model=replace(model, series=name.series, episode=name.episode),
                          label=tag.label))
    return steps


def apply(client: TeddyCloudClient, todo: Plan) -> None:
    """Write the models, link the tonies, delete what nothing points at, then add the audio.

    teddyCloud refuses a custom model whose audio_id and hash another custom
    model already carries, and a renamed model shares both with the model it
    replaces until that is deleted. So the new models are written without
    their audio first and get it only at the end. No tonie is ever without
    its model on the way. Should a write fail in between, a new model lacks
    its audio, which only stops teddyCloud from recognising that audio on a
    tonie that has no model set; every tonie linked here has one.
    """
    new_models = [step.new_model for step in todo.steps
                  if step.new_model and step.kind != RETITLE]
    for model in new_models:
        client.upsert_custom_tonie(replace(model, audio_ids=[], hashes=[]).to_json())
    for step in todo.steps:
        if step.kind != RETITLE:
            client.set_tag(step.ruid, tonie_model=step.model_id)
    if todo.retired:
        client.delete_custom_tonies(todo.retired)
    for model in new_models:
        if model.audio_ids and model.hashes:
            client.upsert_custom_tonie(model.to_json())
    # A renamed model keeps its id and audio, so it is written in one go.
    for step in todo.steps:
        if step.kind == RETITLE:
            client.upsert_custom_tonie(step.new_model.to_json())
    log.info("named %d tonie(s), deleted %d model(s)", len(todo.steps), len(todo.retired))


def library_headers(client: TeddyCloudClient) -> Dict[str, Mapping[str, Any]]:
    """The taf headers of the tafs fetched from the cloud, by file name."""
    try:
        files = client.list_dir(LIBRARY_FOLDER, special="library")
    except TeddyCloudError:
        return {}
    return {entry["name"]: entry["tafHeader"] for entry in files
            if (entry.get("tafHeader") or {}).get("valid")}


class Namer:
    """Names imported tonies, right after each import and once a day for the rest."""

    def __init__(self, client: Callable[[], TeddyCloudClient],
                 dumps: Callable[[], Dict[str, str]],
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.dumps = dumps
        self._catalog: TtlCache[None, Tuple[Catalog, AudioIndex]] = TtlCache(
            lambda _key: self._load_catalog(), CATALOG_TTL, clock)
        self._lock = threading.Lock()

    def catalog(self) -> Tuple[Catalog, AudioIndex]:
        return self._catalog.get(None)

    def _load_catalog(self) -> Tuple[Catalog, AudioIndex]:
        entries = self.client().tonies_json()
        return Catalog(entries), official_by_audio(entries)

    def official(self) -> Catalog:
        """The catalogue alone, for looking a tonie up by its model."""
        return self.catalog()[0]

    def preview(self, migrate: bool = False) -> Plan:
        """What the upkeep would do now, over every tonie of the latest import.

        With ``migrate`` also what only a person starts: moving NFC- models
        into this app's namespace and renaming models the rules now name better.
        """
        client = self.client()
        catalog, by_audio = self.catalog()
        return plan(list_tags(client), self.dumps(), catalog, by_audio, list_models(client),
                    library_headers(client), migrate=migrate, retitle=migrate)

    def upkeep(self, migrate: bool = False, expect: Optional[str] = None) -> Plan:
        """Plan and carry out the upkeep in one go, under the lock writes share.

        With ``expect``, the fingerprint of the plan a person was shown, nothing
        is written unless the plan is still that one; :class:`PlanChanged` says so.
        """
        with self._lock:
            todo = self.preview(migrate)
            if expect is not None and fingerprint(todo) != expect:
                raise PlanChanged()
            if not todo.empty:
                apply(self.client(), todo)
            return todo

    def name_one(self, ruid: str, path: str) -> None:
        """Name one tonie just imported, if teddyCloud cannot name it by itself."""
        with self._lock:
            client = self.client()
            catalog, by_audio = self.catalog()
            todo = plan(list_tags(client), {ruid: path}, catalog, by_audio, list_models(client),
                        library_headers(client), relink=False)
            if not todo.empty:
                apply(client, todo)

    def run(self, stop: threading.Event) -> None:
        """Run the upkeep once a day until ``stop``; never the renaming, which waits for a person."""
        while not stop.wait(UPKEEP_EVERY):
            try:
                self.upkeep()
            except Exception:
                log.exception("naming upkeep failed")
