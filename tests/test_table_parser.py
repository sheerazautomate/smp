import unittest

from smp_extractor.models import ExtractedTable
from smp_extractor.table_parser import normalize_table


class TableParserTests(unittest.TestCase):
    def test_uses_thead_as_column_headers(self):
        snapshot = {
            "index": 0,
            "id": "school-profile",
            "title": "School profile",
            "rows": [
                {
                    "in_thead": True,
                    "cells": [
                        {"text": "EMIS", "tag": "TH"},
                        {"text": "School name", "tag": "TH"},
                    ],
                },
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "32230155", "tag": "TD"},
                        {"text": "  Govt. School\nA ", "tag": "TD"},
                    ],
                },
            ],
        }

        table = normalize_table(snapshot, "32230155", "https://example.test/detail")

        self.assertEqual(table.headers, ["EMIS", "School name"])
        self.assertEqual(table.rows, [["32230155", "Govt. School A"]])
        self.assertEqual(table.title, "School profile")
        self.assertEqual(table.table_ref, "school-profile")

    def test_key_value_rows_are_not_mistaken_for_headers(self):
        snapshot = {
            "index": 1,
            "rows": [
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "District", "tag": "TH"},
                        {"text": "Lahore", "tag": "TD"},
                    ],
                },
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "Tehsil", "tag": "TH"},
                        {"text": "Model Town", "tag": "TD"},
                    ],
                },
            ],
        }

        table = normalize_table(snapshot, "32230155", "https://example.test/detail")

        self.assertEqual(table.headers, ["Column 1", "Column 2"])
        self.assertEqual(
            table.rows,
            [["District", "Lahore"], ["Tehsil", "Model Town"]],
        )

    def test_tbody_group_title_is_not_misclassified_as_column_header(self):
        snapshot = {
            "index": 3,
            "rows": [
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "School information", "tag": "TH", "colspan": 2}
                    ],
                },
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "District", "tag": "TD"},
                        {"text": "Lahore", "tag": "TD"},
                    ],
                },
            ],
        }

        table = normalize_table(snapshot, "32230155", "https://example.test/detail")

        self.assertEqual(table.headers, ["Column 1", "Column 2"])
        self.assertEqual(
            table.rows,
            [["School information", "School information"], ["District", "Lahore"]],
        )

    def test_expands_rowspan_and_colspan_in_multirow_header(self):
        snapshot = {
            "index": 0,
            "rows": [
                {
                    "in_thead": True,
                    "cells": [
                        {"text": "School", "tag": "TH", "rowspan": 2},
                        {"text": "Enrollment", "tag": "TH", "colspan": 2},
                    ],
                },
                {
                    "in_thead": True,
                    "cells": [
                        {"text": "Boys", "tag": "TH"},
                        {"text": "Girls", "tag": "TH"},
                    ],
                },
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "Primary", "tag": "TD"},
                        {"text": "12", "tag": "TD"},
                        {"text": "10", "tag": "TD"},
                    ],
                },
            ],
        }

        table = normalize_table(snapshot, "32230155", "https://example.test/detail")

        self.assertEqual(
            table.headers,
            ["School", "Enrollment / Boys", "Enrollment / Girls"],
        )
        self.assertEqual(table.rows, [["Primary", "12", "10"]])

    def test_duplicate_headers_are_made_unique_and_empty_rows_removed(self):
        snapshot = {
            "index": 2,
            "rows": [
                {
                    "in_thead": True,
                    "cells": [
                        {"text": "Name", "tag": "TH"},
                        {"text": "Name", "tag": "TH"},
                    ],
                },
                {"in_thead": False, "cells": [{"text": "", "tag": "TD"}]},
                {
                    "in_thead": False,
                    "cells": [
                        {"text": "A", "tag": "TD"},
                        {"text": "B", "tag": "TD"},
                    ],
                },
            ],
        }

        table = normalize_table(snapshot, "32230155", "https://example.test/detail")

        self.assertEqual(table.headers, ["Name", "Name (2)"])
        self.assertEqual(table.rows, [["A", "B"]])

    def test_exact_repeated_snapshot_is_not_appended_twice(self):
        table = ExtractedTable(
            emis_code="32230155",
            table_index=0,
            table_ref="dom-index:0",
            title="Table 1",
            headers=["A"],
        )
        rows = [["same"], ["same"]]

        self.assertTrue(table.add_snapshot(rows, "https://example.test/1", "page 1"))
        self.assertFalse(table.add_snapshot(rows, "https://example.test/1", "page 1 again"))
        self.assertEqual(table.rows, [["same"], ["same"]])
        self.assertEqual(table.capture_contexts, ["page 1", "page 1 again"])


if __name__ == "__main__":
    unittest.main()
