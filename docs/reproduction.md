# Reproduce the Study

## Artifact Status

Use the saved profiles below to regenerate the study's recorded reports. If you
want to collect new results instead, follow the test-generation and evaluation
sections later in this guide. Test generation is optional when you use the
published evaluated tests.

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

## Generate New Tests (Optional)

Use this step to ask a model to generate new Java tests for ObligBench methods.
If you want to evaluate the published test suites instead, skip directly to
[Run a Fresh Evaluation](#run-a-fresh-evaluation).

Before generating tests, restore the task bundle under
`benchmarks/java-complexity-v2/tasks/`, install requirements, configure JDK 17 and
Maven, and set model credentials through environment variables. Then start with
one task using either of the following commands. The first uses a zero-shot
prompt, while the second runs the intention-planning and mutation-feedback
pipelines.

```bash
python scripts/run_java_pilot.py \
  --manifest benchmarks/java-complexity-v2/manifests/java_v2_mixed_treatment.jsonl \
  --prompt zero-shot --model MODEL_ID --temperature 0 --limit 1
python scripts/run_java_sota_pipelines.py \
  --manifest benchmarks/java-complexity-v2/manifests/java_v2_mixed_treatment.jsonl \
  --pipeline both --model MODEL_ID --temperature 0 --limit 1
```

These runners generate `src/test/java/benchmark/GeneratedSmokeTest.java` with a
`main` method that throws `AssertionError` when a check fails. They also compile
and execute the generated tests. Project copies are saved under
`pilot-workdir/java/`, while JSONL run records are saved under `pilot-results/`.
Both paths are relative to the repository root.

Once the single-task run works, use `--help` to configure the provider and full-run
limits. P3 corresponds to intention planning, while P5 corresponds to mutation
feedback. For Panta, install the external tool separately and use
`run_panta_benchmark.py`. Keep in mind that model calls can incur costs and may
produce different responses from the historical runs even at temperature zero.

## Run a Fresh Evaluation

For a fresh study run, use the generated projects and JSONL records from the
previous step. The generation runners already record compilation and execution
outcomes. To add mutation evidence, use the study collectors
`collect_java_mutation.py`, `collect_java_extreme_condition_mutation.py`, and
`collect_java_assertion_mutation.py` for PIT, CFA, and assertion evidence,
respectively. Their `--help` output describes the required input records and
output paths. You can also rerun the published evaluated-test projects using
their recorded entry points and the study collection workflow.

For an isolated JUnit project instead, follow
[the evaluation guide](evaluating_tests.md). Its `--mutation` command runs
compilation, test execution, BVA, extreme-condition mutation for CFA, and assertion
mutation for OS together. Because that evaluator requires a JUnit test class,
the study's `main`-based fixtures need adaptation before using it.

Fresh-run reports describe newly collected results rather than regenerate the
historical scores. When interpreting them, remember that only executable rows
enter adequacy averages and mutation scores use scorable obligations rather than
all generated tests.

Final control collection used defaults (`incidental-score=zero`,
`degenerate-null-score=score`). Because historical runs may differ in their
settings, archive the exact settings for each treatment run as well.
