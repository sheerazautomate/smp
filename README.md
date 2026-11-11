# SMP School Table Extractor

A local command-line utility for exporting rendered HTML tables from the SMP school-detail pages. Its configured scope is exactly the six EMIS codes supplied for this task; `--codes` can select a subset for a first pass but rejects codes outside that set. It signs in once, processes each `/school-detail/{EMIS}` page in the same browser session, and writes a timestamped export bundle on the machine running the tool.

## What it captures

- HTML `<table>` elements in the detail page, including tables rendered in hidden tab panels.
- Local tab/pill controls *and* tab-like button pairs (the portal's Milk/Biscuit
  switch is a plain `<button id="tab-milk">`, not a declared tab), plus supported
  collapsed sections that can reveal additional tables. Each view is loaded
  completely when it is entered, so a table behind a tab is never left at page 1.
- Common pagination controls inside pagination/pager containers, with a configurable safety limit.
- Table captions/nearby headings, headers, rows, source URLs, and capture contexts for auditing.
- Header `rowspan`/`colspan` layout and key/value tables without a `<thead>`.

Pagination is read from each table's own pager and its "Showing 1 to 5 of 18
entries" line. The tool first asks DataTables to show **all** rows through its
length selector; if that is unavailable it walks the numbered pages. Every table
records how many rows the portal reported, so a short capture cannot pass as
complete.

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
--max-pages N       Maximum page advances per table (default: 100)
--headless          Hide the Chromium window
--non-interactive   Read SMP_USERNAME and SMP_PASSWORD from the environment; never prompt
```

For an unattended run, set `SMP_USERNAME` and `SMP_PASSWORD` in the process environment, then use `--non-interactive`. Do not put credentials in source code, command-line arguments, `.env` files, or an export; set environment variables through your shell or an approved secret manager. Interactive prompts are preferred on a personal workstation.

## Output

Each run creates a timestamped folder under `exports/` containing:

- `smp_tables.xlsx` — a `Summary` worksheet and one worksheet per captured table.
- `csv/` — one CSV file per table, encoded as UTF-8 with BOM for spreadsheet compatibility.
- `run_manifest.json` — requested codes, status, row/table counts, headers, source URLs, capture contexts, and warnings; it does not contain table values or credentials. Credential-like URL query parameters are redacted in provenance metadata.

Each table also records completeness: `rows captured`, `rows expected (portal)`,
`rows missing`, and `pages captured / expected`. A school whose tables did not
reach the portal's own row totals is reported with status **`incomplete`** rather
than `ok`, prints a warning, and makes the CLI exit non-zero — so an incomplete
export is never mistaken for a finished one. The same fields appear in
`run_manifest.json` (`rows_expected`, `rows_missing`, `complete`,
`pages_captured`, `pages_expected`, `pager_kind`).

The workbook writes portal values as explicit text cells, preserving leading zeros and preventing values from being interpreted as Excel formulas. CSV has no cell types, so formula-like values are prefixed with an apostrophe to reduce spreadsheet formula-injection risk; use the workbook when exact displayed values are required. No HTML snapshots, screenshots, or browser storage state are saved.

## Run it in GitHub Actions

The repository ships a worker workflow, `.github/workflows/smp-extract.yml`, so the
extraction can run on GitHub and keep the data in the repository itself — no local
Python or Chromium install needed.

### One-time setup

1. Add repository secrets **`SMP_USERNAME`** and **`SMP_PASSWORD`** under
   *Settings → Secrets and variables → Actions*. The workflow refuses to run without them.
2. Make sure Actions is allowed to push: *Settings → Actions → General → Workflow
   permissions* must allow **Read and write permissions** (the workflow also declares
   `permissions: contents: write`).
3. If the branch is protected, allow the `github-actions[bot]` identity to push, or
   run the workflow from an unprotected branch. Without push rights the export still
   uploads as a run artifact, but is not committed.
4. **Keep the repository private.** Committed exports contain real school data.

### Run it

*Actions → SMP table extract → Run workflow*. Optional inputs:

| Input | Default | Purpose |
| --- | --- | --- |
| `codes` | all six | Space/comma-separated subset of the configured EMIS codes |
| `delay` | `2.0` | Seconds between school pages |
| `max_pages` | `100` | Pagination safety cap per table view |
| `keep_last` | `30` | How many bundles to retain in the repository |
| `publish` | `true` | Commit the bundle back to the branch |

A weekly `schedule` trigger (Mondays 03:00 UTC) is included; edit the `cron` line or
delete the block if you only want on-demand runs. Concurrent runs on the same branch
are serialised by a `concurrency` group, so two runs never race on the commit.

### Caching (set up once, saves most of the run time)

Two caches make repeat runs much cheaper. Both are `actions/cache` entries, so
they are populated by the first run and reused from then on:

| Cache | Path | Key | Saves |
| --- | --- | --- | --- |
| pip downloads and wheels | `~/.cache/pip` | hash of `requirements.txt` + the setup script | ~3 s, and far more on a cold PyPI |
| Chromium build | `~/.cache/ms-playwright` | hash of `requirements.txt` | ~170 MB download, ~15-18 s |

The Chromium key is derived from the Playwright pin, so bumping Playwright
invalidates the cache instead of reusing an incompatible browser. On a cache hit
`scripts/setup_workflow_env.py` skips the download entirely, then **launches
Chromium once** to prove the cached build works — a corrupt cache fails in setup,
not halfway through an extraction. The OS libraries Chromium needs
(`playwright install-deps`) are not part of the browser download, so they are only
installed on a cache miss.

Setup timings are printed at the end of the step and written to the run summary,
so it is obvious whether a run was a hit or a miss. The test workflow
(`checks.yml`) shares the pip cache key and uses `--skip-browser`, since the unit
tests never launch a browser.

```bash
# see what the workflow would do on this machine, without installing anything
python scripts/setup_workflow_env.py --dry-run
```

### What the worker does

1. Restores the pip and Chromium caches, installs whatever is missing, runs the
   unit tests.
2. Runs `python -m smp_extractor --non-interactive --headless --output-dir <runner temp>`.
   The browser always runs headless on a runner; the `headless` input only matters on a
   self-hosted runner with a display.
3. `scripts/publish_exports.py` copies the newest bundle to `data/exports/<run_id>/`,
   appends it to `data/exports/index.json` (status and row/table totals only, no table
   values), and prunes bundles beyond `keep_last`.
4. Commits `data/exports` and pushes to the branch the run started from.
5. Uploads the bundle as a workflow artifact (kept 30 days) on every run, including
   failures, and writes a per-school summary table to the run's summary page.

```text
data/exports/
├── index.json                     # newest-first run index
└── 20261111_090000/               # one bundle per run
    ├── smp_tables.xlsx
    ├── run_manifest.json
    └── csv/...
```

`data/exports` is committed; `exports/` (local CLI output) stays git-ignored.
Failures — missing secrets, an expired account, a changed login form, or HTTP 429 —
fail the job with a clear message and commit nothing.

`.github/workflows/checks.yml` runs the unit tests on every push and pull request.

## Security and operating notes

- Use this only for schools and records you are authorized to access and export under your organization’s policy.
- The export can contain sensitive education data. Keep the output directory access-controlled, do not upload it to unapproved services, and delete it when no longer needed. This applies equally to `data/exports` when the workflow commits bundles into the repository: a public repository would publish the data.
- Credentials are held only in memory for the run and are not written to output files. The browser session is closed when the run ends.
- The utility waits between schools, uses modest waits while paging, and stops if the portal returns HTTP 429. Do not lower the delay to evade portal protections.
- If the portal changes its login form or table controls, the run will report errors/warnings rather than attempting to defeat access controls.

## Tests

```bash
python -m unittest discover -s tests -v
```

Some tests execute the collector's browser JavaScript against
`tests/fixtures/portal_datatable.html` (markup captured from the portal) using
Node and jsdom, which is what keeps the pager logic honest without signing in. If
`node` or jsdom is unavailable those cases skip; everything else still runs.

```bash
# optional: enable the DOM-backed pager tests
npm install jsdom        # or place jsdom in /tmp/jsdom/node_modules
```

The tests cover table normalization (headers, spans, key/value rows, duplicate snapshots), safe Excel/CSV export, CLI code-scope validation, and the export publisher used by the GitHub Actions worker (staging, index, pruning). A further set of static checks loads the workflow files and rejects expressions GitHub cannot evaluate where they are placed (for example `runner.*` inside a job's `env:`, which otherwise makes the whole workflow file invalid). They do not sign in to or exercise the live portal. The first live run should be checked against the portal's displayed table and pagination counts.
