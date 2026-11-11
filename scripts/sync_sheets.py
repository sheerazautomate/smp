#!/usr/bin/env python3
"""Push an SMP export bundle into the Google Sheets master workbook.

Usage::

    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
    export SMP_SHEET_ID=1oz7edUR9ZKT-Knm7Lp90p5NsuPAhi7ytQz5GDlkfZPA
    python scripts/sync_sheets.py --bundle data/exports/20261111_080706

The script only fills the five derived tabs. ``RAW Milk`` and ``Raw Biscuits``
are never written: they are maintained by hand and the request that introduced
this tool explicitly excluded them.

The workbook's Summary tab is formula-driven, so nothing needs to be pushed
there; it recalculates from the four tabs this script writes.

Exit codes: 0 success (or nothing to do), 1 error, 2 layout refuses the data
(the sheet needs widening before it can accept this bundle).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# Allow `python scripts/sync_sheets.py ...` from a checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheets.plan import SheetWrite, build_writes, load_bundle  # noqa: E402

#: Tabs this tool is allowed to write. Everything else is off limits.
DRY_RUN_ENV = "SMP_SHEET_DRY_RUN"
SHEET_ID_ENV = "SMP_SHEET_ID"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

#: Google's batchUpdate accepts a Values.ValueRange list; keep batches modest.
MAX_RANGES_PER_BATCH = 20


@dataclass
class SyncResult:
    writes: list[dict[str, Any]]
    updated_cells: int = 0


def _imports():
    """Import the Google client lazily so the planner stays dependency-free."""
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as error:  # pragma: no cover - exercised manually
        raise SystemExit(
            "google-api-python-client and google-auth are required: "
            "pip install 'google-api-python-client>=2' 'google-auth>=2' "
            f"({error})"
        ) from error
    return service_account, build


def build_service(credentials_path: str | None = None):
    """Build Sheets v4 client from a service-account file or ambient ADC."""
    service_account, build = _imports()
    source = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if source:
        credentials = service_account.Credentials.from_service_account_file(
            source, scopes=SCOPES
        )
    else:
        try:
            import google.auth  # type: ignore
        except ImportError as error:  # pragma: no cover
            raise SystemExit(f"google-auth is required: {error}") from error
        credentials, _ = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def write_rows(
    service,
    spreadsheet_id: str,
    writes: Sequence[SheetWrite],
    *,
    value_input_option: str = "USER_ENTERED",
) -> SyncResult:
    """Write each :class:`SheetWrite` in its own ``values.update`` call."""
    result = SyncResult(writes=[])
    for write in writes:
        response = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=write.a1_range,
                valueInputOption=value_input_option,
                body={"range": write.a1_range, "majorDimension": "ROWS",
                      "values": write.values},
            )
            .execute()
        )
        updated = int(response.get("updatedCells", 0)) if response else 0
        result.updated_cells += updated
        result.writes.append(
            {"range": write.a1_range, "updated_cells": updated}
        )
    return result


def clear_stale_tail(
    service, spreadsheet_id: str, writes: Sequence[SheetWrite], sheet_sizes: dict[str, int]
) -> list[dict[str, Any]]:
    """Blank the date columns a previous, wider bundle left behind.

    Without this, shrinking from 20 service days to 18 would keep the last two
    days of the older bundle on screen.
    """
    cleared: list[dict[str, Any]] = []
    for write in writes:
        available = sheet_sizes.get(write.tab)
        if not available or write.end_column() >= available:
            continue
        start = _column_name(write.end_column() + 1)
        end = _column_name(available)
        a1 = f"'{write.tab}'!{start}{write.start_row()}:{end}{write.end_row()}"
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=a1, body={}
        ).execute()
        cleared.append({"range": a1})
    return cleared


def sheet_sizes(service, spreadsheet_id: str) -> dict[str, int]:
    """Column count per sheet, used to decide what the stale tail is."""
    metadata = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    sizes: dict[str, int] = {}
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        title = properties.get("title")
        count = (properties.get("gridProperties") or {}).get("columnCount")
        if title and count:
            sizes[title] = int(count)
    return sizes


def _column_name(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bundle",
        type=Path,
        help="Export bundle directory (defaults to the newest under --data-root).",
    )
    parser.add_argument(
        "--data-root", type=Path, default=Path("data/exports"),
        help="Directory holding export bundles (default: data/exports).",
    )
    parser.add_argument(
        "--spreadsheet-id", default=os.environ.get(SHEET_ID_ENV),
        help=f"Target workbook (default: ${SHEET_ID_ENV}).",
    )
    parser.add_argument(
        "--credentials",
        help="Service-account JSON (default: $GOOGLE_APPLICATION_CREDENTIALS).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the ranges and exit without contacting Google Sheets.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    bundle = args.bundle
    if bundle is None:
        candidates = sorted(
            path
            for path in args.data_root.iterdir()
            if path.is_dir() and (path / "run_manifest.json").is_file()
        )
        if not candidates:
            print(f"No export bundles under {args.data_root}", file=sys.stderr)
            return 1
        bundle = candidates[-1]

    try:
        schools, manifest = load_bundle(bundle)
        writes = build_writes(schools)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Refusing to sync {bundle}: {error}", file=sys.stderr)
        return 2

    plan = {
        "bundle": str(bundle),
        "generated_at_utc": manifest.get("generated_at_utc"),
        "ranges": [
            {
                "tab": write.tab,
                "range": write.a1_range,
                "rows": len(write.values),
                "columns": max(len(row) for row in write.values),
            }
            for write in writes
        ],
    }

    dry_run = args.dry_run or os.environ.get(DRY_RUN_ENV) == "1"
    if dry_run:
        payload = {"dry_run": True, **plan}
        print(json.dumps(payload, indent=2) if args.json else _human_plan(payload))
        return 0

    if not args.spreadsheet_id:
        print(
            f"Set --spreadsheet-id or ${SHEET_ID_ENV} to the target workbook id.",
            file=sys.stderr,
        )
        return 1

    service = build_service(args.credentials)
    sizes = sheet_sizes(service, args.spreadsheet_id)
    too_wide = [
        write for write in writes
        if write.tab in sizes and write.end_column() > sizes[write.tab]
    ]
    if too_wide:
        for write in too_wide:
            print(
                f"{write.tab}: needs {write.end_column()} columns but the sheet has "
                f"{sizes[write.tab]}. Widen the tab, then re-run.",
                file=sys.stderr,
            )
        return 2

    cleared = clear_stale_tail(service, args.spreadsheet_id, writes, sizes)
    result = write_rows(service, args.spreadsheet_id, writes)

    summary = {**plan, "dry_run": False, "cleared": cleared, **result.__dict__}
    print(json.dumps(summary, indent=2) if args.json else _human_plan(summary))
    return 0


def _human_plan(payload: dict[str, Any]) -> str:
    lines = [f"Bundle: {payload['bundle']}"]
    if payload.get("generated_at_utc"):
        lines.append(f"Extracted: {payload['generated_at_utc']}")
    lines.append("")
    for entry in payload["ranges"]:
        cells = entry.get("updated_cells")
        suffix = f"  ({cells} cells written)" if cells else ""
        lines.append(
            f"  {entry['tab']:<22} {entry['range']:<28} "
            f"{entry['rows']}x{entry['columns']}{suffix}"
        )
    for entry in payload.get("cleared") or []:
        lines.append(f"  cleared stale  {entry['range']}")
    if payload.get("dry_run"):
        lines.append("\nDry run: nothing was sent to Google Sheets.")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
