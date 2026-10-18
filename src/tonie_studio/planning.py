"""Deciding what an encode will make: which tracks, how many tafs, under which names.

The album page plans one album the way a person arranged it; a batch plans
several the way each album's own page would offer them untouched. Both split
into parts by the same rules, which live here, so the routes only read the form
and the batch page only shows the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .album import Album, AlbumSummary, Track
from .albumnotes import match_by_tracks
from .batch import allocate, episode_order
from .encode import existing_taf, library_path, taf_exists
from .i18n import Problem
from .library import is_library, path_of, played, same, source_of
from .models import Model
from .parts import MOST_CHAPTERS, fewest_parts, part_name, split
from .publishing import free_tags
from .tags import list_tags
from .teddycloud import TeddyCloudClient

# A track and the name a person gave it, in the order it will play.
Chosen = Tuple[Track, str]


class PlanError(Problem, ValueError):
    """An album cannot be encoded as asked; a person reads why."""


@dataclass
class Part:
    """One taf of an album: its tracks, where it goes, and its episode name."""

    tracks: List[Chosen]
    target: str
    episode: str


def ordered_tracks(album: Album, order: str) -> List[Tuple[int, Track]]:
    """The album's tracks in the order the page sent, each with its row's position.

    ``order`` carries one track number per row of the page, as the rows were
    arranged; the titles are sent one per row too, so a track's title is the
    one at its position. Something that is no track number, or a track named
    a second time, is passed over and keeps its position for nobody else.
    """
    by_number = {track.number: track for track in album.tracks}
    ordered, seen = [], set()
    for position, part in enumerate(order.split(",")):
        number = int(part) if part.strip().isdigit() else None
        if number in by_number and number not in seen:
            seen.add(number)
            ordered.append((position, by_number[number]))
    return ordered


def chosen_tracks(album: Album, order: str, selected: Sequence[int],
                  titles: Sequence[str]) -> List[Chosen]:
    """The tracks to encode with their names, in the order the page put them in.

    Read as ``ordered_tracks`` reads the form; without an order, the album's.
    Reading a title by its track number instead of its row would hand the
    wrong name to every reordered track.
    """
    ordered = ordered_tracks(album, order) or list(enumerate(album.tracks))
    keep = set(selected)
    chosen = []
    for position, track in ordered:
        if track.number not in keep:
            continue
        name = titles[position].strip() if position < len(titles) else ""
        chosen.append((track, name or track.title))
    return chosen


def plan_parts(chosen: Sequence[Chosen], series: str, episode: str, parts: int) -> List[Part]:
    """Split the chosen tracks into ``parts`` tafs of about equal length.

    Refuses a split teddyCloud could not play: more parts than tracks, or so
    few that a taf would hold more chapters than the box can jump to.
    """
    if not chosen:
        raise PlanError("error.no_track")
    if parts < 1:
        raise PlanError("error.no_parts")
    if parts > len(chosen):
        raise PlanError("error.too_many_parts", tracks=len(chosen), parts=parts)
    if parts < fewest_parts(len(chosen)):
        raise PlanError("error.too_few_parts", tracks=len(chosen), chapters=MOST_CHAPTERS,
                        fewest=fewest_parts(len(chosen)))
    groups = split([track.duration for track, _name in chosen], parts)
    planned = []
    for number, indices in enumerate(groups, 1):
        name = part_name(episode, number, parts)
        planned.append(Part(tracks=[chosen[index] for index in indices],
                            target=library_path(series, name), episode=name))
    return planned


@dataclass
class BatchRow:
    """One album of a batch, as planned."""

    identifier: str
    album: Album
    series: str
    episode: str
    chosen: List[Chosen]
    parts: List[Part]
    occupied: bool
    # Whether a tag plays an existing taf; only meaningful when occupied.
    in_use: bool = False
    # Its tafs are there, fit the album and no tag plays them: what an
    # interrupted batch leaves behind. Such a row only needs its stickers.
    reusable: bool = False
    # The model already made from this album, if any.
    tonie: str = ""
    tags: List[str] = field(default_factory=list)
    # A sticker that plays this album's tonie from a taf that is gone: the
    # album is encoded again to that very path and the tonie keeps its model.
    rebuild_ruid: str = ""
    # Encoded on its own right now, from the album page: left out, or the
    # batch would encode the same album a second time beside it.
    busy: bool = False

    @property
    def rebuild(self) -> bool:
        return bool(self.rebuild_ruid)

    @property
    def targets(self) -> List[str]:
        return [part.target for part in self.parts]

    @property
    def skipped(self) -> bool:
        """Left out: busy, its tonie exists and plays, or its taf is there and cannot be reused."""
        if self.busy:
            return True
        if self.rebuild:
            return False
        return bool(self.tonie) or (self.occupied and not self.reusable)


def plan_batch(client: TeddyCloudClient, albums: Sequence[Tuple[str, Album]],
               archive_dir: Path, upload_dir: Path, series: Optional[Dict[str, str]] = None,
               episodes: Optional[Dict[str, str]] = None, tonies: Optional[Dict[str, str]] = None,
               models: Sequence[Model] = (), busy: Sequence[str] = ()) -> List[BatchRow]:
    """Each album with its default tracks, its taf paths and its stickers.

    Every album takes what its own page would offer untouched: the tracks
    that are not karaoke takes, in the album's order, the first cover, and as
    few parts as teddyCloud allows.

    An album a tonie was already made from is left out: known from the
    records (``tonies``, by album id) or recognised by its track list among
    ``models``, since the name typed back then may differ from the
    suggestion. A second copy is a decision for the album's own page. An
    album whose tafs exist but play on no tag is not encoded again, only
    given its stickers - if every part is there with as many chapters as the
    part has tracks; anything else there is left alone.

    ``busy`` names the targets encodes are writing right now; an album
    with one of them is left out.

    ``series`` and ``episodes`` hold what the page sent, by album id; an
    album missing from them gets the suggestion. A series sent empty stays
    empty, since that is a choice the album page allows too.
    """
    series, episodes = series or {}, episodes or {}
    tags = list_tags(client)
    playing = played(tag.source for tag in tags)
    rows = []
    for identifier, album in albums:
        row_series = (series[identifier] if identifier in series
                      else suggest_series(album, archive_dir, upload_dir)).strip()
        episode = (episodes.get(identifier) or "").strip() or album.title
        chosen = [(track, track.title) for track in album.tracks if not track.extra]
        parts = plan_parts(chosen, row_series, episode, fewest_parts(len(chosen)))
        occupied = any(taf_exists(client, part.target) for part in parts)
        in_use = any(same(source_of(part.target)) in playing for part in parts)
        rows.append(BatchRow(
            identifier=identifier, album=album, series=row_series, episode=episode,
            chosen=chosen, parts=parts, occupied=occupied, in_use=in_use,
            reusable=occupied and not in_use and _tafs_fit(client, parts),
            tonie=((tonies or {}).get(identifier)
                   or match_by_tracks([track.title for track in album.tracks], models) or ""),
        ))
        _plan_rebuild(client, rows[-1], tags)
        rows[-1].busy = any(same(target) in {same(path) for path in busy}
                            for target in rows[-1].targets)
    # Grouped by series and in episode order within one, which is also the
    # order the stickers are handed out in.
    rows.sort(key=lambda row: (row.series.casefold(), episode_order(row.episode)))

    waiting = [row for row in rows if not row.skipped and not row.rebuild]
    for row, tags in zip(waiting, allocate([len(row.parts) for row in waiting],
                                           free_tags(client)), strict=True):
        row.tags = tags
    return rows


def _plan_rebuild(client: TeddyCloudClient, row: BatchRow, tags: Sequence) -> None:
    """Make ``row`` encode its album again where its tonie's sticker expects the audio.

    Only for an album of one part: a tonie of several parts is several
    models, and which part belongs where is no longer known. The path is the
    sticker's own, not the one the batch would choose, so the sticker plays
    again without being touched and every other sticker sharing it too.
    """
    if not row.tonie or len(row.parts) != 1:
        return
    for tag in tags:
        if (tag.model and tag.model.lower() == row.tonie.lower() and is_library(tag.source)
                and not taf_exists(client, path_of(tag.source))):
            row.parts[0] = replace(row.parts[0], target=path_of(tag.source))
            row.occupied = row.reusable = False
            row.rebuild_ruid = tag.ruid
            return


def expected_targets(album: AlbumSummary, archive_dir: Path, upload_dir: Path) -> List[str]:
    """The library paths the album would be encoded to on the batch page, untouched.

    From the album list's summary alone, so the list can tell which albums
    are encoded without reading every track. The parts are counted from all
    tracks while a batch leaves karaoke takes out; right at the chapter limit
    the two can differ, and the album then looks not encoded. A name changed
    on the album page is not known here either.
    """
    series = suggest_series(album, archive_dir, upload_dir)
    episode = album.title or album.name
    parts = fewest_parts(album.track_count)
    return [library_path(series, part_name(episode, number, parts))
            for number in range(1, parts + 1)]


def suggest_series(album: Union[Album, AlbumSummary], archive_dir: Path,
                   upload_dir: Path) -> str:
    """The series an album most likely belongs to.

    Its artist when the tags name one; otherwise the folder holding it, the
    way a series is usually kept on disk. The archive and upload roots are
    not a name anyone chose, and neither is the folder an upload was spooled
    into directly below the upload root.
    """
    if album.artist:
        return album.artist
    parent = album.path.parent
    if parent in (archive_dir, upload_dir) or parent.parent == upload_dir:
        return ""
    return parent.name


def _tafs_fit(client: TeddyCloudClient, parts: Sequence[Part]) -> bool:
    """Whether every part's taf is there with exactly that part's tracks as chapters."""
    for part in parts:
        found = existing_taf(client, part.target)
        if found is None or found.tracks != len(part.tracks):
            return False
    return True
