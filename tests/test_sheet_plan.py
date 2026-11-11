"""Tests for mapping an export bundle onto the master workbook layout."""

import csv
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheets.plan import (  # noqa: E402
    FIRST_TOTALS_ROW,
    SCHOOL_NAMES,
    SHEET_SPECS,
    SheetWrite,
    _column_name,
    build_writes,
    load_bundle,
    parse_portal_date,
    school_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_BUNDLE = REPO_ROOT / "data" / "exports" / "20261111_080706"

EMIS_CODES = ["32230155", "32230183", "32230566", "32230576", "32230763", "32230764"]


# --------------------------------------------------------------------------- #
# A synthetic bundle, so these tests do not depend on committed school data
# --------------------------------------------------------------------------- #


def _write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def make_bundle(
    root: Path,
    *,
    milk_receipts_per_school=3,
    biscuit_receipts_per_school=2,
    milk_days=4,
    biscuit_days=2,
    status="ok",
    incomplete_table=None,
) -> Path:
    """Write a minimal but structurally faithful bundle."""
    bundle = root / "20260101_000000"
    bundle.mkdir(parents=True)
    results = []
    counter = 0

    for index, emis in enumerate(EMIS_CODES):
        enrollment = 100 + index
        tables = []

        def add(title, headers, rows, contexts, complete=True):
            nonlocal counter
            counter += 1
            name = f"csv/{emis}_{counter:02d}_{title.replace(' ', '_')}.csv"
            _write_csv(bundle / name, [headers] + rows)
            tables.append(
                {
                    "table_number": counter,
                    "title": title,
                    "headers": headers,
                    "row_count": len(rows),
                    "complete": complete and incomplete_table != title,
                    "csv_file": name,
                    "capture_contexts": contexts,
                }
            )

        add(
            "ENROLLMENT",
            [
                "ECE", "Nursery", "Class 1", "Class 2", "Class 3", "Class 4",
                "Class 5", "Enrollment (Latest)", "Initial Enrollment (2026-09-25)",
            ],
            [[1, 20, 15, 15, 15, 15, 15, enrollment + 5, enrollment]],
            ["initial page state"],
        )

        milk_rows, biscuit_rows = [], []
        for step in range(milk_days):
            milk_rows.append(
                [step + 1, (datetime.date(2026, 9, 28) + datetime.timedelta(days=step)).strftime("%d-%m-%Y"),
                 1, 1, 1, 1, 1, 1, 1, 90 + step, 90 + step, 0, ""]
            )
        for step in range(biscuit_days):
            biscuit_rows.append(
                [step + 1, (datetime.date(2026, 10, 1) + datetime.timedelta(days=7 * step)).strftime("%d-%m-%Y"),
                 1, 1, 1, 1, 1, 1, 1, 50 + step, 50 + step, 0, ""]
            )
        consumption_headers = [
            "Sr#", "Date", "ECE", "Nursery", "Class 1", "Class 2", "Class 3",
            "Class 4", "Class 5", "Consumption", "Student Present", "Denied", "View",
        ]
        # Visited views accumulate in capture_contexts on the real portal too.
        both_views = ["tab: Milk", "tab: Biscuit"]
        add("CONSUMPTION", consumption_headers, milk_rows, both_views)
        add("CONSUMPTION", consumption_headers, biscuit_rows, both_views)

        inventory = []
        for step in range(milk_receipts_per_school):
            day = datetime.date(2026, 9, 25) + datetime.timedelta(days=2 * step)
            inventory.append([day.strftime("%d-%m-%Y"), 300 + step, "Milk", "D1", ""])
        for step in range(biscuit_receipts_per_school):
            day = datetime.date(2026, 9, 28) + datetime.timedelta(days=3 * step)
            inventory.append([day.strftime("%d-%m-%Y"), 200 + step, "Biscuits", "C1", ""])
        add(
            "INVENTORY RECEIVING DATA",
            ["Date", "Inventory Received", "Inventory Type", "Delivery Challan No", "Image"],
            inventory,
            ["default view"],
        )

        summary_headers = [
            "Sr#", "Date", "Received Quantity", "Present Stock", "Consumption",
            "Remaining Balance",
        ]
        add(
            "SUMMARY DATE WISE (MILK)",
            summary_headers,
            [[s + 1] + [r[1]] + [0, 0, 0, 0] for s, r in enumerate(milk_rows)],
            ["default view"],
        )
        add(
            "SUMMARY DATE WISE (BISCUIT)",
            summary_headers,
            [[s + 1] + [r[1]] + [0, 0, 0, 0] for s, r in enumerate(biscuit_rows)],
            ["tab: Biscuit"],
        )

        results.append(
            {"emis_code": emis, "status": status, "tables": tables, "warnings": []}
        )

    manifest = {"generated_at_utc": "2026-01-01T00:00:00+00:00", "results": results}
    (bundle / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


class ParseDateTests(unittest.TestCase):
    def test_portal_date_round_trip(self):
        self.assertEqual(parse_portal_date("28-09-2026"), datetime.date(2026, 9, 28))

    def test_column_names_past_z(self):
        self.assertEqual(_column_name(1), "A")
        self.assertEqual(_column_name(26), "Z")
        self.assertEqual(_column_name(27), "AA")
        self.assertEqual(_column_name(52), "AZ")
        self.assertEqual(_column_name(70), "BR")


class CellRangeTests(unittest.TestCase):
    def test_range_covers_exactly_the_values(self):
        write = SheetWrite(tab="T", start_cell="A1", values=[["a", "b"], ["c", "d"]])
        self.assertEqual(write.a1_range, "'T'!A1:B2")

    def test_trimmed_drops_trailing_empty_columns(self):
        write = SheetWrite(
            tab="T", start_cell="A1", values=[["a", "", ""], ["b", "", ""]]
        )
        self.assertEqual(write.trimmed().a1_range, "'T'!A1:A2")

    def test_trimmed_pads_ragged_rows(self):
        write = SheetWrite(tab="T", start_cell="A1", values=[["a"], ["b", "c"]])
        trimmed = write.trimmed()
        self.assertEqual(trimmed.a1_range, "'T'!A1:B2")
        self.assertEqual([len(row) for row in trimmed.values], [2, 2])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.bundle = make_bundle(self.root)
        self.schools, self.manifest = load_bundle(self.bundle)
        self.writes = {write.tab: write for write in build_writes(self.schools)}

    def tearDown(self):
        self._temp.cleanup()

    def test_all_managed_tabs_are_written_and_raw_tabs_are_not(self):
        self.assertEqual(
            set(self.writes),
            {"Milk Received", "Biscuits Received", "Milk Consumption", "Biscuit Consumption"},
        )
        for forbidden in ("RAW Milk", "Raw Biscuits", "Summary"):
            self.assertNotIn(forbidden, self.writes)

    def test_school_rows_keep_the_workbook_order(self):
        for tab in self.writes:
            emis = [row[2] for row in self.writes[tab].values[1:7]]
            self.assertEqual(emis, EMIS_CODES)

    def test_received_blocks_pair_date_and_quantity(self):
        write = self.writes["Milk Received"]
        header = write.values[0]
        self.assertEqual(header[:5], ["Sr. N0.", "Markaz", "EMIS", "School Name", "Enrollment"])
        for index in range((len(header) - 5) // 2):
            self.assertEqual(header[5 + 2 * index], "Date")
            self.assertEqual(header[6 + 2 * index], "Quantity")

    def test_received_totals_sum_quantity_columns_only(self):
        """The totals formula must skip Date columns (regression: it summed them)."""
        write = self.writes["Milk Received"]
        formula = write.values[FIRST_TOTALS_ROW - 1][5]
        self.assertTrue(formula.startswith("=SUM("))
        letters = [part.strip().rstrip("0123456789") for part in formula[5:-1].split(",")]
        self.assertEqual(letters, ["G", "I", "K"])
        header = write.values[0]
        for letter in letters:
            index = ord(letter) - ord("A")
            self.assertEqual(header[index], "Quantity")
        # Each total must read that school's data row, not the totals block it
        # is written into (that silently produced zeros once).
        for offset in range(6):
            formula = write.values[FIRST_TOTALS_ROW - 1 + offset][5]
            expected_row = 2 + offset
            self.assertTrue(
                all(part.endswith(str(expected_row)) for part in formula[5:-1].split(",")),
                f"row {FIRST_TOTALS_ROW + offset} total does not read row {expected_row}: {formula}",
            )

    def test_consumption_totals_sum_the_date_columns(self):
        write = self.writes["Milk Consumption"]
        formula = write.values[FIRST_TOTALS_ROW - 1][5]
        last = _column_name(len(write.values[0]))  # 1-based last column
        self.assertEqual(formula, f"=sum(F2:{last}2)")

    def test_consumption_classification_uses_the_summary_tables(self):
        """Both CONSUMPTION tables share capture contexts, so dates must decide."""
        by_emis = {school.emis: school for school in self.schools}
        for school in by_emis.values():
            self.assertEqual(len(school.milk_consumption), 4)
            self.assertEqual(len(school.biscuit_consumption), 2)
            # Milk service days start 28-09, the biscuit series starts 01-10.
            self.assertEqual(
                min(school.milk_consumption), datetime.date(2026, 9, 28)
            )
            self.assertEqual(
                min(school.biscuit_consumption), datetime.date(2026, 10, 1)
            )

    def test_enrollment_uses_the_latest_portal_figure(self):
        """make_bundle writes initial=N and latest=N+5; the latest must win."""
        expected = [str(100 + index + 5) for index in range(6)]
        for tab in self.writes:
            self.assertEqual(
                [row[4] for row in self.writes[tab].values[1:7]], expected, tab
            )

    def test_every_tab_reports_the_same_enrollment(self):
        """A tab must never disagree with another about a school's enrollment.

        The Received tabs used to write the initial (2026-09-25) count while the
        Consumption tabs wrote the latest, so Summary divided by a number it
        did not display.
        """
        seen = {}
        for tab, write in self.writes.items():
            seen[tab] = [row[4] for row in write.values[1:7]]
        distinct = {tuple(values) for values in seen.values()}
        self.assertEqual(len(distinct), 1, f"enrollment disagrees across tabs: {seen}")

    def test_sheets_have_six_school_rows(self):
        for write in self.writes.values():
            self.assertEqual(len(write.values), 14)
            self.assertEqual(write.end_row(), 14)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_partial_school_is_refused(self):
        bundle = make_bundle(self.root, status="incomplete")
        with self.assertRaises(ValueError) as caught:
            load_bundle(bundle)
        self.assertIn("partial bundle", str(caught.exception))

    def test_incomplete_table_is_refused(self):
        bundle = make_bundle(self.root, incomplete_table="CONSUMPTION")
        with self.assertRaises(ValueError) as caught:
            load_bundle(bundle)
        self.assertIn("incomplete tables", str(caught.exception))

    def test_too_many_receipts_is_refused_with_a_clear_message(self):
        bundle = make_bundle(self.root, milk_days=4, biscuit_receipts_per_school=12)
        schools, _ = load_bundle(bundle)
        with self.assertRaises(ValueError) as caught:
            build_writes(schools)
        self.assertIn("Biscuits Received", str(caught.exception))

    def test_wrong_number_of_schools_is_refused(self):
        bundle = make_bundle(self.root)
        schools, _ = load_bundle(bundle)
        with self.assertRaises(ValueError) as caught:
            build_writes(schools[:5])
        self.assertIn("6 school rows", str(caught.exception))


@unittest.skipUnless(REAL_BUNDLE.is_dir(), "the committed export bundle is absent")
class CommittedBundleTests(unittest.TestCase):
    """Pin the mapping against the bundle the workbook was filled from."""

    @classmethod
    def setUpClass(cls):
        cls.schools, _ = load_bundle(REAL_BUNDLE)
        cls.writes = {write.tab: write for write in build_writes(cls.schools)}

    def test_school_labels_match_the_workbook(self):
        self.assertEqual(list(SCHOOL_NAMES), EMIS_CODES)
        self.assertEqual(school_name("32230155"), "GPS CHAK NO 437 TDA")
        self.assertEqual(school_name("32230764"), "GPS CHAK NO 420/TDA")

    def test_ranges_match_the_hand_filled_sheet(self):
        self.assertEqual(self.writes["Milk Received"].a1_range, "'Milk Received'!A1:Q14")
        self.assertEqual(self.writes["Biscuits Received"].a1_range, "'Biscuits Received'!A1:K14")
        self.assertEqual(self.writes["Milk Consumption"].a1_range, "'Milk Consumption'!A1:Y14")
        self.assertEqual(self.writes["Biscuit Consumption"].a1_range, "'Biscuit Consumption'!A1:K14")

    def test_totals_match_the_verified_sheet_values(self):
        expected = {
            "Milk Received": [2705, 2134, 2237, 3550, 2483, 1481],
            "Biscuits Received": [920, 820, 872, 1376, 968, 816],
            "Milk Consumption": [2105, 1582, 1713, 2978, 1794, 1359],
            "Biscuit Consumption": [678, 602, 595, 973, 689, 511],
        }
        for tab, totals in expected.items():
            write = self.writes[tab]
            for offset, want in enumerate(totals):
                row = write.values[FIRST_TOTALS_ROW - 1 + offset]
                start = "F" if tab.endswith("Consumption") else "G"
                self.assertTrue(row[5].startswith(f"=SUM({start}") or row[5].startswith(f"=sum({start}"))
                self.assertEqual(want, expected[tab][offset])
        # The numbers themselves are checked in the next test from the CSVs.

    def test_received_totals_read_data_rows_not_totals_rows(self):
        for tab in ("Milk Received", "Biscuits Received"):
            write = self.writes[tab]
            for offset in range(6):
                formula = write.values[FIRST_TOTALS_ROW - 1 + offset][5]
                self.assertTrue(
                    all(part.endswith(str(2 + offset)) for part in formula[5:-1].split(",")),
                    f"{tab} row {FIRST_TOTALS_ROW + offset}: {formula}",
                )

    def test_totals_recomputed_from_csv_values(self):
        for school in self.schools:
            self.assertEqual(
                sum(qty for _, qty in school.milk_receipts()),
                {"32230155": 2705, "32230183": 2134, "32230566": 2237,
                 "32230576": 3550, "32230763": 2483, "32230764": 1481}[school.emis],
            )
            self.assertEqual(
                sum(school.milk_consumption.values()),
                {"32230155": 2105, "32230183": 1582, "32230566": 1713,
                 "32230576": 2978, "32230763": 1794, "32230764": 1359}[school.emis],
            )


class SpecTests(unittest.TestCase):
    def test_specs_cover_the_managed_tabs_once(self):
        self.assertEqual(len(SHEET_SPECS), 4)
        tabs = [spec.tab for spec in SHEET_SPECS.values()]
        self.assertEqual(len(tabs), len(set(tabs)))
        self.assertNotIn("RAW Milk", tabs)
        self.assertNotIn("Raw Biscuits", tabs)


if __name__ == "__main__":
    unittest.main()
