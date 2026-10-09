# Java Evaluation Example

`Subject.clamp` replaces negative integers with zero and preserves nonnegative
integers. The JUnit 5 test checks -1, 0, and 2, without model or benchmark downloads.

Run the commands in the root README. Expected basic results:

- `compile_passed` and `execution_passed` are true in `results.jsonl`.
- BVA credits negative, zero, and inferred-bound input evidence.
- CFA and mutation-based Oracle Strength scores are null
  in basic mode because custom mutation collection was not requested.
- In mutation mode, control-flow and return obligations have detailed mutant
  outcomes.

Inspect item statuses rather than treating missing scores as zero. The full
profile preserves the evidence behind each result.

For the bundled example, a verified mutation run produced `metrics.json` scores
of 1.0 for BVA, CFA, and Oracle Strength. Environment/tool failures
can make evidence unavailable; the expected scores do not override item statuses.
