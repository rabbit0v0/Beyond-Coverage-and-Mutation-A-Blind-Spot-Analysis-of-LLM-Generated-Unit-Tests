# Source Distribution

ObligBench's study population remains 1,595 tasks: 895 treatment and 700 control.
To respect upstream licensing conditions, the public benchmark ZIP includes
production source for 1,585 tasks: 890 treatment and 695 control. Source for five
tasks in each group is excluded from distribution under the release's
source-distribution policy. Their task IDs, provenance and historical results
remain included. These are distribution exclusions, not exclusions from the
experiments. Task-specific licensing reasons are listed below.

The full manifests, task IDs, provenance, evaluation records, and published
numeric profiles remain intact. Historical scores and their denominators have
not been recalculated. Generated tests are retained where they do not embed the
withheld production implementation. Evaluated-test records identify unavailable
production source through `production_source_distribution` and
`source_exclusion_reason`; original source hashes and recorded outcomes remain
unchanged. These test projects cannot be rerun without obtaining the missing
production source under its applicable terms.

## Withheld Source

Task IDs, reasons and evidence links are listed in
[`source-exclusions.json`](benchmarks/java-complexity-v2/source-exclusions.json).

| Source | Tasks | Reason |
| --- | ---: | --- |
| `GaboHub/fermat` | 1 | Modified source is restricted to use within the Fermat Framework. |
| `Hellohi3654/React` | 1 | Original source carries custom revenue and other restrictions rather than the dataset WTFPL terms. |
| `zhaoxianjin/reader` | 1 | Apache wording is combined with study-only and noncommercial restrictions. |
| `arindam7development/CertWare` | 3 | NOSA-1.3 requires historical modification dates and contributor declarations that have not been established. |
| `Tifancy/goja` | 1 | The historical fork is unavailable; legacy JXLS terms could not be reconciled with the dataset labels. |
| `S2-group/mobilesoft-2020-iam-replication-package` | 3 | The repository MIT license does not establish rights to its decompiled third-party application code. |

Stack v2 labels are used where no conflicting evidence is found. When original
file or module terms conflict with dataset labels, those original terms take
precedence. The missing Randoop, Kickstarter and linked Jyroscope notices have
been collected. Open Source Physics retains its file-level GPL notice, including
the notice at the end of the original file. Those resolved tasks are distributed.

## Reproduction

All released numeric profiles can still be analyzed for the complete study.
Fresh execution using only the distributed source covers a subset, not a full
rerun of all 1,595 tasks. Do not interpret absent source as a test failure or
silently replace historical scores with subset results. A complete execution
rerun requires independently obtaining authorized source for the withheld tasks.

Removing these files from public packages does not delete the historical local
fixtures or change the experiments. The exclusion register applies only to
source distribution, not to analysis of the recorded results.

The same exclusions apply to production-code copies or excerpts in future
mutation-evidence and raw-response packages. Preserve task IDs and numerical
outcomes without redistributing the withheld implementation.
