"""The columns of a list page: what the list can be ordered by, described once.

A list page shows its orders three times: as the entries of its order menu,
as its column headings, and to its script, which orders the rows the server
sent. All three come from the same Column, so a column added or changed is
one line where the page lists its columns, and the three cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Column:
    """One column a list page can be ordered by."""

    key: str  # the data-<key> every row carries for it
    heading: str  # translation key of its heading
    choice: str = ""  # translation key in the order menu, where that says more than the heading
    number: bool = False  # ordered as numbers rather than as text
    desc: bool = False  # first ordered largest first: newest, most chapters


def orders(columns: Iterable[Column]) -> Dict[str, Dict[str, bool]]:
    """What the list's script needs of each column to order by it."""
    return {column.key: {"number": column.number, "desc": column.desc} for column in columns}


def filters(tags: Iterable[Iterable[str]], order: Sequence[str]) -> Tuple[Counter, List[str]]:
    """How many rows each filter keeps, and the filters worth a chip.

    ``tags`` gives each row's filters. Offered are those of ``order``, in that
    order, that keep any row: a chip that empties the list helps nobody.
    """
    counts = Counter(tag for row in tags for tag in row)
    return counts, [tag for tag in order if counts[tag]]
