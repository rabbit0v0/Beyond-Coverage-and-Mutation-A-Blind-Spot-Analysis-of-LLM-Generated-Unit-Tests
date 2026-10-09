# Custom Assertion Mutation Operator Table

The collector in `scripts/collect_java_assertion_mutation.py` uses the following
deterministic fallback hierarchy. For each construct, it selects the first
applicable mutation rule.

| Construct | First Applicable Mutation Rule |
| --- | --- |
| Return value: Primitive boolean | Negate the returned value. |
| Return value: Boxed Boolean | Literal `true`/`false` and `null` are replaced with the opposite/non-null value. Expression-valued boxed Boolean returns are `not_generated` because `!(expr)` can introduce an unboxing NPE when the original expression is `null`. |
| Return value: Primitive integral numeric / char | Literal `0` becomes `1`, while non-zero literals become `0`. For expression returns, use `expr + 1` with casts for `byte`, `short`, and `char`. Like boolean negation, this changes the value without observing runtime values, including overflow cases. |
| Return value: Boxed integral numeric / Character | Literals and `null` are replaced with type-compatible alternatives. Expression-valued boxed returns are `not_generated` because `expr + 1` can introduce an unboxing NPE when the original expression is `null`. |
| Return value: Floating point | Apply a literal-only no-op guard. `0.0` becomes `1.0`, while non-zero literals become `0.0`. Expression-valued `float`/`double` returns are `not_generated` because `expr + 1` can be a no-op for huge magnitudes or `NaN`. |
| Return value: String | String literals become `""`, except `""` becomes `"MUTATED1"`. Expression returns become `expr + "MUTATED1"`, which is value-changing and null-safe in Java string concatenation. |
| Return value: Literal null | If the original return expression is literally `null`, replace it with a type-compatible non-null/default value where the declared return type permits one. For example, boxed booleans/numerics become non-null primitive literals, strings become `""`, class tokens become a compatible `.class` token, collections use the collection rule, and `Object` becomes `new Object()`. This emits `operator_rule = return_null_type_compatible_replacement` or a class-token-specific variant. Because tests reaching null-returning paths often assert `assertNull`, this is an easy mutant by construction. Therefore, its high kill rate should not be read as strong structural oracle evidence. |
| Return value: Collection/Map/array | Return a mutable empty instance of the same broad type unless the original is provably empty, in which case return a mutable single-element instance. Prefer non-null singleton elements when the element type is inferable. |
| Return value: Enum | Return the next different constant in declaration order. Single-constant enums are explicitly `not_generated`. |
| Return value: Class token | Replace a `T.class` token with a different compatible class token. For `Class<? extends Bound>`, choose a replacement inside the recognized bound or mark `not_generated` if none is available. `Class<T>[]` and array-like class-token containers are treated as arrays rather than positive class-token values. |
| Return value: Domain object with observable state and an accessible no-arg constructor | Return an instance constructed with the no-arg constructor, leaving fields at type defaults. Observable state includes public fields, public JavaBean getters, public bare/fluent field accessors, overridden `equals`, and inherited visible members. Package-private/protected fields and accessors count only when the generated test is explicitly detected in the same package as `Subject.java`. `toString()` alone does not admit this rule. The admitting signal is recorded as `observability_signal`. |
| Return value: Immutable domain object with a single meaningful constructor value | Perturb that value using numeric `+1`, a string suffix, or boolean negation, as appropriate for its type. |
| Return value: Opaque object | Return `null` and tag the item as `degenerate_null` for separate reporting. This null substitution measures whether a test checks for null/non-null behavior, not whether it checks the returned object's internal state. If the object has an accessible constructor but no statically observable state, mark it `not_generated` with `unobservable_return_state` and report it as a representability limitation. |
| Return value: Otherwise | Mark as `not_generated`. |
| Exception behavior: direct throw | Replace `throw new T(...)` with a different exception type, never `AssertionError`. For unchecked exceptions, use unchecked sibling-like alternatives, although `RuntimeException` is not a normal replacement candidate. For checked exceptions, prefer another checked type already declared by the method and otherwise fall back to an unchecked type. Original `RuntimeException`, `Exception`, and `Throwable` throws are tagged `subsumed_by_broad_original` and reported both included and excluded because broad-type assertions cannot distinguish many generated replacements. |
| Exception behavior: indirect throw | For `throw ex;`, mutate the direct `ex = new T(...)` or `T ex = new T(...)` assignment in the same target method. Helper-factory throws are out of scope and are marked `not_generated`. |
| Exception replacement inside local catches | If an enclosing catch clause would catch both the original and candidate exception in the same way, reject that candidate as likely equivalent and try another. If no candidate changes the handler, mark `not_generated`. |
| Exception kill classification | Assertion failures and common assertion-library or Mockito verification failures are `killed_by_oracle`. Mockito strictness failures are logged as `mockito_strictness_failure` and remain incidental. For exception-behavior mutants, if the injected replacement exception escapes the test, the mutant is `killed_by_oracle` because the original passing test must have handled the original exception type. |

After mutation execution, scoring considers reached obligations only and limits
the primary assertion-strength score to `return_behavior` and `exception_behavior`.
For each obligation, the collector first averages scores over represented
generated variants and then averages those obligation scores. Under this policy,
`killed_by_oracle` scores `1.0`, while `survived` and `killed_incidentally` score
`0.0` by default. However, `--incidental-score exclude` removes incidental kills
from scoring. The collector reports `not_reached`, `not_generated`, `out_of_scope`,
compile errors, and timeouts separately.

Opaque-object null-return mutants receive the `degenerate_null` tag, but their
actual execution outcomes are still recorded. By default, they follow the same
scoring policy as other mutants. To exclude them from the primary denominator
instead, use `--degenerate-null-score exclude`.

To support sensitivity analysis, each row also records an optimistic any-killed
score and a strict all-killed score. In addition, collector summaries report both
pooled scores across all obligations and macro scores over rows, along with a
pessimistic bound that counts `not_generated`, `out_of_scope`, and degenerate-null-excluded
obligations as survived.

To keep mutation execution isolated, each mutant runs in its own temporary
project copy. Although the original `Subject.java` supplies candidate and coverage
information, it is not rewritten for mutation execution. To document this,
each run writes a `*-cleanliness.json`
sidecar containing `git status --short` before/after snapshots and an
`unchanged` flag.

For each obligation, the collector records the source line, covered flag,
pre-execution `status`, execution `outcome`, score, artifact class when
available, and a `mutants` list. Within that list, each mutant record includes
`operator`, `sub_variant`, `variant`, `source_line`,
`mutant_text`, `covered`, `coverage_imprecise`, pre-execution `status`,
execution `outcome`, `failure_type`, `artifact_class`, `operator_rule`, and
any tags such as `degenerate_null`. For argument alteration, it also records the
altered argument index and replacement. When line coverage may over-report, as
with ternaries or chained calls, `coverage_imprecise` marks that limitation.
Coverage is collected separately for each generated suite rather than combined
across suites. Finally, the collector summary reports per-operator counts and
generation rates for pre-execution status, execution outcome, and tags.
