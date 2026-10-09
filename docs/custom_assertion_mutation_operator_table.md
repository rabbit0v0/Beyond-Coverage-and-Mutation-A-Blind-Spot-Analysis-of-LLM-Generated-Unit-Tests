# Custom Assertion Mutation Operator Table

This table documents the deterministic fallback hierarchy used by
`scripts/collect_java_assertion_mutation.py`.

| Construct | First Applicable Mutation Rule |
| --- | --- |
| Return value: Primitive boolean | Negate the returned value. |
| Return value: Boxed Boolean | Literal `true`/`false` and `null` are replaced with the opposite/non-null value. Expression-valued boxed Boolean returns are `not_generated` because `!(expr)` can introduce an unboxing NPE when the original expression is `null`. |
| Return value: Primitive integral numeric / char | Literal `0` becomes `1`; non-zero literals become `0`. Expression returns are perturbed as `expr + 1` with casts for `byte`, `short`, and `char`. This is the numeric analogue of boolean negation: it is value-changing without observing runtime values, including overflow cases. |
| Return value: Boxed integral numeric / Character | Literals and `null` are replaced with type-compatible alternatives. Expression-valued boxed returns are `not_generated` because `expr + 1` can introduce an unboxing NPE when the original expression is `null`. |
| Return value: Floating point | Literal-only no-op guard. `0.0` becomes `1.0`; non-zero literals become `0.0`. Expression-valued `float`/`double` returns are `not_generated` because `expr + 1` can be a no-op for huge magnitudes or `NaN`. |
| Return value: String | String literals become `""`, except `""` becomes `"MUTATED1"`. Expression returns become `expr + "MUTATED1"`, which is value-changing and null-safe in Java string concatenation. |
| Return value: Literal null | If the original return expression is literally `null`, replace it with a type-compatible non-null/default value where the declared return type permits one: e.g. boxed booleans/numerics become non-null primitive literals, strings become `""`, class tokens become a compatible `.class` token, collections use the collection rule, and `Object` becomes `new Object()`. This emits `operator_rule = return_null_type_compatible_replacement` or a class-token-specific variant. Tests reaching null-returning paths often assert `assertNull`, so this is an easy mutant by construction; its high kill rate should not be read as strong structural oracle evidence. |
| Return value: Collection/Map/array | Return a mutable empty instance of the same broad type; if the original is provably empty, return a mutable single-element instance. Prefer non-null singleton elements when the element type is inferable. |
| Return value: Enum | Return the next different constant in declaration order. Single-constant enums are explicitly `not_generated`. |
| Return value: Class token | Replace a `T.class` token with a different compatible class token. For `Class<? extends Bound>`, choose a replacement inside the recognized bound; otherwise mark `not_generated`. `Class<T>[]` and array-like class-token containers are treated as arrays, not positive class-token values. |
| Return value: Domain object with observable state and an accessible no-arg constructor | Return an instance constructed with the no-arg constructor, leaving fields at type defaults. Observable state includes public fields, public JavaBean getters, public bare/fluent field accessors, overridden `equals`, and inherited visible members. Package-private/protected fields and accessors count only when the generated test is explicitly detected in the same package as `Subject.java`. `toString()` alone does not admit this rule. The admitting signal is recorded as `observability_signal`. |
| Return value: Immutable domain object with a single meaningful constructor value | Perturb that value by rule: numeric `+1`, string suffix, or boolean negation. |
| Return value: Opaque object | Return `null` and tag the item as `degenerate_null` for separate reporting. This null substitution measures whether a test checks for null/non-null behavior, not whether it checks the returned object's internal state. If the object has an accessible constructor but no statically observable state, mark it `not_generated` with `unobservable_return_state` and report it as a representability limitation. |
| Return value: Otherwise | Mark as `not_generated`. |
| Exception behavior: direct throw | Replace `throw new T(...)` with a different exception type, never `AssertionError`. Use unchecked sibling-like alternatives for unchecked exceptions; `RuntimeException` is not a normal replacement candidate. For checked exceptions, prefer another checked type already declared by the method; otherwise fall back to an unchecked type. Original `RuntimeException`, `Exception`, and `Throwable` throws are tagged `subsumed_by_broad_original` and reported both included and excluded because broad-type assertions cannot distinguish many generated replacements. |
| Exception behavior: indirect throw | For `throw ex;`, mutate the direct `ex = new T(...)` or `T ex = new T(...)` assignment in the same target method. Helper-factory throws are out of scope and are marked `not_generated`. |
| Exception replacement inside local catches | If an enclosing catch clause would catch both the original and candidate exception in the same way, reject that candidate as likely equivalent and try another; if none changes the handler, mark `not_generated`. |
| Exception kill classification | Assertion failures and common assertion-library or Mockito verification failures are `killed_by_oracle`. Mockito strictness failures are logged as `mockito_strictness_failure` and remain incidental. For exception-behavior mutants, if the injected replacement exception escapes the test, the mutant is `killed_by_oracle` because the original passing test must have handled the original exception type. |

Scoring is over reached obligations only. The primary assertion-strength score
is scoped to `return_behavior` and `exception_behavior`.
The primary score is the mean over represented generated variants for
each obligation, then those obligation scores are averaged. `killed_by_oracle`
scores `1.0`; `survived` scores `0.0`; `killed_incidentally` scores `0.0` by
default and can be excluded with `--incidental-score exclude`; `not_reached`,
`not_generated`, `out_of_scope`, compile errors, and timeouts are reported
separately. Opaque-object null-return mutants are tagged `degenerate_null`;
their real execution outcome is still recorded and, by default, scored under
the same outcome policy as other mutants. They can be excluded from the primary
denominator with `--degenerate-null-score exclude`.

For sensitivity, each row also records an optimistic any-killed score and a
strict all-killed score. Collector summaries report both pooled scores across all
obligations and macro scores over rows. They also report a pessimistic bound
that counts `not_generated`, `out_of_scope`, and degenerate-null-excluded
obligations as survived.

Mutants are executed in isolated per-mutant temporary project copies. The
original `Subject.java` is read for candidate discovery and coverage, but it is
not rewritten for mutation execution. Each run writes a `*-cleanliness.json`
sidecar containing `git status --short` before/after snapshots and an
`unchanged` flag.

Per obligation, the collector records the source line, covered flag,
pre-execution `status`, execution `outcome`, score, artifact class when
available, and a `mutants` list. Each mutant
record includes `operator`, `sub_variant`, `variant`, `source_line`,
`mutant_text`, `covered`, `coverage_imprecise`, pre-execution `status`,
execution `outcome`, `failure_type`, `artifact_class`, `operator_rule`, and
any tags such as `degenerate_null`. For argument alteration, it also records the
altered argument index and replacement. `coverage_imprecise` marks line-coverage
cases such as ternaries or chained calls where line coverage may over-report.
Coverage is collected per generated suite, not unioned across suites. The
collector summary reports per-operator counts and generation rates for
pre-execution status, execution outcome, and tags.
