"""Cuts a long album into parts a tonie can hold.

teddyCloud keeps room for 100 chapters in a taf and refuses a new one from the
99th on. Its encoder ignores that refusal and keeps writing, so a 258-track
audiobook does not fail: it becomes 98 chapters and a 99th holding the other
160 tracks, which no knock on the box can skip through. Nothing says so.

The only honest answer is more tafs. Each part becomes its own tonie, cut
between tracks so that the parts play for about equally long - counting tracks
instead would put an audiobook's long chapters all in one part.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

# teddyCloud's TONIEFILE_MAX_CHAPTERS is 100, and a new chapter is refused
# once 99 exist (src/toniefile.c, toniefile_new_chapter).
MOST_CHAPTERS = 99


def fewest_parts(tracks: int) -> int:
    """The smallest number of parts that keeps every one within the limit."""
    return max(1, math.ceil(tracks / MOST_CHAPTERS))


def split(durations: Sequence[Optional[float]], parts: int) -> List[List[int]]:
    """Track indices per part: consecutive, in order, about equally long.

    A duration that is unknown counts as the average of the known ones, or as
    one when none is known, which makes the cut fall by count.
    """
    count = len(durations)
    if parts < 1 or parts > count:
        raise ValueError(f"{count} tracks cannot make {parts} parts")
    if parts < fewest_parts(count):
        raise ValueError(f"{count} tracks need at least {fewest_parts(count)} parts")

    known = [duration for duration in durations if duration]
    fallback = sum(known) / len(known) if known else 1.0
    lengths = [duration if duration else fallback for duration in durations]

    # cumulative[i] is the playing time before track i.
    cumulative = [0.0]
    for length in lengths:
        cumulative.append(cumulative[-1] + length)
    total = cumulative[-1]

    cuts = [0]
    for part in range(1, parts):
        previous = cuts[-1]
        left = parts - part
        # Every later part needs at least one track and at most the limit.
        lowest = max(previous + 1, count - left * MOST_CHAPTERS)
        highest = min(previous + MOST_CHAPTERS, count - left)
        goal = total * part / parts
        cuts.append(min(range(lowest, highest + 1),
                        key=lambda cut: abs(cumulative[cut] - goal)))
    cuts.append(count)

    return [list(range(cuts[index], cuts[index + 1])) for index in range(parts)]


def part_name(name: str, part: int, parts: int) -> str:
    """The episode name of one part; a whole album keeps its own.

    Always German, whatever language the page is read in: the name is stored
    in the model and in the taf's library path, and a path that changed with
    the reader would orphan every part encoded before.
    """
    return name if parts == 1 else f"{name} - Teil {part} von {parts}"
