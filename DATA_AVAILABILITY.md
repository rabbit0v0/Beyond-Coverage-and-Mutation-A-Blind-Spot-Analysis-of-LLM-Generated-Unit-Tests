# Data Availability

This repository contains the code, prompts, manifests, documentation, tests, and
selected compact analysis artifacts needed to reproduce the study workflow.

Benchmark source, evaluated-test projects and mutation evidence are distributed
as release ZIPs rather than stored directly in Git.

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
| ObligBench Java v2 | All 1,595 source projects, task records, manifests, provenance and licenses | `ObligBench.zip` |
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

The packages include the complete benchmark source and available historical
evaluated-test fixtures, along with unredacted production excerpts in mutation
records. Historical missing files and failed generations remain recorded as such;
they are not replaced with new implementations or passing tests. MIT covers our
original material only. Please respect upstream licenses and retain their notices;
see [source distribution](SOURCE_DISTRIBUTION.md) for Stack v2 licensing references.

## Restore the Artifacts

Extract `ObligBench.zip` into the repository root to restore source projects under
`benchmarks/java-complexity-v2/tasks/`.

Extract the evaluated-test ZIPs into separate directories. Their evaluation
records identify the model, strategy, task and corresponding project files.
Extract the mutation-evidence ZIPs separately and follow their accompanying
README for record paths and verification.

Follow [the reproduction guide](docs/reproduction.md) to regenerate numeric
reports from the profiles included in Git.
