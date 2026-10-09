# Beyond Coverage and Mutation

## Overview

Research code for evaluating behavioral blind spots in generated Java unit tests.
The study uses ObligBench: 895 treatment functions and 700 easy control functions.
The evaluation reports compilation and execution outcomes and classifies failed
runs by failure type and stage. For tests that execute successfully, it measures
Boundary-Value Adequacy (BVA), Control-Flow Adequacy (CFA), and Oracle Strength (OS).

These scores indicate which boundary inputs the tests exercise and which changes
to control flow, return values, or exceptions they detect. A high score does not
guarantee that the tests find every bug or that the production code is correct.

## ObligBench Benchmark

ObligBench contains 1,595 Java method-level tasks extracted from Stack v2 and
packaged as isolated Maven projects with the supporting code needed to run each
focal method. The current Java v2 release has two non-overlapping groups:

| Group | Tasks | Purpose |
| --- | ---: | --- |
| Treatment | 895 | Higher-complexity methods used to study behavioral blind spots |
| Control | 700 | Easy methods used as the comparison group |
| Total | 1,595 | Combined treatment and control release |

The study and full manifests include all 1,595 tasks. The public source bundle
contains 1,585 task projects; source for five treatment and five control tasks is
withheld because redistribution terms or required notices remain unresolved.
Their task IDs, provenance, generated-test records and historical scores remain
in the published study record. These are distribution exclusions, not changes
to the experimental population or score denominators. See
[source distribution](SOURCE_DISTRIBUTION.md) for reasons, task IDs and rerun limitations.

Each manifest records the target method's signature, relative project directory,
source provenance and license, complexity measures, and group membership:

- [Treatment manifest](benchmarks/java-complexity-v2/manifests/java_v2_mixed_treatment.jsonl)
- [Control manifest](benchmarks/java-complexity-v2/manifests/java_v2_mixed_control.jsonl)
- [Combined manifest](benchmarks/java-complexity-v2/manifests/java_v2_mixed.jsonl): the union of those two groups, not a separate benchmark version.

Only this release's manifests and benchmark notes are included in Git. Task
projects are distributed separately and must be restored under
`benchmarks/java-complexity-v2/tasks/` for benchmark runs. Download `ObligBench.zip`
and `SHA256SUMS` from the [research artifact release](https://github.com/rabbit0v0/Beyond-Coverage-and-Mutation-A-Blind-Spot-Analysis-of-LLM-Generated-Unit-Tests/releases/tag/v1.0.0-obligbench);
see [data availability](DATA_AVAILABILITY.md)
and [benchmark notes](benchmarks/java-complexity-v2/README.md).
The quickstart below uses the included example and requires no benchmark download.

## Quick Start: Evaluate Existing Tests

This walkthrough evaluates a small Java example included in the repository.
Its purpose is to check that your environment works and show what an evaluation
report looks like before you supply your own code and tests. It uses existing
tests and makes no LLM calls, so no model API key is required.

The example's `Subject.clamp(int value)` method returns zero for a negative
integer and preserves a nonnegative integer. `SubjectTest` checks these cases:

| Input | Expected return value |
| --- | --- |
| `-1` | `0` |
| `0` | `0` |
| `2` | `2` |

### 1. Prepare Your Environment

Use Python 3.12+, JDK 17, and Maven 3.9+. Set `JAVA_HOME` to your JDK installation
and put its `bin` directory and Maven on `PATH`. Run the following commands from
the repository root. The first three create and activate an isolated Python
environment and install the evaluator's dependencies. The last two print the
Java and Maven versions so you can check that both tools are available.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
java -version
mvn -version
```

### 2. Run the Basic Evaluation (Optional)

This command evaluates the supplied tests for the example's `clamp` method:

```bash
python scripts/evaluate_tests.py \
  --project examples/java-tests \
  --signature 'public static int clamp(int value)' \
  --test-class benchmark.SubjectTest \
  --out-dir evaluation-output/example
```

The arguments select:

| Argument | Meaning in this example |
| --- | --- |
| `--project` | The Maven project containing production code and tests |
| `--signature` | The production method whose adequacy is scored: `Subject.clamp(int value)` |
| `--test-class` | The JUnit class to execute: `benchmark.SubjectTest` |
| `--out-dir` | A new directory for the project copy, reports, and logs |

The trailing `\` characters continue the shell command onto the next line.

The script copies the project, compiles its production and test code, and runs
the selected JUnit test class. If tests execute successfully, it analyzes the
inputs used to exercise the target method and reports Boundary-Value Adequacy.
Your original project is left unchanged. Compilation or execution failures
produce logs and a nonzero command exit status.

The report also includes an **Execution Funnel** counting compilation and
execution successes, and **Failure Classification** grouping unsuccessful runs
by failure type and stage using the recorded logs. For this passing example,
expect one compilation success, one execution success, and zero failed runs.
Failed runs are excluded from adequacy averages, not scored as zero.

On success, the terminal prints the location of `summary.md`. Open
`evaluation-output/example/metrics.json` first for the concise score report.
For this example you should see:

| Metric | Basic evaluation result |
| --- | --- |
| Boundary-Value Adequacy | `scored`, with score `1.0` |
| Control-Flow Adequacy | `not_collected`, with score `null` |
| Oracle Strength | `not_collected`, with score `null` |

The last two require mutation evidence, which the basic command does not
collect. `null` here means that a score is unavailable, not that the test failed.

The following files are produced by `scripts/evaluate_tests.py` inside the
directory selected by `--out-dir`. Relative paths are resolved from your shell's
current working directory, not from the script's directory. Since these commands
run from the repository root, the basic example writes to
`<repository-root>/evaluation-output/example/`; the mutation example below
writes to `<repository-root>/evaluation-output/example-mutation/`.
You can also supply an absolute output path.

| Output file | What it tells you |
| --- | --- |
| `metrics.json` | Public scores and whether each metric was collected or applicable |
| `results.jsonl` | Execution results; `compile_passed` and `execution_passed` should both be `true` |
| `summary.md` | Descriptive counts, scores, and breakdown tables |
| `profile.json` | Item-level analysis evidence and diagnostic fields |
| `compile.txt`, `execute.txt` | Tool output for investigating failures |
| `evaluation.json` | Input/code hashes and evaluation settings |

The evaluated project copy is stored in `workdir/` under the same output
directory. `execute.txt` is written only if compilation succeeds and execution
is attempted. Invalid arguments or interrupted runs may leave no outputs or
incomplete outputs.

### 3. Run Extreme-Condition and Assertion Mutation

With `--mutation`, the evaluator runs our two mutation methods:

- **Extreme-condition mutation** forces conditions to true or false and alters
  other control-flow constructs to measure Control-Flow Adequacy (CFA).
- **Assertion mutation** changes return values or exception types to measure
  whether the tests detect those changes, yielding Oracle Strength (OS).

Both methods rerun the selected tests against mutated copies of the production
code; your input project remains unchanged. The first command below only
downloads JaCoCo, which supplies execution-coverage evidence. The second command
compiles and runs the example, computes BVA, and collects both mutation types.

```bash
mvn org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get \
  -Dartifact=org.jacoco:org.jacoco.agent:0.8.14:jar:runtime
python scripts/evaluate_tests.py \
  --project examples/java-tests \
  --signature 'public static int clamp(int value)' \
  --test-class benchmark.SubjectTest \
  --mutation --out-dir evaluation-output/example-mutation
```

This is a complete evaluation, not an add-on to the basic run. Execution and
failure counts, BVA, CFA, and OS are reported together in
`evaluation-output/example-mutation/summary.md`, with all three scores in
`metrics.json` in that same directory. You can run this command directly without
running step 2 first.

The different directory name keeps the optional basic run from step 2 intact.
The evaluator requires an empty output directory to avoid overwriting an existing
run; it does not require separate directories for different metrics. If you skip
step 2, you can use `--out-dir evaluation-output/example` here instead, provided
that directory is empty or does not yet exist.

For this example, `evaluation-output/example-mutation/metrics.json` should report
BVA, CFA, and Oracle Strength scores of `1.0`.
These results describe this small example, not a guarantee of test correctness.
See [the example notes](examples/java-tests/README.md) for more detail.

## Evaluate Your Own Tests

Our evaluation is **method-based**, matching the study's experimental design.
Each ObligBench task has one designated production method, extracted from a
Stack v2 Java source file and packaged with the supporting code needed to run it.
Adequacy is measured against that method's inputs and behavioral obligations.

Each run evaluates the selected test class against one production method,
identified by `--signature`.

When evaluating your own tests, supply the target method's declaration through
`--signature`, for example `--signature 'public static int clamp(int value)'`.
Include the method's modifiers, return type, name, and parameters, without its
body. The signature tells the analyzer which production method to evaluate.
`--test-class` separately identifies the JUnit class whose tests should run.

For benchmark runs, each task's signature and project location are already stored
in the manifest. The benchmark runners read them automatically, so you do not
need to enter a signature manually for every task.

The supported input is an isolated Maven project containing
`src/main/java/benchmark/Subject.java` and a selected JUnit test class. Arbitrary
repository layouts and multi-module projects require adaptation. Read
[evaluating your own tests](docs/evaluating_tests.md) for the contract and limitations.

The current version does not support overloaded target methods. Use a fixture
containing only one method with the target name. Support for selecting overloaded
methods by their full signature is planned for a future version.

## Analyze Recorded Results

The separate analyzer reads existing result rows and produces reports; it does
not compile tests or collect new mutants:

```bash
python scripts/analyze_java_blindspots.py \
  evaluation-output/example-mutation/results.jsonl \
  --out-dir evaluation-output/example-analysis
```

This command writes only these analysis outputs under
`<repository-root>/evaluation-output/example-analysis/`:

| Output file | Contents |
| --- | --- |
| `summary.md` | Descriptive counts, scores, and breakdown tables |
| `blindspot_profiles.jsonl` | One derived analysis profile per input row |
| `blindspot_profiles.csv` | Tabular export of the derived profiles |

Relative input and output paths use your current working directory.
Without `--out-dir`, the analyzer writes to
`analysis/java_blindspots/`; it can overwrite previous analysis outputs there.
Keep the referenced `workdir/` directories available when reanalyzing results.

## Reproduce the Study

The [study results](analysis/final-mixed-runs/release/README.md) are
included in Git as compressed profiles: 14,320 treatment rows (4 models x 4
strategies x 895 tasks) and 5,600 control rows (GPT-5.4 and Gemma x 4 strategies x
700 tasks).

Regenerate the treatment summary directly from the saved profiles:

```bash
python scripts/analyze_java_blindspots.py \
  analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz \
  --saved-profiles --out-dir evaluation-output/paper-treatment
```

This regenerates numeric reports from recorded counts and scores without model
calls, source workdirs, or mutation execution. Independently rerunning the original
evaluation also requires the benchmark source, generated tests, and mutation
evidence; see [data availability](DATA_AVAILABILITY.md) for their availability.
[The reproduction guide](docs/reproduction.md) includes
control, comparison, and funnel commands and explains rerunning the evaluation.
Run the regression suite with:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

Exact historical model outputs are reproduced from archived responses, not by
assuming repeated model calls return identical tests.

## Repository Map

| Path | Purpose |
| --- | --- |
| `scripts/evaluate_tests.py` | Entry point for existing tests |
| `scripts/analyze_java_blindspots.py` | Current combined adequacy analyzer |
| `scripts/collect_java_*mutation.py` | PIT and custom mutation collectors |
| `scripts/run_java_pilot.py`, `scripts/run_java_sota_pipelines.py` | Test generation |
| `scripts/run_panta_benchmark.py` | External Panta baseline adapter |
| `prompts/` | Generation and repair prompt templates |
| `examples/java-tests/` | Self-contained executable example |
| `tests/` | Analyzer and evaluation regression tests |
| `benchmarks/java-complexity-v2/` | Current benchmark manifests and availability notes |
| `analysis/final-mixed-runs/release/` | Compact final treatment/control profiles and provenance |
| `docs/` | Evaluation, schemas, and reproduction instructions |

Legacy Java v1 and TypeScript benchmarks and superseded standalone scorers have
been removed from the public workflow. Exploratory wrappers, one-off repair
scripts, and older diagnostic reports are also excluded. Only Java v2 is supported here.

## Data Availability, Citation, and License

[Data availability](DATA_AVAILABILITY.md) describes the benchmark and evidence
bundles and reproduction limits. The [research artifact release](https://github.com/rabbit0v0/Beyond-Coverage-and-Mutation-A-Blind-Spot-Analysis-of-LLM-Generated-Unit-Tests/releases/tag/v1.0.0-obligbench)
provides the benchmark, evaluated tests, aligned assertion-mutation evidence,
licensing notices and checksums. Historical PIT and extreme-condition/CFA
evidence are not included in these mutation ZIPs. The example runs independently.

Original project material is licensed under [MIT](LICENSE). Third-party
benchmark sources and other third-party content retain their upstream terms;
see [third-party notices](THIRD_PARTY_NOTICES.md). The final paper citation has
not yet been assigned. Release preparation is
tracked in [PUBLICATION_CHECKLIST.md](PUBLICATION_CHECKLIST.md).
