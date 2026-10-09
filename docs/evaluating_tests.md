# Evaluate Your Own Java Tests

## Supported Input

Prepare an isolated, single-module Maven project with the following layout.

```text
pom.xml
src/main/java/benchmark/Subject.java
src/test/java/benchmark/YourTest.java
```

Within this project, the focal class must be `benchmark.Subject`. To select what
to evaluate, supply the target method's complete signature and the fully qualified
name of the JUnit test class. The collectors then use Maven Surefire to run that
class, as shown by the JUnit 5 example. Declare dependencies and Java compilation
settings in the POM, and keep any helper classes in the project. However, arbitrary
multi-module repositories require adaptation before evaluation.

Because the current version does not support overloaded target methods, use a
fixture containing only one method with the target name. Selecting overloaded
methods by their full signature is planned for a future version. Once the project
is ready, run the evaluator with the following command.

```bash
python scripts/evaluate_tests.py \
  --project /path/to/isolated-project \
  --signature 'public static int clamp(int value)' \
  --test-class benchmark.YourTest \
  --mutation --out-dir evaluation-output/my-tests
```

Choose an empty output directory outside the input project so that the evaluated
copy and its results stay separate from your source. The evaluator first compiles
the project and runs the tests in the selected JUnit class. Only after both steps
succeed does it compute adequacy scores. Otherwise, it returns a nonzero exit
status and keeps the logs for diagnosis.

During mutation collection, the existing collectors work on copied source,
leaving your input project unchanged. Although `--timeout` limits each subprocess,
it does not limit the entire mutation run. No model calls are made.

Exit codes are 0 for a completed evaluation, 1 for compilation/execution failure,
and 2 when requested mutation collection does not complete. To ensure that tests
actually ran, the evaluator checks Surefire XML reports and rejects a successful
Maven process with no executed tests.

## Outputs and Interpretation

These outputs belong to `scripts/evaluate_tests.py` and are written under
`--out-dir`. Relative paths use the working directory where you run the command.
For the example above, running from the repository root creates
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

Because test execution follows compilation, `execute.txt` is created only when
compilation succeeds and execution is attempted. Likewise, invalid arguments or
interrupted runs may leave incomplete outputs.

If you want to analyze saved results instead, `scripts/analyze_java_blindspots.py`
reads those results and writes only `summary.md`, `blindspot_profiles.jsonl`,
and `blindspot_profiles.csv`
under its own `--out-dir`. The default is `analysis/java_blindspots/`, relative to
the current working directory. It does not rerun Maven or mutation collection.

In basic mode, the evaluator computes static boundary evidence after verifying
the tests. Adding `--mutation` also collects custom CFA and assertion evidence,
using JaCoCo to check reachability. By contrast, PIT belongs to a separate study
pipeline and is not required by this entry point. Maven may download dependencies
on first use.

Before the first mutation run, fetch the JaCoCo 0.8.14 runtime agent.

```bash
mvn org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get \
  -Dartifact=org.jacoco:org.jacoco.agent:0.8.14:jar:runtime
```

The agent is loaded from Maven's standard user repository by default. If you use
another repository location, set `JACOCO_AGENT` to the agent JAR. Similarly,
`JAVA_BIN` can override the Java executable. Otherwise, `JAVA_HOME` and `PATH`
are used.

When interpreting adequacy, consider only tests that execute successfully.
Missing evidence, tooling failures, private targets, and inapplicable constructs
do not count as uncovered obligations, so consult item statuses and scorable
denominators before interpreting a score. Oracle Strength uses custom assertion
evidence, whereas static oracle fields in the profile serve only as diagnostics
and are not the paper's headline score.

The seven summary sections are Execution Funnel, Failure Classification,
Boundary-Value Adequacy Details, Extreme Mutation Funnel, Control-Flow Adequacy
Details, Assertion Mutation Funnel, and Oracle Strength Details. Together, these
sections provide descriptive counts and score tables, while the guides explain
the evaluation methods.

Analysis uses Tree-sitter when its Java parser is available and otherwise falls
back to pattern-based detection. Because parser availability can change detection,
record the environment when comparing scores. Even with the parser, these
approximations do not resolve all Java aliasing,
reflection, helper-method, or framework behavior.

## Future Work

We plan to add a metric for side-effect adequacy, so supporting code is retained
and raw evidence and analysis profiles may contain its experimental fields.
However, the metric is not part of the current reported evaluation and does not
appear in `metrics.json`, summary reports, or plots. Excluding it from reports
does not change mutation collection or the BVA, CFA, and OS scoring logic.
