from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .collector import DEFAULT_BASE_URL, DEFAULT_CODES, ExtractionError, extract_schools
from .exporters import write_export_bundle


def _validate_codes(codes: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    allowed = set(DEFAULT_CODES)
    for code in codes:
        value = str(code).strip()
        if value not in allowed:
            raise ValueError(
                f"EMIS code {value!r} is outside the six-code scope configured for this tool."
            )
        if value not in seen:
            cleaned.append(value)
            seen.add(value)
    if not cleaned:
        raise ValueError("No EMIS codes were provided.")
    return cleaned


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must be a full http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the base URL.")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL must not include a query string or fragment.")
    return value.strip().rstrip("/")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smp-extractor",
        description=(
            "Sign in to the SMP portal and export rendered school-detail tables "
            "to a local Excel workbook and CSV files."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Portal base URL.")
    parser.add_argument(
        "--codes",
        nargs="+",
        help="Subset of the six configured EMIS codes. Defaults to all six."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exports"),
        help="Local parent directory for timestamped export bundles (default: exports).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to wait between school pages (default: 1.5).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Navigation timeout in seconds (default: 45).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Maximum pagination advances per view (default: 100).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium without a visible window. By default, the browser is visible.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt; read SMP_USERNAME and SMP_PASSWORD from the environment.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    return parser


def _print_results(results: list, output_dir: Path) -> None:
    print("\nExtraction summary")
    print("=" * 72)
    for result in results:
        print(
            f"{result.emis_code}: {result.status} | "
            f"{result.table_count} table(s), {result.row_count} row(s)"
        )
        if result.error:
            print(f"  Error: {result.error}")
        for warning in result.warnings:
            print(f"  Note: {warning}")
    print(f"\nExport bundle: {output_dir.resolve()}")
    print(f"Workbook: {output_dir / 'smp_tables.xlsx'}")
    print(f"CSV files: {output_dir / 'csv'}")
    print(f"Manifest: {output_dir / 'run_manifest.json'}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        requested_codes = args.codes if args.codes is not None else list(DEFAULT_CODES)
        codes = _validate_codes(requested_codes)
        base_url = _validate_base_url(args.base_url)
        if args.delay < 0:
            raise ValueError("--delay must be zero or greater.")
        if args.timeout < 1:
            raise ValueError("--timeout must be at least one second.")
        if not 1 <= args.max_pages <= 1000:
            raise ValueError("--max-pages must be between 1 and 1000.")
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Portal: {base_url}")
    print(f"EMIS codes: {', '.join(codes)}")
    print("Credentials are requested only if the portal presents a login form.")
    print("No password is written to the export or source files.\n")

    try:
        results = extract_schools(
            base_url,
            codes,
            delay_seconds=args.delay,
            headless=args.headless,
            timeout_seconds=args.timeout,
            max_pages=args.max_pages,
            non_interactive=args.non_interactive,
        )
        output_dir = write_export_bundle(results, base_url, args.output_dir)
    except (ExtractionError, OSError, ValueError) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled by user. No further portal requests will be made.", file=sys.stderr)
        return 130

    _print_results(results, output_dir)
    return 0 if all(result.status == "ok" for result in results) else 1
