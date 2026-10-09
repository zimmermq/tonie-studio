"""Planning several albums at once: in which order, and onto which stickers.

A series of ten episodes is ten encodes and ten tags. Doing them one by one
means ten times the album page and ten times the assignment page for choices
that are the same each time, so a batch takes the album's own defaults and
hands out the free tags the box already knows.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

_DIGITS = re.compile(r"(\d+)")

# How a batch's encodes get their stickers: the free ones handed out in order
# at once, or each one as it is put on the box.
AUTO, PLACE = "auto", "place"


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


@dataclass
class Slot:
    """One encode of a batch waiting for the sticker it will be played from."""

    job_id: str
    episode: str
    ruid: Optional[str] = None
    # Set once the encode has finished and taken whatever sticker was
    # reserved by then; a sticker placed later is published by the placer.
    settled: bool = False


# The sticker a batch encode was published on, by job id; None while it has none.
PlayedOn = Callable[[str], Optional[str]]


def _nowhere(_job_id: str) -> Optional[str]:
    return None


class Placing:
    """Hands the stickers put on the box to a batch's encodes, in order.

    Free stickers look alike, so the order they are placed in is the only
    way to know which one carries which episode. A sticker can be placed
    before its episode is encoded; it is reserved and filled when the
    encode finishes. A batch that hands its stickers out automatically is
    the same thing with every sticker reserved from the start. All state
    changes happen under ``lock``, because the encode thread and the
    request placing a sticker meet on ``settled``.
    """

    def __init__(self, slots: Sequence[Slot], by_hand: bool = True):
        self.slots = list(slots)
        # Whether stickers are placed on the box one by one; otherwise every
        # slot had its sticker reserved when the batch started.
        self.by_hand = by_hand
        self.lock = threading.Lock()

    def add(self, slot: Slot) -> None:
        with self.lock:
            self.slots.append(slot)

    def reserve(self, ruid: str, failed: Callable[[str], bool],
                played_on: PlayedOn = _nowhere) -> Tuple[Optional[Slot], bool]:
        """Give ``ruid`` to the next encode without a sticker.

        Returns the slot and whether it was already settled, in which case
        the caller publishes at once. A sticker already reserved here comes
        back as its own slot, not as a second episode. ``played_on`` names the
        sticker an encode was published on, if any: an encode that failed, or
        that went on another sticker by hand, gives its reservation up and is
        passed over.
        """
        with self.lock:
            for slot in self.slots:
                if slot.ruid is not None and _given_up(slot, failed, played_on):
                    slot.ruid = None
            for slot in self.slots:
                if slot.ruid == ruid:
                    return slot, False
            for slot in self.slots:
                if _open(slot, failed, played_on):
                    slot.ruid = ruid
                    return slot, slot.settled
            return None, False

    def settle(self, job_id: str) -> Optional[str]:
        """Mark an encode finished; returns the sticker reserved for it, if any."""
        with self.lock:
            for slot in self.slots:
                if slot.job_id == job_id:
                    slot.settled = True
                    return slot.ruid
        return None

    def next_episode(self, played_on: PlayedOn = _nowhere) -> str:
        """The episode the next sticker put on the box will get, for the page."""
        waiting = self.waiting(lambda job_id: False, played_on)
        return waiting.episode if waiting else ""

    def waiting(self, failed: Callable[[str], bool],
                played_on: PlayedOn = _nowhere) -> Optional[Slot]:
        """The next encode still without a sticker."""
        with self.lock:
            return next((slot for slot in self.slots if _open(slot, failed, played_on)), None)


def _given_up(slot: Slot, failed: Callable[[str], bool], played_on: PlayedOn) -> bool:
    """Whether a slot no longer needs the sticker reserved for it."""
    if failed(slot.job_id):
        return True
    published = played_on(slot.job_id)
    return published is not None and published != slot.ruid


def _open(slot: Slot, failed: Callable[[str], bool], played_on: PlayedOn) -> bool:
    """Whether a slot still waits for a sticker."""
    return slot.ruid is None and not failed(slot.job_id) and played_on(slot.job_id) is None
