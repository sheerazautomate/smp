from __future__ import annotations

from typing import Any

from .models import ExtractedTable


def _text(value: Any) -> str:
    """Normalize presentation whitespace without coercing values to numbers."""
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _expand_spans(raw_rows: list[dict[str, Any]]) -> list[list[str]]:
    """Expand HTML rowspan/colspan cells to a rectangular list of strings."""
    expanded: list[list[str]] = []
    pending: dict[int, tuple[str, int]] = {}

    for raw_row in raw_rows:
        current: dict[int, str] = {}
        next_pending: dict[int, tuple[str, int]] = {}

        for column, (value, remaining) in pending.items():
            current[column] = value
            if remaining > 1:
                next_pending[column] = (value, remaining - 1)

        cursor = 0
        for cell in raw_row.get("cells", []):
            while cursor in current:
                cursor += 1

            colspan = _positive_int(cell.get("colspan", 1))
            rowspan = _positive_int(cell.get("rowspan", 1))
            while any(column in current for column in range(cursor, cursor + colspan)):
                cursor += 1

            value = _text(cell.get("text", ""))
            for column in range(cursor, cursor + colspan):
                current[column] = value
                if rowspan > 1:
                    next_pending[column] = (value, rowspan - 1)
            cursor += colspan

        width = max(current, default=-1) + 1
        expanded.append([current.get(column, "") for column in range(width)])
        pending = next_pending

    return expanded


def _unique_headers(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, name in enumerate(names, start=1):
        base = _text(name) or f"Column {index}"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base} ({seen[base]})")
    return result


def normalize_table(
    snapshot: dict[str, Any],
    emis_code: str,
    source_url: str,
    context: str = "initial",
) -> ExtractedTable:
    """Turn a browser-extracted table snapshot into a stable row/column model.

    A table with row-header cells mixed with data cells (common for school
    profile key/value tables) is treated as data, not as a column-header row.
    """
    raw_rows: list[dict[str, Any]] = snapshot.get("rows") or []
    grid = _expand_spans(raw_rows)

    header_indexes = [
        index for index, row in enumerate(raw_rows) if row.get("in_thead")
    ]
    if not header_indexes and raw_rows:
        first_cells = raw_rows[0].get("cells", [])
        if first_cells and all(
            str(cell.get("tag", "")).upper() == "TH" for cell in first_cells
        ):
            has_column_semantics = any(
                str(cell.get("scope", "")).casefold() == "col"
                or str(cell.get("role", "")).casefold() == "columnheader"
                for cell in first_cells
            )
            has_row_header = any(
                str(cell.get("scope", "")).casefold() == "row"
                or _positive_int(cell.get("colspan", 1)) > 1
                for cell in first_cells
            )
            if has_column_semantics or not has_row_header:
                header_indexes = [0]

    width = max([len(row) for row in grid] or [0])
    header_names: list[str] = []
    if header_indexes:
        for column in range(width):
            parts: list[str] = []
            for row_index in header_indexes:
                value = _text(grid[row_index][column] if column < len(grid[row_index]) else "")
                if value and value not in parts:
                    parts.append(value)
            header_names.append(" / ".join(parts) if parts else f"Column {column + 1}")
        data_indexes = [i for i in range(len(grid)) if i not in set(header_indexes)]
    else:
        header_names = [f"Column {column + 1}" for column in range(width)]
        data_indexes = list(range(len(grid)))

    headers = _unique_headers(header_names)
    rows: list[list[str]] = []
    for row_index in data_indexes:
        row = grid[row_index]
        normalized = [_text(value) for value in row]
        if not any(normalized):
            continue
        if len(normalized) < len(headers):
            normalized.extend([""] * (len(headers) - len(normalized)))
        elif len(normalized) > len(headers):
            # Defensive fallback for malformed HTML whose spans exceed the
            # header width; do not silently discard the extra cells.
            while len(headers) < len(normalized):
                headers.append(f"Column {len(headers) + 1}")
        rows.append(normalized)

    index = snapshot.get("index", 0)
    try:
        table_index = int(index)
    except (TypeError, ValueError):
        table_index = 0

    title = _text(snapshot.get("title", "")) or f"Table {table_index + 1}"
    dom_id = _text(snapshot.get("id", ""))
    table_ref = dom_id or f"dom-index:{table_index}"

    table = ExtractedTable(
        emis_code=emis_code,
        table_index=table_index,
        table_ref=table_ref,
        title=title,
        headers=headers,
    )
    table.add_snapshot(rows, source_url, context)
    return table
