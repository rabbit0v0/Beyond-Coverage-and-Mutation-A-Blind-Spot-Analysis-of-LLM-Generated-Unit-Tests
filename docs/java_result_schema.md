# Java Evaluation Result Schema

One JSONL row represents one focal method under one evaluation configuration.
The table covers external evaluations and benchmark runs. Generation adapters
add configuration metadata; separate collectors add optional PIT evidence.

| Field | Meaning |
| --- | --- |
| `run_id`, `task_id` | Stable run/task identifiers |
| `language` | `java` |
| `model_id`, `prompt_template` | Configuration; `external` for supplied tests |
| `task.signature` | Complete focal-method signature |
| `workdir` | Project containing `src/main/java/benchmark/Subject.java` |
| `test_class` | Selected fully qualified JUnit class |
| `test_file` | Selected test source path relative to workdir |
| `extracted_test_code` | Test source used for static evidence |
| `compile_passed`, `execution_passed` | Separate compilation/execution outcomes |
| `error_stage` | `compile` or `execute` for failed external evaluations |
| `coverage` | Optional JaCoCo counters |
| `mutation_score`, `mutation_details` | Optional PIT evidence |
| `extreme_condition_mutation` | Optional custom CFA evidence |
| `assertion_mutation` | Optional custom assertion-mutation evidence, including retained future-work fields |

Resolve archive-relative paths after extraction. JSONL alone is insufficient
when referenced source or coverage artifacts are missing. Failed rows remain
available for failure analysis and are excluded from adequacy averages.

Preserve custom mutation objects in full: they include collector status,
item outcomes, and denominators. Primary analyzed score fields are
`boundary_strength_score`, `if_else_if_condition_avg_item_score`,
and `assertion_strength_score`. For null scores,
consult status and obligation counts to distinguish missing and inapplicable evidence.

CFA reports construct-specific scores. `if_else_if_condition_avg_item_score`
covers condition obligations; the `extreme_condition_mutation` object retains
all custom constructs. `branch_condition_strength_score` is a reach
diagnostic, not the headline custom CFA score.

For external evaluations, `metrics.json` is the public score interface. It uses
the combined custom CFA collector score and primary return/exception Oracle
Strength. Reach fields are retained only as diagnostics.
