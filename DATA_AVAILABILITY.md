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
Generated summaries are not tracked. Inventory hashes identify the profiles and
their matching mutation-evidence bundles. OS uses assertion-mutation evidence
with incidental kills scored zero, while the recorded BVA and CFA values are
preserved. The [inventory](analysis/final-mixed-runs/release/inventory.json)
records collector identifiers and exact evidence-bundle filenames for provenance.

The accompanying artifact distribution provides the following ZIPs and
`SHA256SUMS`. After downloading all five ZIPs
and the checksum file into one directory, run `shasum -a 256 -c SHA256SUMS`.
The Java example runs independently. A DOI has not been assigned.

| Bundle | Required contents | Public location |
| --- | --- | --- |
| Final numeric profiles | Compact final treatment/control analysis and inventory | `analysis/final-mixed-runs/release/` in Git |
| ObligBench | All 1,595 source projects, task records, manifests, provenance and licenses | `ObligBench.zip` |
| Evaluated treatment tests | 14,320 records; generated tests, helpers, available production sources and build files | `evaluated-tests-treatment.zip` |
| Evaluated control tests | 5,600 records; generated tests, helpers, available production sources and build files | `evaluated-tests-control.zip` |
| Treatment assertion evidence | 14,320 mutation records, referenced logs, tests, compact profiles and collector snapshots | Treatment mutation-evidence ZIP identified in the inventory |
| Control assertion evidence | 5,600 mutation records, referenced logs, tests, compact profiles and collector snapshots | Control mutation-evidence ZIP identified in the inventory |

The mutation ZIPs contain the assertion-mutation evidence used for the OS
scores in the tracked profiles. Recognized assertion and verification failures
earn credit, while uncaught non-assertion exceptions and crashes receive zero.
They do not contain PIT or extreme-condition/CFA mutation evidence. The profiles
retain those recorded numeric results, so reports can be regenerated without
repeating collection. Full model responses and human-validation materials are
not included.

MIT covers our original material only. When using the packages, please respect
upstream licenses and retain their notices. See
[source distribution](SOURCE_DISTRIBUTION.md) for Stack v2 licensing references.

## Restore the Artifacts

Extract `ObligBench.zip` into the repository root to restore source projects under
`benchmarks/java-complexity-v2/tasks/`.

Extract the evaluated-test ZIPs into separate directories. Their evaluation
records identify the model, strategy, task and corresponding project files.
Extract the mutation-evidence ZIPs separately and follow their accompanying
README for record paths and verification.

Follow [the reproduction guide](docs/reproduction.md) to regenerate numeric
reports from the profiles included in Git.
