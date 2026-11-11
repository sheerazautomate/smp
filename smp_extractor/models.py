from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtractedTable:
    """A normalized HTML table, merged across UI states and pagination."""

    emis_code: str
    table_index: int
    table_ref: str
    title: str
    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    capture_contexts: list[str] = field(default_factory=list)
    # Pagination accounting. ``rows_expected`` comes from the portal's own
    # "Showing 1 to 5 of 18 entries" line; ``None`` means the table is not
    # paginated, so completeness cannot (and need not) be judged.
    rows_expected: int | None = None
    pages_captured: int = 0
    pages_expected: int | None = None
    pager_kind: str = ""
    #: Which rendered view this table's pager belongs to (``default view`` or
    #: ``tab: <label>``), so an incomplete table can be revisited in context.
    pager_view: str = ""
    _seen_snapshots: set[tuple[tuple[str, ...], ...]] = field(
        default_factory=set, repr=False
    )
    # Rows already captured from an earlier snapshot. Needed because pagination
    # states overlap: capturing page 1 and then "show all" re-renders rows 1-5.
    _seen_rows: set[tuple[str, ...]] = field(default_factory=set, repr=False)

    def add_snapshot(
        self,
        rows: list[list[str]],
        source_url: str,
        context: str,
    ) -> bool:
        """Append a new rendered state once; preserve duplicate rows within it."""
        fingerprint = tuple(tuple(str(cell) for cell in row) for row in rows)
        if source_url and source_url not in self.source_urls:
            self.source_urls.append(source_url)
        if context and context not in self.capture_contexts:
            self.capture_contexts.append(context)
        if fingerprint in self._seen_snapshots:
            return False

        self._seen_snapshots.add(fingerprint)
        # Skip rows a previous snapshot already contributed, but keep genuine
        # duplicates that occur *within* one rendered page.
        fresh: list[list[str]] = []
        for row in rows:
            key = tuple(str(cell) for cell in row)
            if key in self._seen_rows:
                continue
            fresh.append(list(row))
        self._seen_rows.update(tuple(str(cell) for cell in row) for row in rows)
        self.rows.extend(fresh)
        return True

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.headers)

    @property
    def rows_missing(self) -> int:
        if self.rows_expected is None:
            return 0
        return max(0, self.rows_expected - self.row_count)

    @property
    def is_complete(self) -> bool:
        """True when every row the portal reported has been captured."""
        return self.rows_missing == 0


@dataclass
class SchoolResult:
    emis_code: str
    url: str
    status: str = "pending"
    page_title: str = ""
    tables: list[ExtractedTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def row_count(self) -> int:
        return sum(table.row_count for table in self.tables)

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def incomplete_tables(self) -> list[ExtractedTable]:
        return [table for table in self.tables if not table.is_complete]

    @property
    def rows_expected(self) -> int:
        return sum(
            table.rows_expected if table.rows_expected is not None else table.row_count
            for table in self.tables
        )
