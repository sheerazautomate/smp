#!/usr/bin/env python3
"""Stage a finished SMP export bundle inside the repository and refresh the index.

The GitHub Actions workflow runs the extractor into a scratch directory and then
calls this script, which:

* copies the newest completed bundle into ``data/exports/<run_id>``,
* updates ``data/exports/index.json`` with a small per-run summary,
* prunes older bundles so the repository stays a reasonable size,
* writes a Markdown summary for the workflow run page.

It only touches files under ``--data-root``. Committing and pushing is the
workflow's job, so this script stays safe to run locally too.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "run_manifest.json"
INDEX_NAME = "index.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _display_path(path: Path) -> str:
    """Render a path relative to the current directory when possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def list_bundles(root: Path) -> list[Path]:
    """Return bundle directories (newest last), oldest first."""
    if not root.is_dir():
        return []
    bundles = [
        child
        for child in root.iterdir()
        if child.is_dir() and (child / MANIFEST_NAME).is_file()
    ]
    return sorted(bundles, key=lambda path: path.name)


def newest_bundle(root: Path) -> Path | None:
    bundles = list_bundles(root)
    return bundles[-1] if bundles else None


def load_manifest(bundle: Path) -> dict[str, Any]:
    with (bundle / MANIFEST_NAME).open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError(f"{bundle / MANIFEST_NAME} does not contain a JSON object.")
    return manifest


def stage_bundle(source_bundle: Path, data_root: Path) -> Path:
    """Copy a bundle into the repository, never overwriting an existing run."""
    data_root.mkdir(parents=True, exist_ok=True)
    target = data_root / source_bundle.name
    suffix = 2
    while target.exists():
        target = data_root / f"{source_bundle.name}_{suffix:02d}"
        suffix += 1
    shutil.copytree(source_bundle, target)
    return target


def bundle_totals(manifest: dict[str, Any]) -> tuple[dict[str, str], int, int]:
    statuses: dict[str, str] = {}
    table_count = 0
    row_count = 0
    for result in manifest.get("results") or []:
        if not isinstance(result, dict):
            continue
        code = str(result.get("emis_code") or "")
        statuses[code] = str(result.get("status") or "unknown")
        try:
            table_count += int(result.get("table_count") or 0)
            row_count += int(result.get("row_count") or 0)
        except (TypeError, ValueError):
            continue
    return statuses, table_count, row_count


def repo_path(data_root: Path, bundle: Path) -> str:
    """Path recorded in the index, relative to the working directory when possible."""
    suffix = bundle.name
    try:
        return (data_root.resolve().relative_to(Path.cwd().resolve()) / suffix).as_posix()
    except (ValueError, OSError):
        return f"{data_root.as_posix().rstrip('/')}/{suffix}"


def build_entry(
    bundle: Path,
    manifest: dict[str, Any],
    run_url: str = "",
    data_root: Path | None = None,
) -> dict[str, Any]:
    statuses, table_count, row_count = bundle_totals(manifest)
    return {
        "run_id": bundle.name,
        "path": repo_path(data_root, bundle) if data_root else _display_path(bundle),
        "generated_at_utc": str(manifest.get("generated_at_utc") or ""),
        "tool_version": str(manifest.get("version") or ""),
        "base_url": str(manifest.get("base_url") or ""),
        "requested_codes": [str(code) for code in manifest.get("requested_codes") or []],
        "statuses": statuses,
        "table_count": table_count,
        "row_count": row_count,
        "workflow_run_url": run_url,
    }


def update_index(
    data_root: Path,
    entry: dict[str, Any],
    bundle: Path,
) -> dict[str, Any]:
    """Merge the new run into ``index.json`` and drop entries with no bundle."""
    index_path = data_root / INDEX_NAME
    runs: list[dict[str, Any]] = []
    if index_path.is_file():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        if isinstance(existing, dict):
            runs = [run for run in existing.get("runs") or [] if isinstance(run, dict)]

    entry["path"] = repo_path(data_root, bundle)
    runs = [run for run in runs if run.get("run_id") != entry["run_id"]]
    runs.append(entry)
    runs.sort(key=lambda run: str(run.get("run_id") or ""), reverse=True)

    index = {
        "updated_at_utc": _utc_now(),
        "latest": entry["run_id"],
        "run_count": len(runs),
        "runs": runs,
    }
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def prune_bundles(data_root: Path, keep_last: int) -> list[Path]:
    """Delete the oldest bundles beyond ``keep_last``; return what was removed."""
    keep = max(1, keep_last)
    bundles = list_bundles(data_root)
    doomed = bundles[:-keep] if len(bundles) > keep else []
    removed: list[Path] = []
    for bundle in doomed:
        shutil.rmtree(bundle)
        removed.append(bundle)
    return removed


def render_summary(
    entry: dict[str, Any],
    manifest: dict[str, Any],
    removed: list[Path],
) -> str:
    lines = ["## SMP school table export", ""]
    lines.append(f"- Bundle: `{entry['path']}`")
    lines.append(f"- Generated (UTC): {entry['generated_at_utc'] or 'unknown'}")
    lines.append(f"- Portal: {entry['base_url'] or 'unknown'}")
    lines.append(f"- Tables: {entry['table_count']} · rows: {entry['row_count']}")
    if entry.get("workflow_run_url"):
        lines.append(f"- Workflow run: {entry['workflow_run_url']}")
    lines.append("")
    lines.append("| EMIS code | Status | Tables | Rows |")
    lines.append("| --- | --- | --- | --- |")
    for result in manifest.get("results") or []:
        if not isinstance(result, dict):
            continue
        lines.append(
            "| {code} | {status} | {tables} | {rows} |".format(
                code=result.get("emis_code") or "",
                status=result.get("status") or "unknown",
                tables=result.get("table_count") or 0,
                rows=result.get("row_count") or 0,
            )
        )

    warnings = [
        f"- {result.get('emis_code') or 'unknown'}: {text}"
        for result in manifest.get("results") or []
        if isinstance(result, dict)
        for text in ([*(result.get("warnings") or []), result.get("error") or ""])
        if text
    ]
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(warnings)
    if removed:
        lines.extend(["", f"Pruned {len(removed)} old bundle(s): " + ", ".join(p.name for p in removed)])
    lines.append("")
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish-exports",
        description=(
            "Copy a fresh SMP export bundle into the repository, refresh "
            "data/exports/index.json, and prune old bundles."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/exports"),
        help="Tracked directory that holds published bundles (default: data/exports).",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Scratch directory to copy the newest bundle from (workflow: runner temp).",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="Existing bundle to (re)index instead of staging a new one.",
    )
    parser.add_argument(
        "--keep-last",
        type=_positive_int,
        default=30,
        help="Number of most recent bundles to keep (default: 30).",
    )
    parser.add_argument(
        "--run-url",
        default="",
        help="Workflow run URL recorded in the index entry.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help="Write a Markdown summary here (typically $GITHUB_STEP_SUMMARY).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root: Path = args.data_root

    bundle: Path | None = None
    if args.source_dir is not None:
        fresh = newest_bundle(args.source_dir)
        if fresh is None:
            print(
                "::notice::No completed export bundle was produced; nothing to publish."
            )
            return 0
        bundle = stage_bundle(fresh, data_root)
        print(f"Staged bundle: {bundle}")
    elif args.bundle is not None:
        bundle = args.bundle
        if not (bundle / MANIFEST_NAME).is_file():
            print(f"::error::{bundle} does not contain {MANIFEST_NAME}.", file=sys.stderr)
            return 2
    else:
        bundle = newest_bundle(data_root)

    if bundle is None:
        print("::notice::No export bundles found; nothing to publish.")
        return 0

    try:
        manifest = load_manifest(bundle)
    except (OSError, ValueError) as exc:
        print(f"::error::Could not read {bundle / MANIFEST_NAME}: {exc}", file=sys.stderr)
        return 2

    entry = build_entry(bundle, manifest, args.run_url)
    index = update_index(data_root, entry, bundle)
    removed = prune_bundles(data_root, args.keep_last)

    summary = render_summary(entry, manifest, removed)
    print(summary)
    if args.summary_file is not None:
        try:
            args.summary_file.parent.mkdir(parents=True, exist_ok=True)
            with args.summary_file.open("a", encoding="utf-8") as handle:
                handle.write(summary + "\n")
        except OSError as exc:
            print(f"::warning::Could not write the step summary: {exc}")

    print(f"Index now lists {index['run_count']} run(s): {data_root / INDEX_NAME}")
    if removed:
        print("Pruned: " + ", ".join(path.name for path in removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
