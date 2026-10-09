# Source Distribution

ObligBench's study population remains 1,595 tasks: 895 treatment and 700 control.
The public benchmark ZIP includes
production source for 1,585 tasks: 890 treatment and 695 control. Source for five
tasks in each group is excluded from distribution under the release's
source-distribution policy. Their task IDs, provenance and historical results
remain included. These are distribution exclusions, not exclusions from the
experiments. This is a conservative policy for public redistribution of adapted
source, not a finding that the study's research use was prohibited. The reasons
for withholding particular source files are listed below.

The full manifests, task IDs, provenance, evaluation records, and published
numeric profiles remain intact. Generated tests are retained where they do not
embed the withheld production implementation. Evaluated-test records identify unavailable
production source through `production_source_distribution` and
`source_exclusion_reason`; original source hashes and recorded outcomes remain
unchanged. These test projects cannot be rerun without obtaining the missing
production source under its applicable terms.

## Withheld Source

Task IDs, reasons and evidence links are listed in
[`source-exclusions.json`](benchmarks/java-complexity-v2/source-exclusions.json).

| Source | Tasks | Reason for withholding source from public packages |
| --- | ---: | --- |
| `GaboHub/fermat` | 1 | The original terms restrict use of modified source to the Fermat Framework; the benchmark uses an adapted standalone fixture. |
| `Hellohi3654/React` | 1 | The original source carries custom revenue and other restrictions that conflict with the Stack v2 WTFPL label. |
| `zhaoxianjin/reader` | 1 | Research-only and noncommercial wording accompanies Apache-2.0; redistribution terms for adapted fixtures are ambiguous. |
| `arindam7development/CertWare` | 3 | The original NOSA-1.3 license was identified, but the historical modification dates and contributor declarations required for these adapted fixtures have not been established. |
| `Tifancy/goja` | 1 | The historical fork is unavailable, and the applicable terms for its legacy JXLS code could not be established from the available evidence. |
| `S2-group/mobilesoft-2020-iam-replication-package` | 3 | These files contain decompiled third-party app or SDK code; the applicable original redistribution licenses were not identified. |

Stack v2 provides source provenance and detected license labels, rather than a
single license covering all extracted code. Its labels are used where no
conflicting evidence is found. When original file or module terms conflict with
dataset labels, those original terms take
precedence. Our original material is licensed under MIT; bundled upstream source
retains its applicable licenses and required notices. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Randoop, Kickstarter and linked Jyroscope notices have
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

The same policy applies to production-code copies or excerpts in accompanying
artifact packages, including mutation evidence. Task IDs and numerical outcomes
are retained without redistributing the withheld implementation.
