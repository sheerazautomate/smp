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
    _seen_snapshots: set[tuple[tuple[str, ...], ...]] = field(
        default_factory=set, repr=False
    )

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
        self.rows.extend([list(row) for row in rows])
        return True

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.headers)


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
