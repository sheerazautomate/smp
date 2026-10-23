# SMP School Table Extractor

A local command-line utility for exporting rendered HTML tables from the SMP school-detail pages. Its configured scope is exactly the six EMIS codes supplied for this task; `--codes` can select a subset for a first pass but rejects codes outside that set. It signs in once, processes each `/school-detail/{EMIS}` page in the same browser session, and writes a timestamped export bundle on the machine running the tool.

## What it captures

- HTML `<table>` elements in the detail page, including tables rendered in hidden tab panels.
- Local tab/pill controls and supported collapsed sections that can reveal additional tables.
- Common pagination controls inside pagination/pager containers, with a configurable safety limit.
- Table captions/nearby headings, headers, rows, source URLs, and capture contexts for auditing.
- Header `rowspan`/`colspan` layout and key/value tables without a `<thead>`.

It does **not** follow unrelated links or other portal modules, and it does not scan tables inside iframes. It does not bypass authentication, CAPTCHA, rate limits, or other access controls. It extracts HTML tables, not data rendered only as charts or non-table UI widgets. The tab and pagination handling is deliberately heuristic; reconcile the first run's counts with the portal before using the export for official reporting.

## Install

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Run

By default, the browser is visible, the six supplied codes are used, and credentials are prompted only if a login form appears. The password prompt is hidden.

```bash
python -m smp_extractor
```

For a single-school first pass:

```bash
python -m smp_extractor --codes 32230155
```

The default command processes all six configured codes. `--codes` accepts a subset of those six for a first-pass check; it rejects any code outside the configured scope:

```bash
python -m smp_extractor --codes 32230155 32230183
```

Useful options:

```text
--output-dir PATH   Parent directory for timestamped bundles (default: exports)
--delay SECONDS     Delay between school pages (default: 1.5)
--timeout SECONDS   Navigation timeout (default: 45)
--max-pages N       Maximum page advances per table view (default: 100)
--headless          Hide the Chromium window
--non-interactive   Read SMP_USERNAME and SMP_PASSWORD from the environment; never prompt
```

For an unattended run, set `SMP_USERNAME` and `SMP_PASSWORD` in the process environment, then use `--non-interactive`. Do not put credentials in source code, command-line arguments, `.env` files, or an export; set environment variables through your shell or an approved secret manager. Interactive prompts are preferred on a personal workstation.

## Output

Each run creates a timestamped folder under `exports/` containing:

- `smp_tables.xlsx` — a `Summary` worksheet and one worksheet per captured table.
- `csv/` — one CSV file per table, encoded as UTF-8 with BOM for spreadsheet compatibility.
- `run_manifest.json` — requested codes, status, row/table counts, headers, source URLs, capture contexts, and warnings; it does not contain table values or credentials. Credential-like URL query parameters are redacted in provenance metadata.

The workbook writes portal values as explicit text cells, preserving leading zeros and preventing values from being interpreted as Excel formulas. CSV has no cell types, so formula-like values are prefixed with an apostrophe to reduce spreadsheet formula-injection risk; use the workbook when exact displayed values are required. No HTML snapshots, screenshots, or browser storage state are saved.

## Security and operating notes

- Use this only for schools and records you are authorized to access and export under your organization’s policy.
- The export can contain sensitive education data. Keep the output directory access-controlled, do not upload it to unapproved services, and delete it when no longer needed.
- Credentials are held only in memory for the run and are not written to output files. The browser session is closed when the run ends.
- The utility waits between schools, uses modest waits while paging, and stops if the portal returns HTTP 429. Do not lower the delay to evade portal protections.
- If the portal changes its login form or table controls, the run will report errors/warnings rather than attempting to defeat access controls.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests cover table normalization (headers, spans, key/value rows, duplicate snapshots) and safe Excel/CSV export. They do not sign in to or exercise the live portal. The first live run should be checked against the portal's displayed table and pagination counts.
