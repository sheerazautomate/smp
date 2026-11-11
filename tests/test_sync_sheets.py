"""Tests for the CSV -> Google Sheets sync CLI (no network, no credentials)."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import sync_sheets  # noqa: E402
from tests.test_sheet_plan import make_bundle  # noqa: E402


class FakeValues:
    def __init__(self, calls):
        self._calls = calls

    def update(self, spreadsheetId, range, valueInputOption, body):
        self._calls.append(("update", spreadsheetId, range, body))
        return FakeRequest({"updatedCells": sum(len(row) for row in body["values"])})

    def clear(self, spreadsheetId, range, body):
        self._calls.append(("clear", spreadsheetId, range))
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, calls, sizes):
        self._values = FakeValues(calls)
        self._calls = calls
        self._sizes = sizes

    def values(self):
        return self._values

    def get(self, spreadsheetId, fields):
        sheets = [
            {
                "properties": {
                    "title": title,
                    "gridProperties": {"columnCount": count},
                }
            }
            for title, count in self._sizes.items()
        ]
        return FakeRequest({"sheets": sheets})


class FakeService:
    def __init__(self, sizes=None):
        self.calls = []
        self.spreadsheets_obj = FakeSpreadsheets(self.calls, sizes or {})

    def spreadsheets(self):
        return self.spreadsheets_obj


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class SyncWriteTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.bundle = make_bundle(self.root)

    def tearDown(self):
        self._temp.cleanup()

    def _writes(self):
        schools, _ = sync_sheets.load_bundle(self.bundle)
        return sync_sheets.build_writes(schools)

    def test_writes_each_tab_once_and_never_the_raw_tabs(self):
        service = FakeService()
        result = sync_sheets.write_rows(
            service, "sheet-id", self._writes()
        )
        ranges = [call[2] for call in service.calls]
        self.assertEqual(len(ranges), 4)
        self.assertTrue(all(call[0] == "update" for call in service.calls))
        self.assertFalse(any("RAW" in r or "Raw" in r for r in ranges))
        self.assertGreater(result.updated_cells, 0)

    def test_requests_use_the_exact_grid_of_their_values(self):
        service = FakeService()
        writes = self._writes()
        sync_sheets.write_rows(service, "sheet-id", writes)
        for call, write in zip(service.calls, writes):
            _, _, a1, body = call
            self.assertEqual(a1, write.a1_range)
            self.assertEqual(body["values"], write.values)
            rows = len(body["values"])
            columns = max(len(row) for row in body["values"])
            self.assertTrue(a1.endswith(f"{sync_sheets._column_name(columns)}{rows}"))

    def test_stale_tail_is_cleared_but_never_the_raw_tabs(self):
        writes = self._writes()
        sizes = {write.tab: write.end_column() + 5 for write in writes}
        sizes["RAW Milk"] = 26
        service = FakeService(sizes)
        cleared = sync_sheets.clear_stale_tail(service, "sheet-id", writes, sizes)

        self.assertEqual(len(cleared), 4)
        cleared_ranges = [entry["range"] for entry in cleared]
        self.assertFalse(any("RAW" in r or "Raw" in r for r in cleared_ranges))
        for write, a1 in zip(writes, cleared_ranges):
            start = a1.split("!")[1].split(":")[0]
            self.assertEqual(
                sync_sheets._column_name(write.end_column() + 1),
                start[: len(start) - 1],
            )

    def test_nothing_is_cleared_when_the_sheets_are_exactly_the_right_size(self):
        writes = self._writes()
        sizes = {write.tab: write.end_column() for write in writes}
        service = FakeService(sizes)
        cleared = sync_sheets.clear_stale_tail(service, "sheet-id", writes, sizes)
        self.assertEqual(cleared, [])


class SyncCliTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sync_sheets.main(list(args))
        return code, buffer.getvalue()

    def test_dry_run_prints_the_plan_without_a_spreadsheet_id(self):
        make_bundle(self.root)
        code, output = self.run_cli("--data-root", str(self.root), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Milk Received", output)
        self.assertIn("Dry run", output)

    def test_dry_run_json_lists_exact_ranges(self):
        make_bundle(self.root)
        code, output = self.run_cli("--data-root", str(self.root), "--dry-run", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        tabs = [entry["tab"] for entry in payload["ranges"]]
        self.assertEqual(
            tabs,
            ["Milk Received", "Biscuits Received", "Milk Consumption", "Biscuit Consumption"],
        )

    def test_newest_bundle_is_used_by_default(self):
        make_bundle(self.root)
        code, output = self.run_cli("--data-root", str(self.root), "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertIn("20260101_000000", json.loads(output)["bundle"])

    def test_missing_spreadsheet_id_is_an_error(self):
        make_bundle(self.root)
        code, _ = self.run_cli("--data-root", str(self.root))
        self.assertEqual(code, 1)

    def test_incomplete_bundle_exits_with_two(self):
        make_bundle(self.root, status="incomplete")
        code, _ = self.run_cli("--data-root", str(self.root), "--dry-run")
        self.assertEqual(code, 2)

    def test_empty_data_root_is_an_error(self):
        code, _ = self.run_cli("--data-root", str(self.root), "--dry-run")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
