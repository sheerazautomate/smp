# Published export bundles

Each subdirectory here is one completed run from the `SMP table extract` GitHub
Actions workflow. A bundle contains the same files the CLI writes locally:

```text
data/exports/<run_id>/
├── smp_tables.xlsx      # Summary sheet + one sheet per captured table
├── run_manifest.json    # counts, headers, source URLs, capture contexts, warnings
└── csv/                 # one CSV per captured table
```

`index.json` lists every published run, newest first, with a per-school status
and row/table totals. It never contains table values.

The workflow commits these directories back to the branch it ran from and keeps
the most recent `keep_last` bundles (default 30); older ones are pruned from the
working tree on the next run.

**This directory contains real education data.** If this repository is public or
shared outside your organization, the exports are public too — make the
repository private before enabling the workflow, and review your organization's
data-handling policy.
