"""Drive ``_paginate_current_view`` against a fake page object.

This is the regression test for the original bug: on a ``pagingType: "numbers"``
pager there is no "Next" control, so the old code found zero candidates and
stopped silently at page 1 while reporting ``status: ok``.
"""

import unittest

from smp_extractor.collector import (
    PAGE_DIGEST_JS,
    _apply_pagination_accounting,
    _paginate_current_view,
    _report_incomplete,
)
from smp_extractor.models import SchoolResult

PAGER_DIGEST_PROBE = PAGE_DIGEST_JS

CODE = "32230155"
URL = f"https://smp26.pemblem.test/school-detail/{CODE}".replace("pemblem", "pesrp")


def _rows(start, count):
    return [[str(start + offset), "x"] for offset in range(count)]


class FakePage:
    """Minimal stand-in for a Playwright page rendered by DataTables."""

    def __init__(
        self,
        total=18,
        page_size=5,
        length_has_all=True,
        select_supported=True,
        step_works=True,
    ):
        self.url = URL
        self.total = total
        self.page_size = page_size
        self.length_has_all = length_has_all
        self.select_supported = select_supported
        self.step_works = step_works
        self.show_all = False
        self.page = 0
        self.clicked: list[str] = []
        self.selected: list[str] = []

    # ---- geometry -----------------------------------------------------
    @property
    def effective_size(self):
        return self.total if self.show_all else self.page_size

    @property
    def page_count(self):
        if self.effective_size <= 0:
            return 0
        return max(1, -(-self.total // self.effective_size))

    def visible_rows(self):
        if self.show_all:
            return _rows(1, self.total)
        start = self.page * self.page_size + 1
        remaining = self.total - self.page * self.page_size
        return _rows(start, max(0, min(self.page_size, remaining)))

    # ---- Playwright surface -------------------------------------------
    def _snapshot(self):
        rows = self.visible_rows()
        return [
            {
                "index": 0,
                "id": "DataTables_Table_0",
                "classes": "dataTable consumption-summary-table",
                "title": "CONSUMPTION",
                "rows": [{"in_thead": False, "cells": [{"text": cell, "tag": "TD"} for cell in row]} for row in rows],
            }
        ]

    def evaluate(self, script, arg=None):
        if "tableIndex" in script and "infoText" in script:  # PAGER_INFO_JS
            shown = self.visible_rows()
            total = self.total
            if not shown:
                text = "No matching records found"
            else:
                text = f"Showing {shown[0][0]} to {shown[-1][0]} of {total} entries"
            return [
                {
                    "tableIndex": 0,
                    "tableId": "DataTables_Table_0",
                    "infoText": text,
                    "hasPager": True,
                    "hasLength": True,
                    "lengthValue": "-1" if self.show_all else str(self.page_size),
                    "lengthHasAll": self.length_has_all,
                    "numericIdxs": list(range(self.page_count)),
                    "activeIdx": 0 if self.show_all else self.page,
                }
            ]
        if "in_thead" in script:  # TABLE_SNAPSHOT_JS (must precede the digest)
            return self._snapshot()
        if script.strip() == PAGER_DIGEST_PROBE:  # PAGE_DIGEST_JS
            return "|".join(row[0] for row in self.visible_rows())
        if "visited" in script:  # page-step candidates
            current = 0 if self.show_all else self.page
            return [
                {
                    "pageIndex": index,
                    "next": False,
                    "signature": f"DataTables_Table_0|{index + 1}",
                }
                for index in range(self.page_count)
                if index != current and index not in set(arg[1] if arg else [])
            ]
        if "signature =>" in script:  # element handle resolver
            signature = arg
            return {"signature": signature}
        raise AssertionError(f"unexpected script: {script[:60]}")

    def evaluate_handle(self, script, arg=None):
        class Handle:
            def as_element(inner):
                class Element:
                    def click(inner_self, timeout=None):
                        page_index = int(str(arg).split("|")[-1]) - 1
                        self.clicked.append(str(arg))
                        if self.step_works:
                            self.page = page_index

                return Element()

        return Handle()

    def locator(self, selector):
        page = self

        class Locator:
            @property
            def first(self):
                return self

            def count(self):
                return 1 if "length select" in selector else 0

            def select_option(self, value, timeout=None):
                if not page.select_supported:
                    raise TimeoutError("select_option timed out")
                page.selected.append(value)
                if value == "-1":
                    page.show_all = True

        return Locator()

    def wait_for_timeout(self, milliseconds):
        return None

    def title(self):
        return "Dashboard | School Meal Program"


def _run(page, max_pages=100, result=None):
    result = result or SchoolResult(emis_code=CODE, url=URL)
    table_map = {}
    _paginate_current_view(
        page,
        result,
        table_map,
        "default view",
        URL,
        max_pages,
        5000,
    )
    result.tables = sorted(
        table_map.values(), key=lambda item: (item.table_index, item.title.casefold())
    )
    _apply_pagination_accounting(page, result)
    _report_incomplete(result)
    return result, table_map


class PaginationLoopTests(unittest.TestCase):
    def test_show_all_captures_every_row_in_one_step(self):
        page = FakePage(total=18, page_size=5)
        result, table_map = _run(page)

        self.assertEqual(page.selected, ["-1"])
        self.assertEqual(page.clicked, [], "show-all should avoid page walking")
        tables = list(table_map.values())
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table.row_count, 18)
        self.assertEqual(table.rows_expected, 18)
        self.assertTrue(table.is_complete)
        self.assertIn("show all", " ".join(table.capture_contexts))
        self.assertEqual(result.warnings, [])

    def test_walks_numbered_pages_when_show_all_is_unavailable(self):
        page = FakePage(total=18, page_size=5, length_has_all=False)
        result, table_map = _run(page)

        self.assertEqual(page.selected, [])
        self.assertEqual(len(page.clicked), 3)  # pages 2, 3, 4
        table = next(iter(table_map.values()))
        self.assertEqual(table.row_count, 18)
        self.assertTrue(table.is_complete)
        self.assertEqual(result.warnings, [])

    def test_walks_numbered_pages_when_show_all_times_out(self):
        page = FakePage(total=18, page_size=5, select_supported=False)
        result, table_map = _run(page)

        table = next(iter(table_map.values()))
        self.assertEqual(table.row_count, 18)
        self.assertTrue(table.is_complete)

    def test_stops_cleanly_when_a_step_button_does_nothing(self):
        page = FakePage(total=18, page_size=5, length_has_all=False, step_works=False)
        result, table_map = _run(page)

        table = next(iter(table_map.values()))
        self.assertEqual(table.row_count, 5)
        self.assertFalse(table.is_complete)
        self.assertTrue(
            any("did not change the rendered tables" in warning for warning in result.warnings)
        )

    def test_respects_max_pages_and_says_so(self):
        page = FakePage(total=200, page_size=5, length_has_all=False)
        result, table_map = _run(page, max_pages=2)

        table = next(iter(table_map.values()))
        self.assertLess(table.row_count, 200)
        self.assertTrue(
            any("configured limit" in warning for warning in result.warnings)
        )

    def test_empty_table_is_recorded_as_zero_rows_without_warnings(self):
        page = FakePage(total=0, page_size=5)
        result, table_map = _run(page)

        table = next(iter(table_map.values()))
        self.assertEqual(table.row_count, 0)
        self.assertEqual(table.rows_expected, 0)
        self.assertTrue(table.is_complete)
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
