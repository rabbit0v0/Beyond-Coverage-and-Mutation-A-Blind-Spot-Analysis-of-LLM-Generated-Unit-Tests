# Reproduce the Study

## Artifact Status

Use the saved profiles below to regenerate the study's recorded reports. For
evaluating your own tests, follow
[Evaluate Your Own Tests](../README.md#evaluate-your-own-tests) in the README.

To regenerate the study's numeric reports and comparisons, use the compact
profiles under `analysis/final-mixed-runs/release/`. These profiles work without
the original workdirs. Treatment covers four models across four strategies
(14,320 rows), while control covers GPT-5.4 and Gemma across four strategies
(5,600 rows). The profiles are included in Git, so you can generate summaries
locally. The executable example provides a separate way to check your environment.

To rerun evaluation rather than regenerate recorded reports, you also need
ObligBench tasks and archived run artifacts. See
[data availability](../DATA_AVAILABILITY.md) for bundle filenames, checksums and
upstream licensing requirements. The bundles provide benchmark source and
archived evaluated-test projects.

The compact profiles preserve recorded scores, but not the full source and
mutation evidence needed to verify those scores independently. In addition, the
mutation bundles contain a separate aligned-zero assertion rerun rather than
historical PIT or extreme-condition/CFA evidence. Therefore, use the compact
profiles to regenerate the study's recorded tables and treat the aligned rerun
as a separate collection of assertion evidence.

## Regenerate Numeric Results

Run the following commands from the repository root so that outputs are written
to the relative paths shown below.

```bash
python scripts/analyze_java_blindspots.py \
  analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz \
  --saved-profiles --out-dir evaluation-output/paper-treatment
python scripts/analyze_java_blindspots.py \
  analysis/final-mixed-runs/release/control/blindspot_profiles.jsonl.gz \
  --saved-profiles --out-dir evaluation-output/paper-control
```

With `--saved-profiles`, the analyzer uses recorded scores and counts without
rerunning tests or recomputing adequacy. It then writes `summary.md` and JSONL/CSV
profiles to each selected output directory. Because these files are derived from
the committed profiles, they can remain local.

## Additional Paper Analyses

To reproduce specific paper analyses, use the following commands with the saved
study profiles. These are optional additions to the main adequacy reports and
do not rerun tests or mutations.

```bash
python scripts/compare_complexity.py \
  --treatment analysis/final-mixed-runs/release/treatment \
  --control analysis/final-mixed-runs/release/control \
  --out evaluation-output/rq5-comparison.csv
python scripts/compute_funnel_numbers.py
python scripts/plot_failure_analysis.py \
  analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz \
  --out-dir evaluation-output/paper-failures --prefix treatment
```

The comparison command writes treatment/control comparisons for RQ5 to a CSV.
The funnel command prints execution and scoring-eligibility counts for the
paper's funnel table, while the failure plotter writes RQ1 failure distributions
and figures under `evaluation-output/paper-failures/`.

The comparison export preserves each boundary category, including separate
boolean/bound variants. For CFA, it divides score sums by scorable obligations
so that partial credit is retained. To preserve those distinctions, do not
average rounded percentages or combine categories. Exact paper figure styling
remains separate from evaluation.

### Paper Result Map

| Result | Required input | Command/output |
| --- | --- | --- |
| RQ1 execution/failures | Final treatment profiles | `plot_failure_analysis.py` produces failure distributions |
| RQ2 BVA (`fig:bva-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles` produces boundary-value reports and category data |
| RQ3 CFA (`tab:cfa-heatmap`, `fig:cfa-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles` produces construct scores and denominators |
| RQ4 Oracle Strength (`fig:os-return-type-heatmap`) | Final treatment profiles | Analyzer with `--saved-profiles` produces return/exception reports and return-type data |
| RQ5 complexity (`fig:rq5-heatmap`) | Separate treatment/control profiles | `compare_complexity.py` produces per-construct comparisons and denominators |
| Eligibility-funnel table | Final treatment profiles | `compute_funnel_numbers.py --profiles ...` |

The combined analyzer, `analyze_java_blindspots.py`, produces `summary.md` and
JSONL/CSV profiles containing the data needed for the paper's adequacy results.
However, figure styling is separate from evaluation. The table above uses LaTeX
labels to identify paper results because numeric figure and table numbers can
change.
