# Reproduce the Study

## Artifact Status

The code and example are runnable. Compact final numeric profiles are included
under `analysis/final-mixed-runs/release/`. They regenerate numeric reports and
comparisons without the original workdirs. Treatment contains four models across
four strategies (14,320 rows); control contains GPT-5.4 and Gemma across four
strategies (5,600 rows). Generated summaries are not committed.

Independently rerunning evaluation requires Java v2 tasks and archived run/evidence
bundles. See [data availability](../DATA_AVAILABILITY.md) for bundle filenames,
checksums and upstream licensing requirements. All benchmark task sources and
available historical evaluated-test fixtures are included. Historical missing
files and failed generations remain identified in the evaluation records.
The compact profiles preserve recorded scores, not the full source and mutation
evidence required to verify those scores independently. The mutation bundles
contain the separate aligned-zero assertion rerun, not historical PIT or
extreme-condition/CFA evidence. They do not replace the historical compact
profiles or silently update the paper's scores.

Final analysis uses the `pit-mutation-v11c-object-observability-broad` lineage.
The assertion collector's internal version is `custom_assertion_mutation_v9`;
the run label and collector version are different identifiers. Preserve both.

## Regenerate Numeric Results

Run from the repository root. Outputs go to the relative paths supplied below:

```bash
python scripts/analyze_java_blindspots.py \
  analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz \
  --saved-profiles --out-dir evaluation-output/paper-treatment
python scripts/analyze_java_blindspots.py \
  analysis/final-mixed-runs/release/control/blindspot_profiles.jsonl.gz \
  --saved-profiles --out-dir evaluation-output/paper-control
python scripts/compare_complexity.py \
  --treatment analysis/final-mixed-runs/release/treatment \
  --control analysis/final-mixed-runs/release/control \
  --out evaluation-output/rq5-comparison.csv
python scripts/compute_funnel_numbers.py
python scripts/plot_failure_analysis.py \
  analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz \
  --out-dir evaluation-output/paper-failures --prefix treatment
```

`--saved-profiles` uses recorded scores and counts without rerunning tests or
recomputing adequacy. The analyzer writes `summary.md` and JSONL/CSV profiles to
each selected output directory. The comparison command writes a CSV, the funnel
command prints counts, and the failure plotter writes distributions and figures.
These derived files can remain local; the committed profiles are their inputs.

## Reanalyze Archived Evidence

Restore source workdirs, coverage artifacts, and rows with resolvable paths.
Use final assertion-enriched rows and custom-control-flow evidence:

```bash
python scripts/analyze_java_blindspots.py restored/treatment/*.jsonl \
  --custom-control-flow-result restored/custom-control-flow/treatment \
  --out-dir evaluation-output/paper-treatment
python scripts/plot_failure_analysis.py restored/treatment/*.jsonl \
  --out-dir evaluation-output/paper-failures --prefix treatment
python scripts/compute_funnel_numbers.py \
  --profiles evaluation-output/paper-treatment/blindspot_profiles.jsonl
```

Repeat separately for control rows. Only executable rows enter adequacy averages;
mutation scores use scorable obligations, not all generated tests. Exclude the
noncanonical `java-v2-mixed-gemma-panta-control-full` run from restored inputs,
retaining the canonical fix-enabled Panta run.

## Paper Result Map

| Result | Required input | Command/output |
| --- | --- | --- |
| RQ1 execution/failures | Final treatment profiles | `plot_failure_analysis.py`: failure distributions |
| RQ2 BVA (`fig:bva-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles`: boundary-value reports and category data |
| RQ3 CFA (`tab:cfa-heatmap`, `fig:cfa-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles`: construct scores and denominators |
| RQ4 Oracle Strength (`fig:os-return-type-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles`: return/exception reports and return-type data |
| RQ5 complexity (`fig:rq5-heatmap`) | Separate treatment/control profiles | `compare_complexity.py`: per-construct comparisons and denominators |
| Eligibility-funnel table | Final treatment profiles | `compute_funnel_numbers.py --profiles ...` |

The combined analyzer is `analyze_java_blindspots.py`. It produces `summary.md`
and JSONL/CSV profiles containing the data needed for the paper's adequacy results.
The exploratory adequacy plotter and its generated figures are not included in
the public archive. Paper figure styling is separate from evaluation.
Paper references use LaTeX labels because numeric figure and table numbers can
change. The old manually transcribed RQ5 renderer is excluded from the public
workflow. Export comparison data from analyzed evidence:

```bash
python scripts/compare_complexity.py \
  --treatment evaluation-output/paper-treatment \
  --control evaluation-output/paper-control \
  --out evaluation-output/rq5-comparison.csv
```

This preserves each boundary category, including separate boolean/bound variants.
CFA uses score sums divided by scorable obligations, retaining partial credit.
Do not average rounded percentages or silently combine categories. The exported
data supports regenerating the paper comparison; exact figure styling is separate.

## Rerun Generation

Restore the task bundle under `benchmarks/java-complexity-v2/tasks/`. Install
requirements, configure JDK 17/Maven, and set model credentials through environment
variables. Start with one task:

```bash
python scripts/run_java_pilot.py \
  --manifest benchmarks/java-complexity-v2/manifests/java_v2_mixed_treatment.jsonl \
  --prompt zero-shot --model MODEL_ID --temperature 0 --limit 1
python scripts/run_java_sota_pipelines.py \
  --manifest benchmarks/java-complexity-v2/manifests/java_v2_mixed_treatment.jsonl \
  --pipeline both --model MODEL_ID --temperature 0 --limit 1
```

Use `--help` for provider configuration and full-run limits. P3 is intention
planning; P5 is mutation feedback. Panta needs a separate external installation
and uses `run_panta_benchmark.py`. Model calls can incur costs and need not reproduce historical
responses even at temperature zero.

Collect PIT, CFA, and assertion evidence with `collect_java_mutation.py`,
`collect_java_extreme_condition_mutation.py`, and `collect_java_assertion_mutation.py`.
Final control collection used defaults (`incidental-score=zero`,
`degenerate-null-score=score`). Archive exact settings per treatment run rather
than assuming every historical run used identical settings.
