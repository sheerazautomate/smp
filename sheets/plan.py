"""Turn an SMP export bundle into the cell ranges the master sheet expects.

The master workbook (``SMP Master Sheet Marhan Male``) is maintained by hand, so
this module deliberately treats its layout as configuration rather than guessing
it:

* the five tabs we fill each hold a single six-school block,
* rows 1-7 are the data block (header + six schools),
* row 8 is the block's header row and rows 9-14 are its per-school totals,
* the received tabs pair a date and a quantity per receipt (``Date``/``Quantity``),
* the consumption tabs have one column per service day (a date in the header).

Nothing here talks to Google. ``build_writes`` returns plain
(:class:`SheetWrite`) values so the mapping can be tested without credentials,
and the sync script is a thin loop over the result.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Workbook layout
# --------------------------------------------------------------------------- #

#: Marker the SMH portal uses to label a biscuit row in INVENTORY RECEIVING DATA.
BISCUIT_TYPES = frozenset({"biscuits", "biscuit"})

#: Column headings used by the two "Received" tabs.
RECEIVED_HEADERS = ("Sr. N0.", "Markaz", "EMIS", "School Name", "Enrollment")
RECEIVED_PAIR = ("Date", "Quantity")

#: Column headings used by the two "Consumption" tabs.
CONSUMPTION_HEADERS = ("Sr. N0.", "Markaz", "EMIS", "School Name", "Enrollment")

#: First data row / first totals row of every block (1-based spreadsheet rows).
FIRST_SCHOOL_ROW = 2
TOTALS_HEADER_ROW = 8
FIRST_TOTALS_ROW = 9
SCHOOLS_PER_BLOCK = 6
LAST_DATA_ROW = FIRST_SCHOOL_ROW + SCHOOLS_PER_BLOCK - 1  # 7
LAST_TOTALS_ROW = FIRST_TOTALS_ROW + SCHOOLS_PER_BLOCK - 1  # 14


@dataclass(frozen=True)
class SheetWrite:
    """One ``values.update`` request: a grid plus where it belongs."""

    tab: str
    start_cell: str
    values: list[list[str]]

    @property
    def a1_range(self) -> str:
        """A1 range covering exactly ``values`` (never larger)."""
        rows = len(self.values)
        columns = max((len(row) for row in self.values), default=0)
        return f"'{self.tab}'!{self.start_cell}:{_cell(rows, columns)}"

    def start_row(self) -> int:
        return _row_number(self.start_cell)

    def end_row(self) -> int:
        return self.start_row() + len(self.values) - 1

    def start_column(self) -> int:
        letters = "".join(char for char in self.start_cell if char.isalpha())
        return _column_number(letters)

    def end_column(self) -> int:
        return self.start_column() + max(
            (len(row) for row in self.values), default=0
        ) - 1

    def trimmed(self) -> "SheetWrite":
        """Drop trailing empty columns so a write never exceeds its range."""
        width = 0
        for row in self.values:
            for index, cell in enumerate(row, start=1):
                if cell != "":
                    width = max(width, index)
        if width == 0:
            return self
        return SheetWrite(
            tab=self.tab,
            start_cell=self.start_cell,
            values=[list(row[:width]) + [""] * (width - len(row)) for row in self.values],
        )


@dataclass(frozen=True)
class TabSpec:
    """How one tab of the workbook is laid out."""

    tab: str
    kind: str  # "received" or "consumption"
    #: Largest number of date columns (or receipts) the tab currently supports.
    capacity: int | None = None
    #: 1-based column the date/quantity block starts at (6 = F).
    first_block_column: int = 6
    #: Set when the tab's totals block lives on a different row than row 8/9.
    totals_header_row: int = TOTALS_HEADER_ROW
    first_totals_row: int = FIRST_TOTALS_ROW
    #: Row holding the per-school total the Summary tab reads (F9, F10, ...).
    total_cell_row: int = FIRST_TOTALS_ROW
    #: Column the per-school total is summarised in plus the one after it.
    total_column: int = 5


SHEET_SPECS: Mapping[str, TabSpec] = {
    "milk_received": TabSpec(tab="Milk Received", kind="received", capacity=32),
    "biscuits_received": TabSpec(tab="Biscuits Received", kind="received", capacity=11),
    "milk_consumption": TabSpec(tab="Milk Consumption", kind="consumption", capacity=25),
    "biscuit_consumption": TabSpec(
        tab="Biscuit Consumption", kind="consumption", capacity=21
    ),
}


@dataclass
class SchoolTables:
    """The three portal tables the master sheet is built from, for one school."""

    emis: str
    enrollment_initial: int
    enrollment_latest: int
    receipts: list[tuple[_dt.date, int, str]] = field(default_factory=list)  # date, qty, type
    milk_consumption: dict[_dt.date, int] = field(default_factory=dict)
    biscuit_consumption: dict[_dt.date, int] = field(default_factory=dict)

    def milk_receipts(self) -> list[tuple[_dt.date, int]]:
        return sorted(
            ((day, qty) for day, qty, kind in self.receipts if _is_milk(kind))
        )

    def biscuit_receipts(self) -> list[tuple[_dt.date, int]]:
        return sorted(
            ((day, qty) for day, qty, kind in self.receipts if not _is_milk(kind))
        )


# --------------------------------------------------------------------------- #
# Reading a bundle
# --------------------------------------------------------------------------- #


def _is_milk(kind: str) -> bool:
    return kind.strip().lower() not in BISCUIT_TYPES


def _read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]


def parse_portal_date(text: str) -> _dt.date:
    """Parse the portal's ``DD-MM-YYYY`` dates."""
    day, month, year = text.strip().split("-")
    return _dt.date(int(year), int(month), int(day))


def _as_int(text: str) -> int:
    cleaned = text.strip().replace(",", "")
    return int(float(cleaned)) if cleaned else 0


def _table_rows(manifest_table: Mapping[str, Any], bundle: Path) -> list[list[str]]:
    csv_file = manifest_table.get("csv_file") or ""
    path = bundle / csv_file
    if not path.is_file():
        raise FileNotFoundError(f"manifest points at a missing CSV: {csv_file}")
    rows = _read_csv(path)
    return rows[1:] if rows else []


def _summary_days(table: Mapping[str, Any], bundle: Path) -> set[_dt.date]:
    """Dates listed by a SUMMARY DATE WISE table."""
    headers = [h.strip() for h in table.get("headers") or []]
    if "Date" not in headers:
        return set()
    date_index = headers.index("Date")
    days: set[_dt.date] = set()
    for row in _table_rows(table, bundle):
        if len(row) > date_index:
            days.add(parse_portal_date(row[date_index]))
    return days


def _consumption_rows(
    table: Mapping[str, Any], bundle: Path
) -> dict[_dt.date, int]:
    headers = [h.strip() for h in table.get("headers") or []]
    if "Date" not in headers or "Consumption" not in headers:
        return {}
    date_index = headers.index("Date")
    value_index = headers.index("Consumption")
    values: dict[_dt.date, int] = {}
    for row in _table_rows(table, bundle):
        if len(row) <= max(date_index, value_index):
            continue
        values[parse_portal_date(row[date_index])] = _as_int(row[value_index])
    return values


def _split_consumption(
    tables: Sequence[Mapping[str, Any]],
    bundle: Path,
    milk_days: set[_dt.date],
    biscuit_days: set[_dt.date],
) -> tuple[dict[_dt.date, int], dict[_dt.date, int]]:
    """Split the two CONSUMPTION tables into (milk, biscuit).

    Both consumption tables render with identical headers, and a table's
    ``capture_contexts`` accumulate every view visited on the page (so they list
    both "tab: Milk" and "tab: Biscuit"). The dependable signal is the data:
    each consumption table lists exactly the service days of one product, and
    the SUMMARY DATE WISE tables say which days belong to which product.
    """
    milk: dict[_dt.date, int] = {}
    biscuit: dict[_dt.date, int] = {}
    for table in tables:
        # SUMMARY DATE WISE also has Date/Consumption headers; only the two
        # CONSUMPTION tables carry per-class consumption.
        if str(table.get("title") or "").strip().upper() != "CONSUMPTION":
            continue
        values = _consumption_rows(table, bundle)
        if not values:
            continue
        milk_hits = len(values.keys() & milk_days)
        biscuit_hits = len(values.keys() & biscuit_days)
        if milk_hits == biscuit_hits == 0:
            raise ValueError(
                f"{table.get('csv_file')}: consumption dates match neither the milk "
                "nor the biscuit summary table"
            )
        target = milk if milk_hits > biscuit_hits else biscuit
        overlap = dict.fromkeys(target)
        for day, quantity in values.items():
            if day in target and target[day] != quantity:  # pragma: no cover
                raise ValueError(f"{table.get('csv_file')}: conflicting value for {day}")
        target.update(values)
    return milk, biscuit


def load_bundle(bundle: Path) -> tuple[list[SchoolTables], dict[str, Any]]:
    """Read every school in an export bundle, in the workbook's school order."""
    bundle = Path(bundle)
    manifest = json.loads((bundle / "run_manifest.json").read_text(encoding="utf-8"))

    schools: list[SchoolTables] = []
    for result in manifest.get("results", []):
        if result.get("status") != "ok":
            raise ValueError(
                f"school {result.get('emis_code')} finished with status "
                f"{result.get('status')!r}: refusing to sync a partial bundle"
            )
        incomplete = [
            t.get("title")
            for t in result.get("tables", [])
            if t.get("complete") is False
        ]
        if incomplete:
            raise ValueError(
                f"school {result.get('emis_code')} has incomplete tables: "
                f"{', '.join(str(name) for name in incomplete)}"
            )

        tables = result.get("tables", [])
        school = SchoolTables(emis=str(result["emis_code"]), enrollment_initial=0, enrollment_latest=0)

        milk_days: set[_dt.date] = set()
        biscuit_days: set[_dt.date] = set()

        for table in tables:
            title = str(table.get("title") or "").upper()
            if title == "SUMMARY DATE WISE (MILK)":
                milk_days = _summary_days(table, bundle)
            elif title == "SUMMARY DATE WISE (BISCUIT)":
                biscuit_days = _summary_days(table, bundle)

        for table in tables:
            title = str(table.get("title") or "").upper()
            rows = _table_rows(table, bundle)
            if title == "ENROLLMENT":
                if not rows:
                    continue
                headers = [h.strip() for h in table.get("headers") or []]
                values = dict(zip(headers, rows[0]))
                # "Initial Enrollment (2026-09-25)" is the figure the workbook uses.
                initial = next(
                    (v for k, v in values.items() if k.lower().startswith("initial enrollment")),
                    "",
                )
                latest = values.get("Enrollment (Latest)", "")
                school.enrollment_initial = _as_int(initial)
                school.enrollment_latest = _as_int(latest) or school.enrollment_initial
            elif title == "INVENTORY RECEIVING DATA":
                headers = [h.strip() for h in table.get("headers") or []]
                try:
                    date_i = headers.index("Date")
                    qty_i = headers.index("Inventory Received")
                    type_i = headers.index("Inventory Type")
                except ValueError:  # pragma: no cover - guarded by the extractor
                    continue
                for row in rows:
                    if len(row) <= max(date_i, qty_i, type_i):
                        continue
                    school.receipts.append(
                        (parse_portal_date(row[date_i]), _as_int(row[qty_i]), row[type_i])
                    )

        school.milk_consumption, school.biscuit_consumption = _split_consumption(
            tables, bundle, milk_days, biscuit_days
        )
        schools.append(school)

    return schools, manifest


# --------------------------------------------------------------------------- #
# Building the writes
# --------------------------------------------------------------------------- #


def _ordered_schools(schools: Sequence[SchoolTables]) -> list[SchoolTables]:
    """Order schools by EMIS so row 2 keeps Sr. No. 145."""
    return sorted(schools, key=lambda school: school.emis)


def _received_write(
    spec: TabSpec,
    schools: Sequence[SchoolTables],
    pick,
    total_enrollment: bool,
) -> SheetWrite:
    receipts = {school.emis: pick(school) for school in schools}
    slots = max((len(value) for value in receipts.values()), default=0)
    if slots == 0:
        raise ValueError(f"{spec.tab}: the bundle has no receipts to write")
    if spec.capacity is not None and slots > spec.capacity:
        raise ValueError(
            f"{spec.tab}: {slots} receipts but the tab only has room for {spec.capacity}; "
            "widen the sheet (or add receipt slots) before syncing"
        )

    header = list(RECEIVED_HEADERS) + list(RECEIVED_PAIR) * slots
    values: list[list[str]] = [header]

    quantity_letters = [
        _column_name(spec.first_block_column + 1 + 2 * i) for i in range(slots)
    ]

    for offset, school in enumerate(schools):
        row: list[str] = [
            str(145 + offset),
            "MARHAN - MALE",
            school.emis,
            school_name(school.emis),
            str(school.enrollment_initial if total_enrollment else school.enrollment_latest),
        ]
        row += [""] * (2 * slots)
        for index, (day, quantity) in enumerate(receipts[school.emis]):
            column = spec.first_block_column - 1 + 2 * index
            row[column] = day.strftime("%d-%m-%Y")
            row[column + 1] = str(quantity)
        values.append(row)

    totals_header = list(RECEIVED_HEADERS) + ["Total Stock"]
    values.append(totals_header)
    for offset, school in enumerate(schools):
        # The total must read the school's own data row (2..7), never the
        # totals rows it is written into -- that resolves to zero.
        data_row = FIRST_SCHOOL_ROW + offset
        formula = "=SUM(" + ",".join(f"{letter}{data_row}" for letter in quantity_letters) + ")"
        values.append(
            [
                str(145 + offset),
                "MARHAN - MALE",
                school.emis,
                school_name(school.emis),
                str(school.enrollment_initial if total_enrollment else school.enrollment_latest),
                formula,
            ]
        )
    return SheetWrite(tab=spec.tab, start_cell="A1", values=values)


def _consumption_write(
    spec: TabSpec,
    schools: Sequence[SchoolTables],
    pick,
) -> SheetWrite:
    per_school = {school.emis: pick(school) for school in schools}
    days = sorted({day for value in per_school.values() for day in value})
    if not days:
        raise ValueError(f"{spec.tab}: the bundle has no consumption rows to write")
    if spec.capacity is not None and len(days) > spec.capacity:
        raise ValueError(
            f"{spec.tab}: {len(days)} service days but the tab only has room for "
            f"{spec.capacity} columns; widen the sheet before syncing"
        )

    header = list(CONSUMPTION_HEADERS) + [day.isoformat() for day in days]
    values: list[list[str]] = [header]
    for offset, school in enumerate(schools):
        values.append(
            [
                str(145 + offset),
                "MARHAN - MALE",
                school.emis,
                school_name(school.emis),
                str(school.enrollment_initial),
            ]
            + [str(per_school[school.emis][day]) if day in per_school[school.emis] else "" for day in days]
        )

    values.append(list(CONSUMPTION_HEADERS) + ["Total Cons."] + [""] * (len(days) - 1))
    first_date_column = _column_name(spec.first_block_column)
    last_date_column = _column_name(spec.first_block_column + len(days) - 1)
    for offset, school in enumerate(schools):
        data_row = FIRST_SCHOOL_ROW + offset
        values.append(
            [
                str(145 + offset),
                "MARHAN - MALE",
                school.emis,
                school_name(school.emis),
                str(school.enrollment_initial),
                f"=sum({first_date_column}{data_row}:{last_date_column}{data_row})",
            ]
            + [""] * (len(days) - 1)
        )
    return SheetWrite(tab=spec.tab, start_cell="A1", values=values)


def build_writes(schools: Sequence[SchoolTables]) -> list[SheetWrite]:
    """Return the writes that populate the five managed tabs."""
    ordered = _ordered_schools(schools)
    if len(ordered) != SCHOOLS_PER_BLOCK:
        raise ValueError(
            f"the workbook has {SCHOOLS_PER_BLOCK} school rows but the bundle has "
            f"{len(ordered)} schools"
        )
    writes = [
        _received_write(SHEET_SPECS["milk_received"], ordered, SchoolTables.milk_receipts, True),
        _received_write(
            SHEET_SPECS["biscuits_received"], ordered, SchoolTables.biscuit_receipts, True
        ),
        _consumption_write(
            SHEET_SPECS["milk_consumption"], ordered, lambda s: s.milk_consumption
        ),
        _consumption_write(
            SHEET_SPECS["biscuit_consumption"], ordered, lambda s: s.biscuit_consumption
        ),
    ]
    return [write.trimmed() for write in writes]


#: The six EMIS codes the master workbook covers, with their sheet labels.
SCHOOL_NAMES: Mapping[str, str] = {
    "32230155": "GPS CHAK NO 437 TDA",
    "32230183": "GPS CHAK NO. 419 TDA",
    "32230566": "GPS CHAK NO. 435 TDA",
    "32230576": "GPS CHAK NO. 389 TDA",
    "32230763": "GPS CHAK NO. 398 TDA",
    "32230764": "GPS CHAK NO 420/TDA",
}


def school_name(emis: str) -> str:
    """Sheet label for an EMIS code (falls back to the code itself)."""
    return SCHOOL_NAMES.get(emis, emis)


# --------------------------------------------------------------------------- #
# A1 helpers
# --------------------------------------------------------------------------- #


def _column_name(index: int) -> str:
    """1-based column index to letters (1 -> A, 27 -> AA)."""
    if index < 1:
        raise ValueError(index)
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _column_number(name: str) -> int:
    total = 0
    for char in name:
        if not char.isalpha():
            raise ValueError(name)
        total = total * 26 + (ord(char.upper()) - ord("A") + 1)
    return total


def _row_number(cell: str) -> int:
    digits = "".join(char for char in cell if char.isdigit())
    return int(digits)


def _cell(rows: int, columns: int) -> str:
    return f"{_column_name(columns)}{rows}"
