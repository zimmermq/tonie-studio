"""Planning several albums at once: in which order, and onto which stickers.

A series of ten episodes is ten encodes and ten tags. Doing them one by one
means ten times the album page and ten times the assignment page for choices
that are the same each time, so a batch takes the album's own defaults and
hands out the free tags the box already knows.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

_DIGITS = re.compile(r"(\d+)")


def episode_order(name: str) -> Tuple:
    """Sort key that reads the numbers in a name as numbers.

    Episodes are named "Folge 1" to "Folge 10"; sorted as text, the tenth
    lands second and gets the second sticker.
    """
    return tuple(int(piece) if piece.isdigit() else piece.casefold()
                 for piece in _DIGITS.split(name))


def allocate(needs: Sequence[int], free: Sequence[str]) -> List[List[str]]:
    """Give each album as many free tags as it has parts, in order.

    An album either gets a tag for every part or none: half an album on a
    sticker stops in the middle. Once one does not fit, the later ones get
    nothing either, so the stickers always carry the first episodes and the
    ones still to assign are the last.
    """
    remaining = list(free)
    handed: List[List[str]] = []
    stopped = False
    for need in needs:
        if not stopped and need <= len(remaining):
            handed.append(remaining[:need])
            remaining = remaining[need:]
        else:
            handed.append([])
            stopped = True
    return handed
