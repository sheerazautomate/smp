import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.publish_exports import (  # noqa: E402
    build_entry,
    list_bundles,
    load_manifest,
    main,
    newest_bundle,
    prune_bundles,
    render_summary,
    stage_bundle,
    update_index,
)


def _make_bundle(root: Path, run_id: str, **overrides) -> Path:
    bundle = root / run_id
    bundle.mkdir(parents=True)
    (bundle / "csv").mkdir()
    (bundle / "csv" / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    manifest = {
        "tool": "smp-table-extractor",
        "version": "0.1.0",
        "generated_at_utc": f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}T00:00:00+00:00",
        "base_url": "https://example.test",
        "requested_codes": ["32230155"],
        "results": [
            {
                "emis_code": "32230155",
                "status": "ok",
                "table_count": 2,
                "row_count": 5,
                "warnings": [],
                "error": "",
            }
        ],
    }
    manifest.update(overrides)
    (bundle / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "smp_tables.xlsx").write_bytes(b"not-a-real-workbook")
    return bundle


class PublishExportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data" / "exports"

    def test_stage_bundle_copies_bundle_and_avoids_clobbering(self):
        source = _make_bundle(Path(self.tmp.name) / "scratch", "20260101_010101")

        first = stage_bundle(source, self.root)
        second = stage_bundle(source, self.root)

        self.assertEqual(first.name, "20260101_010101")
        self.assertEqual(second.name, "20260101_010101_02")
        self.assertTrue((first / "csv" / "table.csv").is_file())
        self.assertTrue((first / "smp_tables.xlsx").is_file())
        # The source bundle must be left in place for the artifact upload step.
        self.assertTrue((source / "run_manifest.json").is_file())

    def test_newest_bundle_ignores_incomplete_directories(self):
        scratch = Path(self.tmp.name) / "scratch"
        _make_bundle(scratch, "20260101_010101")
        (scratch / "20260102_010101").mkdir()

        self.assertEqual(newest_bundle(scratch).name, "20260101_010101")

    def test_newest_bundle_returns_none_for_empty_directory(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertIsNone(newest_bundle(empty))
        self.assertIsNone(newest_bundle(Path(self.tmp.name) / "missing"))
        self.assertEqual(list_bundles(empty), [])

    def test_update_index_is_newest_first_and_idempotent(self):
        older = stage_bundle(
            _make_bundle(Path(self.tmp.name) / "s1", "20260101_010101"), self.root
        )
        newer = stage_bundle(
            _make_bundle(Path(self.tmp.name) / "s2", "20260102_010101"), self.root
        )

        for bundle in (older, newer, newer):
            update_index(self.root, build_entry(bundle, load_manifest(bundle)), bundle)

        index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([run["run_id"] for run in index["runs"]], [newer.name, older.name])
        self.assertEqual(index["latest"], newer.name)
        self.assertTrue(index["runs"][0]["path"].endswith(newer.name))
        self.assertEqual(index["run_count"], 2)
        self.assertEqual(index["runs"][0]["table_count"], 2)
        self.assertEqual(index["runs"][0]["row_count"], 5)
        self.assertEqual(index["runs"][0]["statuses"], {"32230155": "ok"})

    def test_update_index_recovers_from_a_corrupt_index_file(self):
        bundle = stage_bundle(
            _make_bundle(Path(self.tmp.name) / "s1", "20260101_010101"), self.root
        )
        (self.root / "index.json").write_text("{not json", encoding="utf-8")

        index = update_index(self.root, build_entry(bundle, load_manifest(bundle)), bundle)

        self.assertEqual(index["run_count"], 1)

    def test_prune_bundles_keeps_the_newest_runs(self):
        names = ["20260101_010101", "20260102_010101", "20260103_010101"]
        for name in names:
            stage_bundle(_make_bundle(Path(self.tmp.name) / name, name), self.root)

        removed = prune_bundles(self.root, keep_last=2)

        self.assertEqual([path.name for path in removed], [names[0]])
        self.assertEqual([path.name for path in list_bundles(self.root)], names[1:])

    def test_prune_bundles_never_deletes_everything(self):
        stage_bundle(_make_bundle(Path(self.tmp.name) / "s", "20260101_010101"), self.root)

        # keep_last is clamped to at least one so a bad input cannot wipe history.
        self.assertEqual(prune_bundles(self.root, keep_last=0), [])
        self.assertEqual(len(list_bundles(self.root)), 1)

    def test_render_summary_lists_statuses_and_warnings(self):
        manifest = {
            "generated_at_utc": "2026-01-01T00:00:00+00:00",
            "base_url": "https://example.test",
            "results": [
                {
                    "emis_code": "32230155",
                    "status": "ok",
                    "table_count": 2,
                    "row_count": 5,
                    "warnings": ["Pagination was capped."],
                    "error": "",
                },
                {
                    "emis_code": "32230183",
                    "status": "error",
                    "table_count": 0,
                    "row_count": 0,
                    "warnings": [],
                    "error": "Portal returned HTTP 500.",
                },
            ],
        }
        entry = {"path": "data/exports/run", "table_count": 2, "row_count": 5}
        entry.update({k: manifest[k] for k in ("generated_at_utc", "base_url")})
        entry["workflow_run_url"] = ""

        summary = render_summary(entry, manifest, [])

        self.assertIn("| 32230155 | ok | 2 | 5 |", summary)
        self.assertIn("Portal returned HTTP 500.", summary)
        self.assertIn("Pagination was capped.", summary)

    def test_main_publishes_a_new_bundle_and_writes_the_summary(self):
        scratch = Path(self.tmp.name) / "scratch"
        _make_bundle(scratch, "20260101_010101")
        summary_file = Path(self.tmp.name) / "summary.md"

        exit_code = main(
            [
                "--source-dir",
                str(scratch),
                "--data-root",
                str(self.root),
                "--keep-last",
                "5",
                "--run-url",
                "https://github.test/run/1",
                "--summary-file",
                str(summary_file),
            ]
        )

        self.assertEqual(exit_code, 0)
        index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["runs"][0]["workflow_run_url"], "https://github.test/run/1")
        self.assertTrue((self.root / "20260101_010101" / "run_manifest.json").is_file())
        self.assertIn("SMP school table export", summary_file.read_text(encoding="utf-8"))

    def test_main_is_a_no_op_when_the_extraction_produced_nothing(self):
        exit_code = main(
            ["--source-dir", str(Path(self.tmp.name) / "empty"), "--data-root", str(self.root)]
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse((self.root / "index.json").exists())

    def test_main_rejects_a_bundle_that_cannot_be_read(self):
        broken = self.root / "20260101_010101"
        broken.mkdir(parents=True)
        (broken / "run_manifest.json").write_text("[1, 2, 3]", encoding="utf-8")

        self.assertEqual(main(["--bundle", str(broken), "--data-root", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
