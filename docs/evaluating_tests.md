# Evaluate Your Own Java Tests

## Supported Input

Prepare an isolated, single-module Maven project with:

```text
pom.xml
src/main/java/benchmark/Subject.java
src/test/java/benchmark/YourTest.java
```

The focal class must be `benchmark.Subject`. Supply its complete method signature
and a fully qualified JUnit test class name. JUnit 5 is verified by the example;
collectors invoke Maven Surefire for the selected class. Declare dependencies and
Java compilation settings in the POM. Helper classes can stay in the project.
Arbitrary multi-module repositories are not automatically supported.

The current version does not support overloaded target methods. Use a fixture
containing only one method with the target name. Support for selecting overloaded
methods by their full signature is planned for a future version.

```bash
python scripts/evaluate_tests.py \
  --project /path/to/isolated-project \
  --signature 'public static int clamp(int value)' \
  --test-class benchmark.YourTest \
  --mutation --out-dir evaluation-output/my-tests
```

Choose an empty output directory outside the input project. Compilation and the
selected test class run before scoring. A failed compile or execution returns a
nonzero exit status and retains logs. `--timeout` limits each subprocess, not
the entire mutation run. Mutation collection modifies and restores copied source
through the existing collectors. No model calls are made.

Exit codes are 0 for a completed evaluation, 1 for compilation/execution failure,
and 2 when requested mutation collection does not complete. A successful Maven
process without executed tests is rejected using Surefire XML reports.

## Outputs and Interpretation

These outputs belong to `scripts/evaluate_tests.py` and are written under
`--out-dir`. Relative paths use the working directory where you run the command;
for the example above, running from the repository root creates
`evaluation-output/my-tests/` there. Absolute output paths are also supported.

| Output | Contents |
| --- | --- |
| `results.jsonl` | Compilation/execution flags and mutation evidence |
| `profile.json` | Per-obligation adequacy profile and evidence |
| `metrics.json` | Public metric scores and explicit availability statuses |
| `summary.md` | Descriptive counts, scores, and breakdown tables for the focal method/test class |
| `evaluation.json` | Input hashes, signature, test class, and evaluation mode |
| `compile.txt`, `execute.txt` | Maven diagnostics |
| `workdir/` | Evaluated project copy and generated artifacts |

`execute.txt` is created only when execution is attempted after successful
compilation. Invalid arguments or interrupted runs may not produce all outputs.
The separate `scripts/analyze_java_blindspots.py` command reads saved results and
writes only `summary.md`, `blindspot_profiles.jsonl`, and `blindspot_profiles.csv`
under its own `--out-dir` (default: `analysis/java_blindspots/`, relative to the
current working directory). It does not rerun Maven or mutation collection.

Basic mode computes static boundary evidence after verifying tests. `--mutation`
collects custom CFA and assertion evidence, with JaCoCo reachability checks.
PIT is a separate study pipeline, not required by this entry point. Maven may
download dependencies on first use.

Before the first mutation run, fetch the JaCoCo 0.8.14 runtime agent:

```bash
mvn org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get \
  -Dartifact=org.jacoco:org.jacoco.agent:0.8.14:jar:runtime
```

The default location is Maven's standard user repository. Set `JACOCO_AGENT` to
the agent JAR when using another Maven repository location. `JAVA_BIN` can
override the Java executable; otherwise `JAVA_HOME` and `PATH` are used.

Only executable tests are eligible for adequacy interpretation. Missing evidence,
tooling failures, private targets, and inapplicable constructs are not uncovered
obligations. Use item statuses and scorable denominators. Oracle Strength uses
custom assertion evidence; static oracle fields in the profile are
diagnostics, not the paper's headline score.

The summary contains seven sections: Execution Funnel, Failure Classification,
Boundary-Value Adequacy Details, Extreme Mutation Funnel, Control-Flow Adequacy
Details, Assertion Mutation Funnel, and Oracle Strength Details. It contains
descriptive counts and score tables; method explanations are kept in these guides.

Analysis uses Tree-sitter when its Java parser is available, with a pattern-based
fallback. Parser availability can change detection; record the environment when
comparing scores. These approximations do not resolve all Java aliasing,
reflection, helper-method, or framework behavior.

## Future Work

We plan to add a metric for side-effect adequacy. Supporting code is retained
for this future work, and raw evidence and analysis profiles may contain its
experimental fields. The metric is not part of the current reported evaluation
and does not appear in `metrics.json`, summary reports, or plots. Reporting
exclusions do not change mutation collection or the BVA, CFA, and OS scoring logic.
