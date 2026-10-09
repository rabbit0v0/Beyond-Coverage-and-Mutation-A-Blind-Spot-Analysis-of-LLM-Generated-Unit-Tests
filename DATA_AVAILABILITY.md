# Data Availability

This repository contains the code, prompts, manifests, documentation, tests, and
selected compact analysis artifacts needed to reproduce the study workflow.

Full raw run outputs and bulky generated workspaces are not intended to be
stored directly in Git. They should be archived separately in a durable research
data repository or attached as release assets, then referenced here with a DOI
or stable URL.

## Public Artifacts

The compact numeric profiles under `analysis/final-mixed-runs/release/` are
included in Git and can regenerate summary tables, eligibility counts, and
treatment/control comparisons. Treatment has four models and four strategies
(14,320 rows); control has GPT-5.4 and Gemma with four strategies (5,600 rows).
Generated summaries are not tracked. Inventory hashes identify the source
profiles and released files.

The accompanying artifact distribution provides the following ZIPs and
`SHA256SUMS`. After downloading all five ZIPs
and the checksum file into one directory, run `shasum -a 256 -c SHA256SUMS`.
The Java example runs independently. A DOI has not been assigned.

| Bundle | Required contents | Public location |
| --- | --- | --- |
| Final numeric profiles | Compact final treatment/control analysis and inventory | `analysis/final-mixed-runs/release/` in Git |
| ObligBench Java v2 | 1,585 source projects; all 1,595 task records, manifests, provenance and licenses | `ObligBench.zip` |
| Evaluated treatment tests | 14,320 records; generated tests, helpers, available production sources and build files | `evaluated-tests-treatment.zip` |
| Evaluated control tests | 5,600 records; generated tests, helpers, available production sources and build files | `evaluated-tests-control.zip` |
| Aligned treatment assertion evidence | 14,320 mutation records, referenced logs, tests and collector snapshots | `mutation-evidence-treatment-aligned-zero.zip` |
| Aligned control assertion evidence | 5,600 mutation records, referenced logs, tests and collector snapshots | `mutation-evidence-control-aligned-zero.zip` |

The mutation ZIPs contain a separate aligned assertion-mutation rerun using
`custom_assertion_mutation_v9`, with incidental kills scored zero. They do not
replace the historical compact profiles in Git or contain historical PIT or
extreme-condition/CFA evidence. The archived profiles support numerical
reanalysis; these packages alone do not reproduce every historical collection
step. Full model responses and human-validation materials are not published in
this release.

To respect upstream licensing conditions, production source for ten tasks is
excluded from distribution, five per cohort; all task IDs and historical outcomes
remain included. The
same exclusions apply to production excerpts in mutation records. Read
[source distribution](SOURCE_DISTRIBUTION.md) and the per-task exclusion registry
for reasons and the limits on fresh execution. MIT covers our original
material; upstream material retains its applicable licenses and notices.

Prepare the current benchmark bundle locally with:

```bash
python scripts/package_benchmark_release.py \
  --notices-dir /path/to/extracted-release/third-party-notices \
  --out /tmp/obligbench-java-v2.zip
```

The adjacent inventory records file SHA-256 hashes and the archive checksum.
The packager automatically applies `benchmarks/java-complexity-v2/source-exclusions.json`
to source files while retaining the full manifests and task metadata. Supply the
consolidated notices directory with `--notices-dir` when preparing release ZIPs.
See [source distribution](SOURCE_DISTRIBUTION.md) for the ten withheld tasks,
their reasons, and the distinction between complete historical analysis and a
fresh execution rerun of only the distributed subset.
The notices directory is required and can be reused from an extracted release.
Retain its upstream license texts and attribution when redistributing the bundle.
Verify the published archive checksum before extraction into the repository root.
Historical Java v1 and TypeScript benchmarks are excluded.

- `code`: this GitHub repository.
- `raw-results`: full JSONL model and evaluation outputs.
- `generated-workdirs`: optional generated test workspaces for auditability.
- `benchmark-bundles`: optional benchmark task bundles, subject to upstream
  license permissions.
- `validation-evidence`: manual-validation sheets and codebooks.

## Local Files Intentionally Excluded

- `pilot-results/`
- `pilot-workdir/`
- Older and intermediate analysis results, generated summaries, and duplicate exports
- `external-tools/`
- `.venv/`, `.cache/`, `.pnpm-store/`, and Python bytecode caches
- `.DS_Store`, logs, backups, and temporary files

Only the compact final profiles and their inventory are committed. Regenerate
summaries and comparison CSVs with the commands in [the reproduction guide](docs/reproduction.md) rather
than publishing redundant generated reports.
