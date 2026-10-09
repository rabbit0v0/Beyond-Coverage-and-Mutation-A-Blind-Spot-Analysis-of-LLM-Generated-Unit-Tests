# Publication Checklist

This repository is intended to publish the reproducible research artifact for
the LLM-generated unit-test bias study. Use this checklist before pushing to a
public GitHub repository.

## Include in Git

- Source code for the harness, analysis, paper-relevant failure plotting, and benchmark construction.
- Prompt templates under `prompts/`.
- Tests under `tests/`.
- Current evaluation, result-schema, mutation-operator, and reproduction guides under `docs/`.
- Benchmark manifests and scripts needed to recreate benchmark tasks.
- Compact final treatment/control profiles and their provenance inventory under
  `analysis/final-mixed-runs/release/`.
- A `README.md` with setup, reproduction, data availability, and citation notes.

## Keep Out of Git

- Virtual environments, bytecode, caches, local package stores, and `.DS_Store`.
- `pilot-workdir/` generated execution workspaces.
- Full raw `pilot-results/` JSONL outputs unless deliberately curated.
- Backup files such as `*.bak`, `*.backup-*`, and scrub backups.
- Logs and temporary files.
- Generated summaries, comparison tables, older analysis versions, and duplicate
  model-only result exports. These can be regenerated from the final profiles.
- Local manuscript sources under `documentation/` and retired exploratory
  documentation and adequacy figures in the ignored archive.
- Bundled third-party tool checkouts or datasets unless their license and
  provenance explicitly allow redistribution.
- Any file containing API keys, credentials, private tokens, or machine-local
  absolute paths.

## Archive Separately

Large raw outputs should be archived in a data repository such as Zenodo, OSF,
Dataverse, or as GitHub Release assets. The GitHub repo should then point to
that archive from the README and cite the DOI once available.

Archive candidates:

- Full raw JSONL model outputs.
- Full generated test workspaces, if needed for auditability.
- Large benchmark task bundles.
- Review packets or generated HTML/ZIP files that are not needed for normal
  source-level reproduction.

## Before Publishing

The public workflow now includes the Java example, existing-test evaluation
entry point, Java schema, RQ reproduction guide, CI, current manifests, and
benchmark ZIP/inventory packaging. Superseded benchmarks and reports are kept
only in the ignored local `archive/pre-publication/` for recovery.

The research artifact release includes benchmark and evaluated-test ZIPs,
aligned assertion-mutation evidence, checksums and collected upstream notices.
Ten tasks have production source withheld; IDs and historical results are
retained. See `SOURCE_DISTRIBUTION.md` and `DATA_AVAILABILITY.md`.
Remaining citation metadata: authors/title/DOI. The reproduction guide maps
current paper results using stable LaTeX table/figure labels.

1. Run `python scripts/audit_public_release.py`.
2. Scrub or remove any reported local absolute paths.
3. Confirm third-party benchmark and tool licenses.
4. Confirm that MIT covers only original project material and retain required upstream notices for third-party source.
5. Add `CITATION.cff` with the final title, authors, and DOI once known.
6. Stage only intentional files with `git add`.
7. Re-run the audit on staged files before pushing.
