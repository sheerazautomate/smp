import unittest

from smp_extractor.pagination import (
    PagerState,
    missing_rows,
    pager_state,
    pager_states,
    parse_info_text,
)


class ParseInfoTextTests(unittest.TestCase):
    def test_standard_datatables_info_line(self):
        self.assertEqual(parse_info_text("Showing 1 to 5 of 18 entries"), (1, 5, 18))

    def test_thousands_separators_are_handled(self):
        self.assertEqual(
            parse_info_text("Showing 1 to 50 of 1,240 entries"), (1, 50, 1240)
        )

    def test_filtered_variant(self):
        self.assertEqual(
            parse_info_text("Showing 1 to 5 of 18 entries (filtered from 240 total entries)"),
            (1, 5, 18),
        )

    def test_single_page_table(self):
        self.assertEqual(parse_info_text("Showing 1 to 18 of 18 entries"), (1, 18, 18))

    def test_empty_state_strings(self):
        for text in (
            "No entries found",
            "No matching records found",
            "No data available",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_info_text(text), (0, 0, 0))

    def test_unparseable_text_returns_none(self):
        for text in ("", "   ", "Something else entirely", "of entries"):
            with self.subTest(text=text):
                self.assertIsNone(parse_info_text(text))


class PagerStateTests(unittest.TestCase):
    def _raw(self, **overrides):
        raw = {
            "tableIndex": 0,
            "tableId": "DataTables_Table_0",
            "infoText": "Showing 1 to 5 of 18 entries",
            "hasPager": True,
            "hasLength": True,
            "lengthValue": "5",
            "lengthHasAll": True,
            "numericIdxs": [0, 1, 2, 3],
            "activeIdx": 0,
        }
        raw.update(overrides)
        return raw

    def test_page_size_and_count_from_pasted_markup(self):
        state = pager_state(self._raw())
        self.assertIsNotNone(state)
        self.assertEqual(state.page_size, 5)
        self.assertEqual(state.page_count, 4)
        self.assertEqual(state.total_rows, 18)
        self.assertTrue(state.is_paginated)
        self.assertTrue(state.can_show_all)

    def test_selected_all_suppresses_the_fast_path(self):
        state = pager_state(self._raw(lengthValue="-1"))
        self.assertFalse(state.can_show_all)

    def test_no_all_option_suppresses_the_fast_path(self):
        state = pager_state(self._raw(lengthHasAll=False))
        self.assertFalse(state.can_show_all)

    def test_single_page_table_is_not_paginated(self):
        state = pager_state(
            self._raw(infoText="Showing 1 to 3 of 3 entries", numericIdxs=[], activeIdx=None)
        )
        self.assertFalse(state.is_paginated)
        self.assertEqual(state.page_count, 1)

    def test_unknown_info_text_yields_no_state(self):
        self.assertIsNone(pager_state(self._raw(infoText="Loading...")))

    def test_page_count_falls_back_to_arithmetic_without_links(self):
        state = pager_state(self._raw(numericIdxs=[], activeIdx=None))
        self.assertEqual(state.page_count, 4)  # ceil(18 / 5)

    def test_pager_states_null_out_for_tables_without_an_info_line(self):
        records = [
            self._raw(tableIndex=0),
            self._raw(tableIndex=1, tableId="enrollment-table", infoText="", hasPager=False),
        ]
        states = pager_states(records)
        self.assertEqual(sorted(states), [0])
        self.assertEqual(states[0].total_rows, 18)

    def test_pager_states_ignores_malformed_records(self):
        self.assertEqual(pager_states([None, {}, {"tableIndex": "x"}]), {})

    def test_missing_rows(self):
        self.assertEqual(missing_rows(18, 5), 13)
        self.assertEqual(missing_rows(18, 18), 0)
        self.assertEqual(missing_rows(5, 18), 0)
        self.assertEqual(missing_rows(None, 3), 0)


class CompletenessWiringTests(unittest.TestCase):
    def test_table_reports_incomplete_until_all_rows_are_captured(self):
        from smp_extractor.models import ExtractedTable

        table = ExtractedTable(
            emis_code="32230155",
            table_index=0,
            table_ref="DataTables_Table_0",
            title="CONSUMPTION",
            headers=["Sr#", "Date"],
            rows=[["1", "04-11-2026"]],
            rows_expected=18,
        )
        self.assertFalse(table.is_complete)
        self.assertEqual(table.rows_missing, 17)

        table.rows.extend([["2", "03-11-2026"]] * 17)
        self.assertTrue(table.is_complete)
        self.assertEqual(table.rows_missing, 0)

    def test_table_without_an_expected_count_counts_as_complete(self):
        from smp_extractor.models import ExtractedTable

        table = ExtractedTable(
            emis_code="32230155",
            table_index=0,
            table_ref="t",
            title="t",
            headers=["a"],
            rows=[["1"]],
        )
        self.assertIsNone(table.rows_expected)
        self.assertTrue(table.is_complete)


if __name__ == "__main__":
    unittest.main()
