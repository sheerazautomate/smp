"""Pagination bookkeeping for the SMP portal tables.

The portal renders DataTables with ``pagingType: "numbers"``: the pager is a row
of numbered links and there is *no* "Next" control. Reading the totals out of the
portal's own "Showing 1 to 5 of 18 entries" line is the only reliable way to know
whether a capture is complete.

Everything here is pure (no browser), so it can be tested against a real markup
fixture without signing in to the portal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: ``Showing 1 to 5 of 18 entries``, with the portal's various dash spellings.
_RANGE = re.compile(
    r"(?P<from>[\d,]+)\s*(?:to|–|—|-)\s*(?P<to>[\d,]+)\s*(?:of|/)\s*(?P<total>[\d,]+)",
    re.IGNORECASE,
)
#: ``Showing 1 to 5 of 18 entries (filtered from 240 total entries)``
_FILTERED = re.compile(r"filtered from\s+(?P<total>[\d,]+)", re.IGNORECASE)
#: DataTables' empty-state strings.
_EMPTY = re.compile(
    r"no\s+(?:matching\s+)?(?:entries|records|data|results)", re.IGNORECASE
)


@dataclass(frozen=True)
class PagerState:
    """What one table's pager says about itself."""

    table_id: str
    from_row: int
    to_row: int
    total_rows: int
    has_pager: bool
    length_value: str | None
    length_has_all: bool
    page_indexes: tuple[int, ...]
    current_page: int | None
    raw_text: str = ""

    @property
    def page_size(self) -> int:
        """Rows currently rendered — 0 when the table is empty."""
        if self.to_row <= 0 or self.from_row <= 0:
            return 0
        return max(0, self.to_row - self.from_row + 1)

    @property
    def page_count(self) -> int:
        """Pages the portal says exist, from the numbered links or the totals."""
        if self.total_rows <= 0:
            return 0
        if self.page_indexes:
            return max(self.page_indexes) + 1
        size = self.page_size or self.total_rows
        return max(1, -(-self.total_rows // size))

    @property
    def is_paginated(self) -> bool:
        """True when the portal has more pages than the one being shown.

        DataTables renders the pager wrapper even for a single page, so the
        presence of a pager is not evidence of pagination; the counts are.
        """
        if self.total_rows <= 0:
            return False
        return self.page_count > 1 or self.total_rows > self.page_size

    @property
    def can_show_all(self) -> bool:
        return self.length_has_all and str(self.length_value or "") != "-1"

    def label(self) -> str:
        return self.table_id or "unnamed table"


def parse_info_text(text: str) -> tuple[int, int, int] | None:
    """Return ``(from_row, to_row, total_rows)`` from a DataTables info line.

    Returns ``None`` when the text cannot be interpreted, so the caller can treat
    completeness as unknown instead of guessing.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return None
    if _EMPTY.search(text):
        return (0, 0, 0)

    match = _RANGE.search(text)
    if match:
        try:
            from_row = int(match.group("from").replace(",", ""))
            to_row = int(match.group("to").replace(",", ""))
            total = int(match.group("total").replace(",", ""))
        except ValueError:
            return None
        # "filtered from N" reports the unfiltered total; trust the visible range.
        return (from_row, to_row, total)

    filtered = _FILTERED.search(text)
    if filtered:
        try:
            total = int(filtered.group("total").replace(",", ""))
        except ValueError:
            return None
        return (1, total, total)

    return None


def pager_state(raw: dict[str, Any]) -> PagerState | None:
    """Build a :class:`PagerState` from one ``PAGER_INFO_JS`` record."""
    if not isinstance(raw, dict):
        return None
    parsed = parse_info_text(str(raw.get("infoText") or ""))
    if parsed is None:
        return None
    from_row, to_row, total = parsed

    page_indexes: list[int] = []
    for value in raw.get("numericIdxs") or []:
        try:
            page_indexes.append(int(value))
        except (TypeError, ValueError):
            continue

    current = raw.get("activeIdx")
    try:
        current_page = int(current) if current is not None else None
    except (TypeError, ValueError):
        current_page = None

    return PagerState(
        table_id=str(raw.get("tableId") or ""),
        from_row=from_row,
        to_row=to_row,
        total_rows=total,
        has_pager=bool(raw.get("hasPager")),
        length_value=(
            None if raw.get("lengthValue") is None else str(raw.get("lengthValue"))
        ),
        length_has_all=bool(raw.get("lengthHasAll")),
        page_indexes=tuple(sorted(set(page_indexes))),
        current_page=current_page,
        raw_text=str(raw.get("infoText") or ""),
    )


def pager_states(raw_records: list[dict[str, Any]]) -> dict[int, PagerState]:
    """Map table DOM index → :class:`PagerState` for every interpretable table."""
    states: dict[int, PagerState] = {}
    for raw in raw_records or []:
        state = pager_state(raw)
        if state is None:
            continue
        try:
            index = int(raw.get("tableIndex"))
        except (TypeError, ValueError):
            continue
        states[index] = state
    return states


def missing_rows(expected_total: int | None, captured: int) -> int:
    if expected_total is None:
        return 0
    return max(0, int(expected_total) - int(captured))
