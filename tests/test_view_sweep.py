"""End-to-end check of ``_extract_school`` against a tabbed page.

Reproduces the portal: a Milk/Biscuit button pair (plain ``<button>`` elements,
no ``data-bs-toggle``), each panel holding its own DataTable, the second panel
hidden until its tab is clicked. The old code never saw these buttons, captured
both tables before any click, and left Biscuit at its 5-row first page.
"""

import unittest

from smp_extractor.collector import _extract_school
from smp_extractor.models import SchoolResult

CODE = "32230155"
URL = f"https://smp26.pesrp.edu.pk/school-detail/{CODE}"


class TabbedPage:
    """Minimal page object: two tabs, two panels, two DataTables."""

    def __init__(self, milk_total=18, biscuit_total=6, page_size=5):
        self.url = URL
        self.page_size = page_size
        self.totals = {"milk": milk_total, "biscuit": biscuit_total}
        self.show_all = {"milk": False, "biscuit": False}
        self.page = {"milk": 0, "biscuit": 0}
        self.panel = "milk"          # which tab is open
        self.clicks: list[str] = []

    # ------------------------------------------------------------------
    def _ids(self):
        return {"milk": "DataTables_Table_0", "biscuit": "DataTables_Table_1"}

    def _rows(self, which):
        total = self.totals[which]
        size = total if self.show_all[which] else self.page_size
        start_index = 0 if self.show_all[which] else self.page[which] * size
        rows = [
            [str(index + 1), f"{index:02d}-11-2026"]
            for index in range(start_index, min(start_index + size, total))
        ]
        return rows

    def _page_count(self, which):
        total = self.totals[which]
        size = total if self.show_all[which] else self.page_size
        return max(1, -(-total // size))

    # ---- Playwright surface -------------------------------------------
    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

        class Response:
            status = 200

        return Response()

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def title(self):
        return "Dashboard | School Meal Program"

    def evaluate(self, script, arg=None):
        if "in_thead" in script:
            return self._snapshot()
        if script.strip() == __import__(
            "smp_extractor.collector", fromlist=["PAGE_DIGEST_JS"]
        ).PAGE_DIGEST_JS.strip():
            return "|".join(row[0] for row in self._rows(self.panel))
        if "tableIndex" in script and "infoText" in script:
            return self._pager_records()
        if "collapseControl" in script:
            return []  # nothing collapsed in this fixture
        if "looksLikeTab" in script:
            return [
                {"index": 0, "text": "Milk", "id": "tab-milk",
                 "signature": "tab-milk|Milk", "active": self.panel == "milk"},
                {"index": 1, "text": "Biscuit", "id": "tab-biscuit",
                 "signature": "tab-biscuit|Biscuit", "active": self.panel == "biscuit"},
            ]
        if "role=\\\"tab\\\"" in script or "role=\"tab\"" in script:
            return []
        if "signature =>" in script:
            return {"signature": arg}
        if "visited" in script:
            which = "milk" if arg[0] == self._ids()["milk"] else "biscuit"
            current = 0 if self.show_all[which] else self.page[which]
            return [
                {"pageIndex": index, "next": False,
                 "signature": f"{self._ids()[which]}|{index + 1}"}
                for index in range(self._page_count(which))
                if index != current and index not in set(arg[1])
            ]
        raise AssertionError(f"unexpected script: {script[:70]}")

    def _visible_which(self):
        return self.panel

    def _snapshot(self):
        which = self._visible_which()
        rows = self._rows(which)
        return [
            {
                "index": 0 if which == "milk" else 1,
                "id": self._ids()[which],
                "classes": "dataTable consumption-summary-table",
                "title": "CONSUMPTION",
                "visible": True,
                "rows": [
                    {"in_thead": False,
                     "cells": [{"text": cell, "tag": "TD"} for cell in row]}
                    for row in rows
                ],
            }
        ]

    def _pager_records(self):
        which = self._visible_which()
        rows = self._rows(which)
        total = self.totals[which]
        text = (
            f"Showing {rows[0][0]} to {rows[-1][0]} of {total} entries"
            if rows
            else "No matching records found"
        )
        return [
            {
                "tableIndex": 0 if which == "milk" else 1,
                "tableId": self._ids()[which],
                "infoText": text,
                "hasPager": True,
                "hasLength": True,
                "lengthValue": "-1" if self.show_all[which] else str(self.page_size),
                "lengthHasAll": True,
                "numericIdxs": list(range(self._page_count(which))),
                "activeIdx": 0 if self.show_all[which] else self.page[which],
            }
        ]

    def evaluate_handle(self, script, arg=None):
        page = self

        class Element:
            def __init__(self, signature):
                self.signature = signature

            def click(self, timeout=None):
                signature = str(self.signature)
                page.clicks.append(signature)
                if signature.startswith("tab-"):
                    page.panel = "milk" if "milk" in signature else "biscuit"
                elif "|" in signature and "DataTables" in signature:
                    table_id, label = signature.split("|", 1)
                    which = "milk" if table_id == page._ids()["milk"] else "biscuit"
                    if label.isdigit():
                        page.page[which] = int(label) - 1

        class Handle:
            def as_element(inner):
                return Element(arg)

        return Handle()

    def locator(self, selector):
        page = self
        which = "milk" if "_Table_0_" in selector else "biscuit"

        class Locator:
            @property
            def first(self):
                return self

            def count(self):
                # The password check uses `input[type="password"]:visible`.
                return 0 if "password" in selector else 1

            def is_visible(self, timeout=None):
                return True

            def filter(self, *args, **kwargs):
                return self

            def select_option(self, value, timeout=None):
                if value == "-1":
                    page.show_all[which] = True
                    page.page[which] = 0

        return Locator()

    def wait_for_timeout(self, milliseconds):
        return None


class ViewSweepTests(unittest.TestCase):
    def _run(self, page, max_pages=100):
        result = SchoolResult(emis_code=CODE, url=URL)
        _extract_school(page, result, max_pages, 5000)
        return result

    def test_biscuit_tab_is_discovered_and_fully_captured(self):
        page = TabbedPage(milk_total=18, biscuit_total=6)
        result = self._run(page)

        titles = [table.title for table in result.tables]
        self.assertEqual(len(result.tables), 2, titles)

        by_ref = {table.table_ref: table for table in result.tables}
        milk = by_ref["DataTables_Table_0"]
        biscuit = by_ref["DataTables_Table_1"]

        self.assertEqual(milk.row_count, 18)
        self.assertEqual(milk.rows_expected, 18)
        self.assertTrue(milk.is_complete)

        # The bug: this table used to stop at 5 of 6 rows.
        self.assertEqual(biscuit.row_count, 6)
        self.assertEqual(biscuit.rows_expected, 6)
        self.assertTrue(biscuit.is_complete)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.warnings, [])

    def test_the_biscuit_tab_is_actually_clicked(self):
        page = TabbedPage()
        self._run(page)
        self.assertIn("tab-biscuit|Biscuit", page.clicks)

    def test_row_counts_are_never_duplicated_across_views(self):
        page = TabbedPage(milk_total=18, biscuit_total=6)
        result = self._run(page)
        for table in result.tables:
            sr = [row[0] for row in table.rows]
            self.assertEqual(len(sr), len(set(sr)), f"duplicate rows in {table.table_ref}")

    def test_a_tab_that_does_nothing_is_reported_not_swallowed(self):
        page = TabbedPage()

        class Stuck(TabbedPage):
            def evaluate_handle(self, script, arg=None):
                handle = super().evaluate_handle(script, arg)

                class Element:
                    def click(self, timeout=None):
                        page.clicks.append(str(arg))  # no panel change

                class Handle:
                    def as_element(inner):
                        return Element()

                return Handle()

        result = self._run(Stuck())
        # A click that renders nothing must be reported: otherwise the next
        # capture would attribute the Milk panel's rows to the Biscuit table.
        self.assertTrue(
            any("Could not activate or verify tab" in w for w in result.warnings),
            result.warnings,
        )
        # And nothing may be reported complete on the strength of a stale panel.
        for table in result.tables:
            self.assertTrue(
                table.rows_expected is None or table.row_count <= table.rows_expected
            )

    def test_single_page_table_needs_no_clicking(self):
        page = TabbedPage(milk_total=3, biscuit_total=2)
        result = self._run(page)
        self.assertEqual(result.status, "ok")
        self.assertEqual({table.row_count for table in result.tables}, {3, 2})


if __name__ == "__main__":
    unittest.main()
