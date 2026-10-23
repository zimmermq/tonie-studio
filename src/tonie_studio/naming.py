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
and cover where they are unambiguous. A folder further up that names a
language, such as "English" or "fr", gives the model that language, German
otherwise. The model carries the taf's audio id and hash, so a second tonie
with the same audio reuses it.

Two upkeep tasks keep that right later. A custom model whose audio the
catalogue has since listed is replaced by the official model, which describes
the recording at least as well and brings the official cover and track names.
And models an earlier tool made under the NFC- prefix are moved into this
app's namespace, so every model it did not get from the catalogue is one it
looks after. A model of this app's that lacks the audio its sticker plays,
as an upkeep cut short leaves it, gets that audio back (see apply).

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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .cache import TtlCache
from .encode import list_folder
from .languages import from_path, spoken
from .models import (CATEGORIES, MODEL_PREFIX, WRITING, Model, highest_id_used, list_models,
                     note_id_used)
from .tags import TonieTag, list_tags
from .teddycloud import TeddyCloudClient, TeddyCloudError

log = logging.getLogger(__name__)

# What a dump speaks when none of its folders names a language.
LANGUAGE = "de-de"

# Where teddyCloud keeps the tafs it fetched from the cloud, named <audioId>.taf.
LIBRARY_PREFIX = "lib://by/audioID/"
LIBRARY_FOLDER = "/by/audioID"

# How a tag's source names a taf in the library.
LIBRARY_SOURCE = "lib://"

# The prefix of the models an earlier command-line tool made for imported tonies.
OLD_PREFIX = "NFC-"

# What names a product of several figures: "Tolle Berufe - Set", "Clever Tonies
# Set", "Multipack", "3 Pack"; a whole word, so not "Sunset". The cost: a single
# tonie whose title uses "set" as a word ("Set Sail") counts as a set and gets a
# model of its own, named after its dump, instead of the official one.
SET_WORDS = re.compile(r"\bset\b|\bmulti-?pack\b|\b\d+[\s-]?pack\b", re.IGNORECASE)
# One figure sold with extras: a Creative-Tonie's "(Starter Set)".
STARTER_SET = re.compile(r"\bstarter[\s-]?set\b", re.IGNORECASE)


def names_a_set(text: Optional[str]) -> bool:
    """Whether ``text``, a catalogue series, episode or title, names a product of several figures."""
    return bool(text and SET_WORDS.search(text) and not STARTER_SET.search(text))

# What a step does to a tonie.
OFFICIAL = "official"  # link to the catalogue's model
NEW = "new"            # make a custom model and link to it
REUSE = "reuse"        # link to the custom model that already carries the audio
PROMOTE = "promote"    # replace a custom model the catalogue now covers
RENAME = "rename"      # move an NFC- model into this app's namespace
RETITLE = "retitle"    # give a model of ours the better name the rules find now
REPAIR = "repair"      # give a model of ours the audio its sticker plays

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


def dump_language(path: str) -> str:
    """What the tonie dumped at ``path`` speaks: what its folders name, else LANGUAGE."""
    return from_path(path) or LANGUAGE


class Catalog:
    """The catalogue's entries by language, looked up by normalised name.

    A dump is only compared with the entries of its own language, in any
    region: an English one with en-us and en-gb alike.
    """

    def __init__(self, entries: Iterable[Mapping[str, Any]]):
        entries = list(entries)
        self.by_model = {entry["model"]: entry for entry in entries if entry.get("model")}
        self.by_series: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
        for entry in entries:
            if entry.get("model") and entry.get("series"):
                key = (spoken(entry.get("language")), norm(entry["series"]))
                self.by_series.setdefault(key, []).append(entry)

    def describe(self, path: str) -> Name:
        """The name of the tonie dumped at ``path``, ``<...>/<series>/<episode>.nfc``."""
        dump = PurePosixPath(path)
        series, episode = dump.parent.name, dump.stem
        entries = self.by_series.get((spoken(dump_language(path)), norm(series)))
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
        return any(names_a_set(entry.get(key)) for key in ("series", "episodes", "title"))

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
        # "Clever Tonies Set - Inventions and Innovation" is a range of sets,
        # whose episode names the set the figure belongs to.
        if names_a_set(series) and entry.get("episodes") and not names_a_set(entry["episodes"]):
            series = entry["episodes"]
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
         relink: bool = True, retitle: bool = False, highest: int = 0,
         repair: bool = True,
         source_headers: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Plan:
    """What naming the tonies in ``dumps`` (``{ruid: dump path}``) takes.

    ``tags`` is teddyCloud's tag index, whose model is the one teddyCloud names
    the tonie after now, from its content JSON or found by its audio.
    ``headers`` holds the taf headers of the library's ``by/audioID`` folder by
    file name. With ``relink`` every tonie on a model the catalogue now covers
    is moved to the official one; with ``migrate`` the NFC- models move into
    this app's namespace too; with ``retitle`` a model of this app named after
    a dump in ``dumps`` is renamed when the rules now find a better name, such
    as a set figure's official title. New ids go above ``highest``, the
    highest number ever handed out. With ``repair`` a model of this app's
    without audio, on any sticker, gets the audio that sticker plays (see
    _repairs); ``source_headers`` holds the taf headers of such stickers
    outside ``by/audioID``, by source.
    """
    tags = list(tags)
    by_ruid = {tag.ruid: tag for tag in tags}
    promoted = promotions(models, by_audio, catalog.unsuitable) if relink else {}
    ids = _Ids([model.id for model in models] + [f"{MODEL_PREFIX}{highest:04d}"])

    renamed: Dict[str, Model] = {}
    if migrate:
        for model in models:
            if model.id.upper().startswith(OLD_PREFIX) and model.id not in promoted:
                renamed[model.id] = replace(model, id=ids.next())
    available = [model for model in models
                 if model.id not in promoted and model.id not in renamed] + list(renamed.values())

    result = Plan(retired=sorted(set(promoted) | set(renamed)))
    handled = _moves(tags, dumps, promoted, renamed, result)

    for ruid, path in dumps.items():
        tag = by_ruid.get(ruid)
        if ruid in handled or tag is None or not tag.source:
            continue
        if tag.model and not catalog.unsuitable(tag.model):
            continue
        audio = _audio_of(tag, headers)
        if audio is None:
            result.skipped.append((path, tag.source))
            continue
        step = _naming_step(tag, path, audio, catalog, by_audio, available, ids)
        result.steps.append(step)
        if step.kind == NEW:
            available.append(step.new_model)
            handled.add(ruid)

    if repair:
        # A renamed model no tag moves onto is only deleted, never written.
        unwritten = ({model.id for model in renamed.values()}
                     - {step.model_id for step in result.steps if step.kind == RENAME})
        kept = [model for model in available if model.id not in unwritten]
        result.steps.extend(_repairs(tags, dumps, handled, kept, headers, source_headers or {}))
    if retitle:
        retitles = _retitles(by_ruid, dumps, handled, catalog, by_audio, models, headers)
        _fold_repairs(result, retitles)
        result.steps.extend(retitles)
    return result


def _repairs(tags: List[TonieTag], dumps: Mapping[str, str], handled: set,
             available: List[Model], headers: Mapping[str, Mapping[str, Any]],
             source_headers: Mapping[str, Mapping[str, Any]]) -> List[Step]:
    """Audio for this app's models that lack the audio their sticker plays.

    apply writes new models without audio and adds it last, so an upkeep cut
    short in between leaves such a model, and nothing else would ever add it.
    A model is only given audio no other model keeps after the plan
    (``available``, which holds the plan's own new models too): teddyCloud
    refuses two custom models with the same audio id and hash, and a model of
    a sticker given audio another model already carries rightly has none. A
    stream, or a taf whose header is unknown, has no audio to add.
    """
    carried = {(str(audio_id), str(sha1).upper()) for model in available
               for audio_id in model.audio_ids for sha1 in model.hashes}
    bare = {model.id: model for model in available
            if model.id.startswith(MODEL_PREFIX) and not (model.audio_ids and model.hashes)}
    steps = []
    for tag in tags:
        model = bare.get(tag.model)
        if model is None or tag.ruid in handled:
            continue
        audio = _audio_of(tag, headers) or _audio_from(source_headers.get(tag.source))
        if audio is None or audio in carried:
            continue
        carried.add(audio)
        del bare[model.id]
        steps.append(Step(REPAIR, tag.ruid, model.id, path=dumps.get(tag.ruid, ""),
                          label=tag.label,
                          new_model=replace(model, audio_ids=[audio[0]], hashes=[audio[1]])))
    return steps


def _fold_repairs(result: Plan, retitles: List[Step]) -> None:
    """Give a retitled model the audio a repair adds, and drop the repair.

    Both write the whole model, so of two steps for one model the later
    would undo the earlier.
    """
    retitled = {step.model_id: step for step in retitles}
    kept = []
    for step in result.steps:
        target = retitled.get(step.model_id) if step.kind == REPAIR else None
        if target is None:
            kept.append(step)
            continue
        target.new_model = replace(target.new_model, audio_ids=step.new_model.audio_ids,
                                   hashes=step.new_model.hashes)
    result.steps = kept


class _Ids:
    """Model ids for a plan: each the next number after every id taken so far."""

    def __init__(self, taken: Iterable[str]):
        self.taken = list(taken)

    def next(self) -> str:
        used = [int(model_id[len(MODEL_PREFIX):]) for model_id in self.taken
                if model_id.startswith(MODEL_PREFIX) and model_id[len(MODEL_PREFIX):].isdigit()]
        model_id = f"{MODEL_PREFIX}{max(used, default=0) + 1:04d}"
        self.taken.append(model_id)
        return model_id


def _moves(tags: List[TonieTag], dumps: Mapping[str, str], promoted: Mapping[str, str],
           renamed: Mapping[str, Model], result: Plan) -> set:
    """Steps moving tags off promoted and renamed models; returns the ruids moved.

    A renamed model is written once, with the first tag that moves onto it.
    """
    handled, written = set(), set()
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
    return handled


def _audio_of(tag: TonieTag, headers: Mapping[str, Mapping[str, Any]]
              ) -> Optional[Tuple[str, str]]:
    """The tag's audio as (audio id, upper-case hash), from the library's taf headers."""
    header = (headers.get(tag.source[len(LIBRARY_PREFIX):])
              if tag.source.startswith(LIBRARY_PREFIX) else None)
    return _audio_from(header)


def _audio_from(header: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    if not header:
        return None
    return str(header["audioId"]), str(header["sha1Hash"]).upper()


def _naming_step(tag: TonieTag, path: str, audio: Tuple[str, str], catalog: Catalog,
                 by_audio: AudioIndex, available: List[Model], ids: _Ids) -> Step:
    """What names one imported tonie: the official model, ours with its audio, or a new one."""
    current = tag.model
    official = current or by_audio.get(audio)
    if official and catalog.is_stub(official):
        official = None
    if official and catalog.is_set(official):
        name = catalog.figure(path, official)
    elif official:
        return Step(OFFICIAL, tag.ruid, official, path=path)
    else:
        name = catalog.describe(path)
        if name.model:
            return Step(OFFICIAL, tag.ruid, name.model, name=name, path=path)

    existing = next((model for model in available if audio[0] in map(str, model.audio_ids)
                     and audio[1] in [sha1.upper() for sha1 in model.hashes]), None)
    if existing:
        return Step(REUSE, tag.ruid, existing.id, name=name, path=path, replaces=current)
    model = Model(id=ids.next(), series=name.series, episode=name.episode,
                  language=dump_language(path),
                  category=name.category if name.category in CATEGORIES else "",
                  picture=name.pic, audio_ids=[audio[0]], hashes=[audio[1]])
    return Step(NEW, tag.ruid, model.id, name=name, path=path, new_model=model, replaces=current)


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
        language = _corrected_language(model.language, path)
        if (model.series, model.episode, model.language) == (name.series, name.episode, language):
            continue
        done.add(model.id)
        steps.append(Step(RETITLE, ruid, model.id, name=name, path=path,
                          new_model=replace(model, series=name.series, episode=name.episode,
                                            language=language),
                          label=tag.label))
    return steps


def _corrected_language(language: str, path: str) -> str:
    """The language a model of an imported tonie should have, given its dump's ``path``.

    German is what a model gets when nothing names its language, so a German
    one whose folder names another language takes that. Any other language
    was picked by a person and stays, even against the folder. The cost: a
    person who set German on a dump from an English folder sees it reverted.
    """
    named = from_path(path)
    if named and language == LANGUAGE and spoken(named) != spoken(LANGUAGE):
        return named
    return language


def apply(client: TeddyCloudClient, todo: Plan,
          progress: Optional[Callable[[str, int, int], None]] = None) -> None:
    """Write the models, link the tonies, delete what nothing points at, then add the audio.

    ``progress`` is told ("names", done, total) before each call to teddyCloud.

    teddyCloud refuses a custom model whose audio_id and hash another custom
    model already carries, and a renamed model shares both with the model it
    replaces until that is deleted. So the new models are written without
    their audio first and get it only at the end. No tonie is ever without
    its model on the way. Should a write fail in between, a new model lacks
    its audio, which only stops teddyCloud from recognising that audio on a
    tonie that has no model set; every tonie linked here has one. The next
    plan adds the missing audio (see _repairs).
    """
    whole = (RETITLE, REPAIR)
    new_models = [step.new_model for step in todo.steps
                  if step.new_model and step.kind not in whole]
    linked = [step for step in todo.steps if step.kind not in whole]
    with_audio = [model for model in new_models if model.audio_ids and model.hashes]
    retitled = [step for step in todo.steps if step.kind in whole]
    total = len(new_models) + len(linked) + bool(todo.retired) + len(with_audio) + len(retitled)
    count = iter(range(total))

    def next_call() -> None:
        if progress:
            progress("names", next(count), total)

    for model in new_models:
        next_call()
        client.upsert_custom_tonie(replace(model, audio_ids=[], hashes=[]).to_json())
    for step in linked:
        next_call()
        client.set_tag(step.ruid, tonie_model=step.model_id)
    if todo.retired:
        next_call()
        client.delete_custom_tonies(todo.retired)
    for model in with_audio:
        next_call()
        client.upsert_custom_tonie(model.to_json())
    # A retitled model keeps its id and audio, and a repaired one only gains
    # audio nothing else carries, so each is written in one go, after the
    # models that held any of it are gone.
    for step in retitled:
        next_call()
        client.upsert_custom_tonie(step.new_model.to_json())
    log.info("named %d tonie(s), deleted %d model(s)", len(todo.steps), len(todo.retired))


def source_headers(client: TeddyCloudClient, tags: Iterable[TonieTag],
                   models: Iterable[Model]) -> Dict[str, Mapping[str, Any]]:
    """The taf headers, by source, of stickers whose model of ours has no audio.

    Only for tafs outside ``by/audioID``, whose headers library_headers has
    already. Such stickers are rare, so their folders are listed one by one.
    """
    bare = {model.id for model in models
            if model.id.startswith(MODEL_PREFIX) and not (model.audio_ids and model.hashes)}
    wanted: Dict[str, Dict[str, str]] = {}
    for tag in tags:
        if tag.model not in bare or not tag.source.startswith(LIBRARY_SOURCE) \
                or tag.source.startswith(LIBRARY_PREFIX):
            continue
        folder, _, name = ("/" + tag.source[len(LIBRARY_SOURCE):]).rpartition("/")
        wanted.setdefault(folder or "/", {})[name] = tag.source
    found = {}
    for folder, names in wanted.items():
        try:
            entries = (client.list_dir("/", special="library") if folder == "/"
                       else list_folder(client, folder))
        except TeddyCloudError:
            continue
        for entry in entries:
            header = entry.get("tafHeader") or {}
            if entry.get("name") in names and header.get("valid"):
                found[names[entry["name"]]] = header
    return found


def library_headers(client: TeddyCloudClient) -> Dict[str, Mapping[str, Any]]:
    """The taf headers of the tafs fetched from the cloud, by file name."""
    try:
        files = list_folder(client, LIBRARY_FOLDER)
    except TeddyCloudError:
        return {}
    return {entry["name"]: entry["tafHeader"] for entry in files
            if (entry.get("tafHeader") or {}).get("valid")}


class Namer:
    """Names imported tonies, right after each import and once a day for the rest."""

    def __init__(self, client: Callable[[], TeddyCloudClient],
                 dumps: Callable[[], Dict[str, str]],
                 clock: Callable[[], float] = time.monotonic,
                 data_dir: Optional[Path] = None):
        self.client = client
        self.dumps = dumps
        # Where the highest model number handed out is kept; see next_model_id.
        self.data_dir = data_dir
        self._catalog: TtlCache[None, Tuple[Catalog, AudioIndex]] = TtlCache(
            lambda _key: self._load_catalog(), CATALOG_TTL, clock)

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
        tags, models = list_tags(client), list_models(client)
        return plan(tags, self.dumps(), catalog, by_audio, models,
                    library_headers(client), migrate=migrate, retitle=migrate,
                    highest=highest_id_used(self.data_dir),
                    source_headers=source_headers(client, tags, models))

    def upkeep(self, migrate: bool = False, expect: Optional[str] = None,
               progress: Optional[Callable[[str, int, int], None]] = None) -> Plan:
        """Plan and carry out the upkeep in one go, under the lock all model writes share.

        With ``expect``, the fingerprint of the plan a person was shown, nothing
        is written unless the plan is still that one; :class:`PlanChanged` says so.
        ``progress`` goes to :func:`apply`.
        """
        # The catalogue first: tonies.json is large, and loading it while
        # holding the lock would hold up every other write for that long.
        self.catalog()
        with WRITING:
            todo = self.preview(migrate)
            if expect is not None and fingerprint(todo) != expect:
                raise PlanChanged()
            if not todo.empty:
                self._note_ids(todo)
                apply(self.client(), todo, progress)
            return todo

    def name_one(self, ruid: str, path: str) -> None:
        """Name one tonie just imported, if teddyCloud cannot name it by itself."""
        catalog, by_audio = self.catalog()
        with WRITING:
            client = self.client()
            todo = plan(list_tags(client), {ruid: path}, catalog, by_audio, list_models(client),
                        library_headers(client), relink=False,
                        highest=highest_id_used(self.data_dir), repair=False)
            if not todo.empty:
                self._note_ids(todo)
                apply(client, todo)

    def _note_ids(self, todo: Plan) -> None:
        """Record the ids a plan hands out before they are written."""
        for step in todo.steps:
            if step.new_model is not None:
                note_id_used(self.data_dir, step.new_model.id)

    def run(self, stop: threading.Event) -> None:
        """Run the upkeep once a day until ``stop``; never the renaming, which waits for a person."""
        while not stop.wait(UPKEEP_EVERY):
            try:
                self.upkeep()
            except Exception:
                log.exception("naming upkeep failed")
