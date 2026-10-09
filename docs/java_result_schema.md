# Java Evaluation Result Schema

One JSONL row represents one focal method under one evaluation configuration.
The table below covers both external evaluations and benchmark runs. Generation
adapters add configuration metadata, while separate collectors add optional PIT
evidence.

| Field | Meaning |
| --- | --- |
| `run_id`, `task_id` | Stable run/task identifiers |
| `language` | `java` |
| `model_id`, `prompt_template` | Configuration, with `external` used for supplied tests |
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

After extracting an archive, resolve its relative paths before analysis because
JSONL records alone are insufficient when referenced source or coverage artifacts
are missing. Failed rows still support failure analysis, but they do not enter
adequacy averages.

Preserve custom mutation objects in full because their collector statuses,
item outcomes, and denominators are needed to interpret the scores. The primary
analyzed score fields are
`boundary_strength_score`, `if_else_if_condition_avg_item_score`,
and `assertion_strength_score`. If a score is null, consult its status and
obligation counts to distinguish missing evidence from inapplicable constructs.

CFA reports construct-specific scores. In particular,
`if_else_if_condition_avg_item_score` covers condition obligations, whereas the
`extreme_condition_mutation` object retains evidence for all custom constructs.
By contrast, `branch_condition_strength_score` is a reach diagnostic rather than
the headline custom CFA score.

For external evaluations, `metrics.json` is the public score interface. It uses
the combined custom CFA collector score and primary return/exception Oracle
Strength, while reach fields are retained only as diagnostics.
