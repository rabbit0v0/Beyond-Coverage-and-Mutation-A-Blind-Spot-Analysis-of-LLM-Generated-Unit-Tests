import inspect
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_java_blindspots as analyze  # noqa: E402
import collect_java_assertion_mutation as assertion_mutation  # noqa: E402


class Approx:
    def __init__(self, expected: float, tolerance: float = 1e-12):
        self.expected = expected
        self.tolerance = tolerance

    def __eq__(self, actual):
        return abs(actual - self.expected) <= self.tolerance


class PytestCompat:
    @staticmethod
    def approx(expected: float):
        return Approx(expected)


pytest = PytestCompat()


def make_workdir(tmp_path: Path, source: str) -> Path:
    workdir = tmp_path / f"case_{len(list(tmp_path.iterdir()))}"
    subject = workdir / "src/main/java/benchmark/Subject.java"
    subject.parent.mkdir(parents=True)
    subject.write_text(source, encoding="utf-8")
    return workdir


def analyze_case(tmp_path: Path, source: str, signature: str, test_code: str, **row_overrides):
    workdir = make_workdir(tmp_path, source)
    row = {
        "task_id": "JCR-REGRESSION",
        "language": "java",
        "model_id": "regression-model",
        "prompt_template": "regression",
        "workdir": str(workdir),
        "task": {"signature": signature},
        "extracted_test_code": test_code,
        "compile_passed": True,
        "execution_passed": True,
        "mutation_score": None,
    }
    row.update(row_overrides)
    return analyze.analyze_row(row)


def boundary_categories(profile, covered: bool):
    key = "boundary_matched_items" if covered else "boundary_missed_items"
    return {(item.get("param_index"), item.get("category"), item.get("bound_key")) for item in profile[key]}


def test_boundary_null_and_empty_string_are_parameter_specific(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static boolean accepts(String value) {
            return value == null || value.isEmpty();
          }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static boolean accepts(String value)",
        'class T { void t() { Subject.accepts(null); Subject.accepts(""); } }',
    )
    matched = boundary_categories(profile, True)
    assert (0, "null", None) in matched
    assert (0, "empty", None) in matched


def test_boundary_zero_valueof_counts_as_zero_for_boxed_integer(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static int clamp(Integer value) { return value == null ? 0 : value; }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static int clamp(Integer value)",
        "class T { void t() { Subject.clamp(Integer.valueOf(0)); } }",
    )
    assert (0, "zero", None) in boundary_categories(profile, True)


def test_boundary_direct_boolean_only_excludes_domain_boolean_wrapper(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static class WrappedBoolean { public boolean value; }
          public static boolean stop(WrappedBoolean wrapped) { return wrapped.value; }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static boolean stop(WrappedBoolean wrapped)",
        """
        class T { void t() {
          Subject.WrappedBoolean stop = new Subject.WrappedBoolean();
          stop.value = false;
          Subject.stop(stop);
        } }
        """,
    )
    applicable = {item["category"] for item in profile["boundary_applicable_items"]}
    assert "false_value" not in applicable
    assert "true_value" not in applicable


def test_boundary_empty_collection_constructor_counts_empty(tmp_path):
    source = """
        package benchmark;
        import java.util.*;
        public class Subject {
          public static int size(List<String> values) { return values.size(); }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static int size(java.util.List<String> values)",
        "class T { void t() { Subject.size(new java.util.ArrayList<String>()); } }",
    )
    assert (0, "empty", None) in boundary_categories(profile, True)


def test_boundary_object_zero_arg_constructor_is_not_empty(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static class Context {}
          public static int use(Context context) { return context == null ? 0 : 1; }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static int use(Context context)",
        "class T { void t() { Subject.use(new Subject.Context()); } }",
    )
    assert (0, "empty", None) not in boundary_categories(profile, True)


def test_different_inferred_bound_requires_different_from_all_source_literals(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static int code(String value) {
            if ("A".equals(value)) return 1;
            if ("B".equals(value)) return 2;
            return 0;
          }
        }
    """
    profile_same = analyze_case(
        tmp_path,
        source,
        "public static int code(String value)",
        'class T { void t() { Subject.code("A"); } }',
    )
    profile_diff = analyze_case(
        tmp_path,
        source,
        "public static int code(String value)",
        'class T { void t() { Subject.code("C"); } }',
    )
    same_diff_items = [item for item in profile_same["boundary_matched_items"] if item["category"] == "different_inferred_bound"]
    diff_diff_items = [item for item in profile_diff["boundary_matched_items"] if item["category"] == "different_inferred_bound"]
    assert same_diff_items == []
    assert len(diff_diff_items) == 1
    assert diff_diff_items[0]["inferred_bound"]["values"] == ["A", "B"]


def test_private_target_boundary_and_assertion_are_not_scored(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          private static int hidden(int value) { return value == 0 ? 1 : 2; }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "private static int hidden(int value)",
        "class T { void t() { } }",
        assertion_mutation={"status": "passed", "total_items": 1, "scorable_items": 1, "checked_items": 1, "score": 1.0},
    )
    assert profile["target_is_private"] is True
    assert profile["boundary_strength_score"] is None
    assert profile["assertion_mutation_status"] == "skipped_private_target_method"
    assert profile["assertion_strength_scorable_items"] == 0


def test_non_executable_row_never_contributes_assertion_score(tmp_path):
    source = """
        package benchmark;
        public class Subject { public static int id(int value) { return value; } }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static int id(int value)",
        "class T { void t() { Subject.id(1); } }",
        execution_passed=False,
        error_stage="execute",
        assertion_mutation={"status": "passed", "total_items": 1, "scorable_items": 1, "checked_items": 1, "score": 1.0},
    )
    assert profile["assertion_mutation_status"] == "skipped_original_not_executed"
    assert profile["assertion_mutation_raw_status"] == "passed"
    assert profile["assertion_strength_scorable_items"] == 0
    assert profile["assertion_strength_checked_items"] == 0


def test_assertion_funnel_is_nested_and_uses_executable_public_rows(tmp_path):
    rows = [
        {"execution_passed": False, "target_is_private": False, "assertion_mutation_raw_status": "passed"},
        {"execution_passed": True, "target_is_private": True, "assertion_mutation_raw_status": "passed"},
        {
            "execution_passed": True,
            "target_is_private": False,
            "assertion_mutation_raw_status": "passed",
            "assertion_mutation_status": "passed",
            "assertion_strength_total_items": 2,
            "assertion_strength_reached_items": 1,
            "assertion_strength_represented_items": 1,
            "assertion_strength_scorable_items": 1,
            "assertion_strength_checked_items": 1,
        },
    ]
    table = "\n".join(analyze.assertion_strength_funnel_table(rows))
    assert "| rows in set | 3 |" in table
    assert "| -> execution_passed | 2 |" in table
    assert "| -> execution_passed and public target method | 1 |" in table
    assert "| -> executable public rows with passed assertion_mutation evidence | 1 |" in table
    assert "- Non-executable rows: 1" in table
    assert "- Executable private-target rows: 1" in table


def test_custom_control_flow_primary_score_uses_scorable_denominator(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static int sign(int x) {
            return x > 0 ? 1 : -1;
          }
        }
    """
    profile = analyze_case(
        tmp_path,
        source,
        "public static int sign(int x)",
        "class T { void t() { Subject.sign(1); } }",
        extreme_condition_mutation={
            "status": "passed",
            "total_conditions": 2,
            "scorable_conditions": 1,
            "score_sum": 0.5,
            "checked_items": 1,
            "represented_items": 1,
            "by_construct": {
                "ternary": {
                    "total": 2,
                    "scorable": 1,
                    "score_sum": 0.5,
                    "checked": 1,
                    "represented": 1,
                    "total_variants": 4,
                    "represented_variants": 2,
                }
            },
            "items": [],
        },
    )
    assert profile["if_else_if_condition_checked_score"] == pytest.approx(1.0)
    assert profile["if_else_if_condition_avg_item_score"] == pytest.approx(0.5)
    assert profile["if_else_if_condition_pessimistic_score"] == pytest.approx(0.25)
    assert profile["if_else_if_condition_by_construct"]["ternary"]["total_variants"] == 4


def test_ternary_without_custom_extreme_is_not_counted_from_line_mutation():
    items = [
        {
            "category": "branch_condition",
            "construct": "ternary",
            "line": 1,
            "candidate_pit_lines": [1],
            "snippet": "return x > 0 ? 1 : -1;",
        }
    ]
    pit = [{"category": "branch_condition", "line": 1, "status": "KILLED", "mutator": "conditional", "description": "conditional"}]
    profile = analyze.source_control_flow_mutation_profile(items, pit, custom_extreme_available=False)
    item = profile["classified_items"][0]
    assert profile["reached"] == 0
    assert item["coverage_imprecise"] is True
    assert item["coverage_source"] == "custom_extreme_mutation_missing"
    assert item["mutation_representability_status"] == "custom_extreme_missing"


def test_ternary_with_custom_extreme_may_carry_line_tag_but_is_custom_sourced():
    items = [
        {
            "category": "branch_condition",
            "construct": "ternary",
            "line": 1,
            "candidate_pit_lines": [1],
            "snippet": "return x > 0 ? 1 : -1;",
        }
    ]
    pit = [{"category": "branch_condition", "line": 1, "status": "KILLED", "mutator": "conditional", "description": "conditional"}]
    profile = analyze.source_control_flow_mutation_profile(items, pit, custom_extreme_available=True)
    item = profile["classified_items"][0]
    assert profile["reached"] == 1
    assert item["coverage_imprecise"] is True
    assert item["coverage_source"] == "custom_extreme_mutation"




def test_boxed_integral_expression_return_is_not_generated_to_avoid_unboxing_npe():
    replacement = assertion_mutation.alternate_return_for_source("", "Integer", "value")
    assert replacement["expression"] is None
    assert replacement["rule"] == "boxed_integral_expression_not_generated_unboxing_guard"


def test_boxed_boolean_expression_return_is_not_generated_to_avoid_unboxing_npe():
    replacement = assertion_mutation.alternate_return_for_source("", "Boolean", "flag")
    assert replacement["expression"] is None
    assert replacement["rule"] == "boxed_boolean_expression_not_generated_unboxing_guard"


def test_degenerate_null_mutants_are_scored_by_normal_outcome_rules():
    aggregate = assertion_mutation.aggregate_variant_scores(
        [
            {
                "outcome": "killed_by_oracle",
                "degenerate_null": True,
                "excluded_from_primary_score": True,
            }
        ],
        incidental_score="exclude",
    )
    assert aggregate["score"] == 1.0
    assert aggregate["outcome"] == "killed_by_oracle"

    survived = assertion_mutation.aggregate_variant_scores(
        [{"outcome": "survived", "degenerate_null": True, "excluded_from_primary_score": True}],
        incidental_score="exclude",
    )
    assert survived["score"] == 0.0
    assert survived["outcome"] == "survived"

    incidental = assertion_mutation.aggregate_variant_scores(
        [{"outcome": "killed_incidentally", "degenerate_null": True, "excluded_from_primary_score": True}],
        incidental_score="exclude",
    )
    assert incidental["score"] is None
    assert incidental["outcome"] == "killed_incidentally"


def test_dependency_result_substitution_uses_non_null_neutral_and_allows_downstream_flow():
    source = """
        package benchmark;
        public class Subject {
          public static String load() {
            String result = dependency.fetch();
            if (result.isEmpty()) {
              return "empty";
            }
            return result;
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "dependency.fetch" in text)
    mutated, meta = assertion_mutation.mutated_result_call_preserved_source(
        source,
        {"line": line},
        "public static String load()",
        "String",
    )

    assert mutated is not None
    assert "dependency.fetch();" in mutated
    assert 'String result = "";' in mutated
    assert meta["stub_rule"] == "neutral_empty_string"
    assert meta["call_behavior"] == "preserved"
    assert meta["mutation_dimension"] == "return_value"
    assert meta["confounded_dimensions"] == []
    assert meta["downstream_flow_risk"] is True
    assert "branch_or_loop" in meta["use_contexts"]
    assert meta["whole_rhs_substitution"] is True


def test_dependency_neutral_values_are_type_appropriate():
    expected = {
        "boolean": "false",
        "int": "0",
        "long": "0L",
        "double": "0.0d",
        "String": '""',
        "List<String>": "java.util.Collections.emptyList()",
        "Set<String>": "java.util.Collections.emptySet()",
        "Map<String, Integer>": "java.util.Collections.emptyMap()",
    }

    for type_text, expression in expected.items():
        neutral = assertion_mutation.neutral_stub_for_type(type_text)
        assert neutral["expression"] == expression
        assert not neutral.get("degenerate_null")


def test_dependency_result_substitution_tags_opaque_null():
    source = """
        package benchmark;
        public class Subject {
          public static Response load() {
            Response response = request.execute();
            return response;
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "request.execute" in text)
    disabled, disabled_meta = assertion_mutation.mutated_result_call_preserved_source(
        source,
        {"line": line},
        "public static Response load()",
        "Response",
    )
    assert disabled is None
    assert disabled_meta["operator_rule"] == "result_stub_no_type_replacement"

    mutated, meta = assertion_mutation.mutated_result_call_preserved_source(
        source,
        {"line": line},
        "public static Response load()",
        "Response",
        allow_opaque_null=True,
    )

    assert mutated is not None
    assert "request.execute();" in mutated
    assert "Response response = null;" in mutated
    assert meta["degenerate_null"] is True
    assert meta["stub_rule"] == "opaque_reference_neutral_null"


def test_path_changed_survivor_is_represented_but_not_scorable():
    aggregate = assertion_mutation.aggregate_variant_scores(
        [{"outcome": "survived_path_changed"}],
        incidental_score="exclude",
    )

    assert aggregate["represented_variant_count"] == 1
    assert aggregate["scored_variant_count"] == 0
    assert aggregate["score"] is None
    assert aggregate["outcome"] == "survived_path_changed"


def test_sensitivity_only_mutant_is_reported_but_not_scored():
    aggregate = assertion_mutation.aggregate_variant_scores(
        [{"outcome": "killed_by_oracle", "sensitivity_only": True}],
        incidental_score="exclude",
    )

    assert aggregate["represented_variant_count"] == 1
    assert aggregate["primary_variant_count"] == 0
    assert aggregate["scored_variant_count"] == 0
    assert aggregate["score"] is None
    assert aggregate["score_any_killed"] is None
    assert aggregate["outcome"] == "killed_by_oracle"


def test_dependency_result_substitution_infers_prior_stream_declaration():
    source = """
        package benchmark;
        public class Subject {
          public static int read() throws Exception {
            java.io.InputStream inputStream;
            inputStream = connection.getInputStream();
            return inputStream.read();
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "getInputStream" in text)
    mutated, meta = assertion_mutation.mutated_result_call_preserved_source(
        source,
        {"line": line},
        "public static int read() throws Exception",
        "int",
    )

    assert mutated is not None
    assert "inputStream = new java.io.ByteArrayInputStream(new byte[0]);" in mutated
    assert meta["inferred_type"] == "java.io.InputStream"
    assert meta["degenerate_null"] is False


def test_side_effect_assignment_does_not_use_return_value_probe():
    source = """
        package benchmark;
        public class Subject {
          public static Response send() {
            Response response = request.execute();
            return response;
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "request.execute" in text)
    mutants = assertion_mutation.mutant_sources_for_item(
        source,
        {"line": line, "construct": "side_effect_or_dependency"},
        "Response",
        "public static Response send()",
    )

    assert len(mutants) == 1
    variant, mutated, meta = mutants[0]
    assert variant == "statement_removed"
    assert mutated is None
    assert meta["operator_rule"] == "def_use_out_of_scope"


def test_side_effect_output_stream_redirect_is_primary_and_precedes_deletion():
    source = """
        package benchmark;
        public class Subject {
          public static void send() throws Exception {
            java.io.OutputStream to_s;
            to_s = to.getOutputStream();
            to_s.write(1);
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "getOutputStream" in text)
    mutants = assertion_mutation.mutant_sources_for_item(
        source,
        {"line": line, "construct": "side_effect_or_dependency"},
        "void",
        "public static void send() throws Exception",
    )

    assert len(mutants) == 1
    variant, mutated, meta = mutants[0]
    assert variant == "output_redirected_in_memory"
    assert mutated is not None and "to_s = new java.io.ByteArrayOutputStream();" in mutated
    assert meta["operator"] == "side_effect"
    assert meta["call_behavior"] == "redirected"
    assert meta["mutation_dimension"] == "external_effect_destination"
    assert meta["confounded_dimensions"] == []
    assert meta["sensitivity_only"] is False


def test_side_effect_opaque_null_probe_is_construct_specific_sensitivity_only():
    source = """
        package benchmark;
        public class Subject {
          public static Response send() {
            Response response = request.execute();
            return response;
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "request.execute" in text)
    mutants = assertion_mutation.mutant_sources_for_item(
        source,
        {"line": line, "construct": "side_effect_or_dependency"},
        "Response",
        "public static Response send()",
        allow_opaque_null_side_effect_sensitivity=True,
        include_confounded_side_effect_neutralization=True,
    )

    assert len(mutants) == 1
    variant, mutated, meta = mutants[0]
    assert variant == "confounded_call_suppressed_value_neutralized"
    assert mutated is not None and "request.execute()" not in mutated
    assert "Response response = null;" in mutated
    assert meta["call_behavior"] == "suppressed"
    assert meta["confounded_dimensions"] == ["invocation", "return_value"]
    assert meta["sensitivity_only"] is True


def test_return_behavior_opaque_null_rule_is_unchanged_by_dependency_policy():
    source = """
        package benchmark;
        public class Subject {
          public static Response send() {
            return request.execute();
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "return request.execute" in text)
    mutants = assertion_mutation.mutant_sources_for_item(
        source,
        {"line": line, "construct": "return_behavior"},
        "Response",
        "public static Response send()",
    )

    variant, mutated, meta = mutants[0]
    assert variant == "return_value_changed"
    assert mutated is not None and "return null;" in mutated
    assert meta["operator_rule"] == "opaque_object_degenerate_null"
    assert meta["degenerate_null"] is True


def test_interaction_dependency_records_preserved_and_suppressed_variants_separately():
    source = """
        package benchmark;
        public class Subject {
          public static String load() {
            String result = dependency.fetch();
            return result;
          }
        }
    """
    line = next(index for index, text in enumerate(source.splitlines(), start=1) if "dependency.fetch" in text)
    mutants = assertion_mutation.mutant_sources_for_item(
        source,
        {"line": line, "construct": "interaction_dependency"},
        "String",
        "public static String load()",
    )
    variants = {variant: (mutated, meta) for variant, mutated, meta in mutants}

    preserved, preserved_meta = variants["call_preserved_value_neutralized"]
    suppressed, suppressed_meta = variants["call_suppressed_value_neutralized"]
    assert preserved is not None and "dependency.fetch();" in preserved
    assert preserved_meta["call_behavior"] == "preserved"
    assert preserved_meta["confounded_dimensions"] == []
    assert suppressed is not None and "dependency.fetch();" not in suppressed
    assert suppressed_meta["call_behavior"] == "suppressed"
    assert suppressed_meta["confounded_dimensions"] == ["invocation", "return_value"]


def test_exception_fallback_replacement_uses_safe_string_constructor():
    source = """
        package benchmark;
        public class Subject {
          static class CrashReport {}
          public static void fail() {
            throw new IllegalArgumentException(new CrashReport());
          }
        }
    """
    start = source.index("IllegalArgumentException")
    end = start + len("IllegalArgumentException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        start,
        end,
        "IllegalArgumentException",
        "public static void fail()",
    )
    assert mutated is not None
    assert 'new IllegalStateException("MUTATED1")' in mutated
    assert meta["constructor_rule"] == "replacement_exception_string_constructor_fallback"


def test_exception_type_mutation_preserves_string_message_constructor():
    source = """
        package benchmark;
        public class Subject {
          public static void fail(String value) {
            throw new IllegalArgumentException("bad value: " + value);
          }
        }
    """
    start = source.index("IllegalArgumentException")
    end = start + len("IllegalArgumentException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        start,
        end,
        "IllegalArgumentException",
        "public static void fail(String value)",
    )
    assert mutated is not None
    assert 'new IllegalStateException("bad value: " + value)' in mutated
    assert meta["constructor_rule"] == "preserved_original_exception_constructor_args"


def test_exception_type_mutation_preserves_string_throwable_constructor():
    source = """
        package benchmark;
        public class Subject {
          public static void fail(Throwable cause) {
            throw new IllegalArgumentException("bad", cause);
          }
        }
    """
    start = source.index("IllegalArgumentException")
    end = start + len("IllegalArgumentException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        start,
        end,
        "IllegalArgumentException",
        "public static void fail(Throwable cause)",
    )
    assert mutated is not None
    assert 'new IllegalStateException("bad", cause)' in mutated
    assert meta["constructor_rule"] == "preserved_original_exception_constructor_args"


def test_custom_exception_multi_arg_string_prefix_constructor_is_preserved():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message, int code, Object detail) {}
          }
          static class SecondException extends Exception {
            SecondException(String message, int code, Object detail) {}
          }
          public static void fail(Object detail) throws FirstException, SecondException {
            throw new FirstException("bad", 7, detail);
          }
        }
    """
    type_start = source.index('throw new FirstException("bad", 7, detail)') + len("throw new ")
    type_end = type_start + len("FirstException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "FirstException",
        "public static void fail(Object detail) throws FirstException, SecondException",
    )
    assert mutated is not None
    assert 'throw new SecondException("bad", 7, detail)' in mutated
    assert meta["constructor_rule"] == "preserved_original_exception_constructor_args_custom_string_prefix"


def test_custom_exception_constructor_mismatch_is_not_generated():
    source = """
        package benchmark;
        public class Subject {
          static class CrashReport {}
          static class FirstException extends Exception {
            FirstException(CrashReport report) {}
          }
          static class SecondException extends Exception {
            SecondException(String message) {}
          }
          public static void fail() throws FirstException, SecondException {
            throw new FirstException(new CrashReport());
          }
        }
    """
    type_start = source.index("throw new FirstException(new CrashReport())") + len("throw new ")
    type_end = type_start + len("FirstException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "FirstException",
        "public static void fail() throws FirstException, SecondException",
    )
    assert mutated is None
    assert meta["operator_rule"] == "replacement_constructor_incompatible"
    assert assertion_mutation.status_for_unwritten_mutant(meta) == "not_generated"


def test_indirect_exception_replacement_must_match_declared_variable_type():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message) { super(message); }
          }
          static class SecondException extends Exception {
            SecondException(String message) { super(message); }
          }
          public static void fail() throws FirstException, SecondException {
            FirstException ex = new FirstException("bad");
            throw ex;
          }
        }
    """
    line = source[: source.index("throw ex;")].count("\n") + 1
    mutated, meta = assertion_mutation.mutated_indirect_exception_source(
        source,
        line,
        "ex",
        "public static void fail() throws FirstException, SecondException",
    )
    assert mutated is None
    assert meta["operator_rule"] == "replacement_not_assignable_to_declared_type"
    assert meta["declared_assignment_type"] == "FirstException"
    assert assertion_mutation.status_for_unwritten_mutant(meta) == "not_generated"


def test_indirect_exception_replacement_allows_broad_declared_variable_type():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message) { super(message); }
          }
          static class SecondException extends Exception {
            SecondException(String message) { super(message); }
          }
          public static void fail() throws Exception {
            Exception ex = new FirstException("bad");
            throw ex;
          }
        }
    """
    line = source[: source.index("throw ex;")].count("\n") + 1
    mutated, meta = assertion_mutation.mutated_indirect_exception_source(
        source,
        line,
        "ex",
        "public static void fail() throws Exception",
    )
    assert mutated is not None
    assert 'Exception ex = new Exception("bad");' in mutated
    assert meta["operator_rule"] == "checked_exception_declared_sibling"


def test_checked_exception_replacement_uses_enclosing_method_throws_not_outer_signature():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message) { super(message); }
          }
          static class SecondException extends Exception {
            SecondException(String message) { super(message); }
          }
          public static void outer() throws FirstException, SecondException {
            helper();
          }
          private static void helper() throws FirstException {
            throw new FirstException("bad");
          }
        }
    """
    type_start = source.index('throw new FirstException("bad")') + len("throw new ")
    type_end = type_start + len("FirstException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "FirstException",
        "public static void outer() throws FirstException, SecondException",
    )
    assert mutated is not None
    assert "throw new IllegalStateException(\"bad\")" in mutated
    assert meta["operator_rule"] == "checked_exception_fallback_unchecked"


def test_checked_exception_inside_lambda_does_not_borrow_outer_throws():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message) { super(message); }
          }
          static class SecondException extends Exception {
            SecondException(String message) { super(message); }
          }
          interface ThrowingRunnable { void run() throws FirstException; }
          public static void outer() throws FirstException, SecondException {
            ThrowingRunnable r = () -> {
              throw new FirstException("bad");
            };
            r.run();
          }
        }
    """
    type_start = source.index('throw new FirstException("bad")') + len("throw new ")
    type_end = type_start + len("FirstException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "FirstException",
        "public static void outer() throws FirstException, SecondException",
    )
    assert mutated is not None
    assert 'throw new IllegalStateException("bad")' in mutated
    assert meta["operator_rule"] == "checked_exception_fallback_unchecked"


def test_checked_exception_inside_anonymous_class_does_not_borrow_outer_throws():
    source = """
        package benchmark;
        public class Subject {
          static class FirstException extends Exception {
            FirstException(String message) { super(message); }
          }
          static class SecondException extends Exception {
            SecondException(String message) { super(message); }
          }
          interface ThrowingRunnable { void run() throws FirstException; }
          public static void outer() throws FirstException, SecondException {
            ThrowingRunnable r = new ThrowingRunnable() {
              public void run() throws FirstException {
                throw new FirstException("bad");
              }
            };
            r.run();
          }
        }
    """
    type_start = source.index('throw new FirstException("bad")') + len("throw new ")
    type_end = type_start + len("FirstException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "FirstException",
        "public static void outer() throws FirstException, SecondException",
    )
    assert mutated is not None
    assert 'throw new IllegalStateException("bad")' in mutated
    assert meta["operator_rule"] == "checked_exception_fallback_unchecked"


def test_domain_object_new_concrete_constructor_uses_default_args_instead_of_null():
    source = """
        package benchmark;
        public class Subject {
          public static AccessLogElement create(String name) {
            return new HeaderElement(name);
          }
          public static class AccessLogElement {}
          public static class HeaderElement extends AccessLogElement {
            public HeaderElement(String name) {}
          }
        }
    """
    replacement = assertion_mutation.alternate_return_for_source(
        source,
        "AccessLogElement",
        "new HeaderElement(name)",
    )
    assert replacement["expression"] == 'new HeaderElement("MUTATED1")'
    assert replacement["rule"] == "domain_object_concrete_constructor_defaults"


def test_domain_object_record_return_uses_canonical_default_args_instead_of_null():
    source = """
        package benchmark;
        public class Subject {
          public record Point(int x, String name) {}
          public static Object create() {
            return new Point(1, "a");
          }
        }
    """
    replacement = assertion_mutation.alternate_return_for_source(
        source,
        "Object",
        'new Point(1, "a")',
    )
    assert replacement["expression"] == 'new Point(1, "MUTATED1")'
    assert replacement["rule"] == "domain_object_record_canonical_defaults"


def test_plain_java_manual_assertion_error_log_is_oracle_kill(tmp_path):
    log = tmp_path / "execute.log"
    log.write_text(
        """
        [ERROR] Failed to execute goal org.codehaus.mojo:exec-maven-plugin:3.5.0:java
        (default-cli) on project task: An exception occurred while executing the Java class.
        java.lang.AssertionError: expected [42] but was [0]
            at benchmark.GeneratedSmokeTest.checkEquals(GeneratedSmokeTest.java:12)
            at benchmark.GeneratedSmokeTest.main(GeneratedSmokeTest.java:7)
        -> [Help 1]
        """,
        encoding="utf-8",
    )
    status, reason = assertion_mutation.killed_status_from_execute_log(str(log))
    assert status == "killed_by_oracle"
    assert reason == "assertion_failure_or_assertion_library_signal"


def test_return_subcategory_is_derived_from_operator_rule_not_source_type():
    assert assertion_mutation.return_subcategory_for_operator_rule("boolean_negate") == "boolean"
    assert assertion_mutation.return_subcategory_for_operator_rule("integral_expression_plus_one_noop_safe") == "numeric"
    assert assertion_mutation.return_subcategory_for_operator_rule("string_expression_concatenated_noop_safe") == "string"
    assert assertion_mutation.return_subcategory_for_operator_rule("collection_map_array_empty_or_singleton") == "collection"
    assert assertion_mutation.return_subcategory_for_operator_rule("enum_next_declared_constant") == "enum_or_class"
    assert assertion_mutation.return_subcategory_for_operator_rule("opaque_object_degenerate_null") == "object"


def test_assertion_strength_primary_scope_excludes_side_effect_research_items():
    mutation = {
        "status": "passed",
        "items": [
            {
                "construct": "return_behavior",
                "status": "executed",
                "outcome": "killed_by_oracle",
                "represented": True,
                "score": 1.0,
                "mutants": [{"operator_rule": "boolean_negate", "return_subcategory": "boolean"}],
            },
            {
                "construct": "exception_behavior",
                "status": "executed",
                "outcome": "survived",
                "represented": True,
                "score": 0.0,
                "mutants": [{"operator_rule": "unchecked_exception_sibling"}],
            },
            {
                "construct": "side_effect_or_dependency",
                "status": "executed",
                "outcome": "killed_by_oracle",
                "represented": True,
                "score": 1.0,
                "mutants": [{"operator_rule": "file_write_redirected"}],
            },
        ],
        "by_construct": {
            "return_behavior": {"total": 1, "scorable": 1, "represented": 1, "checked": 1, "score_sum": 1.0},
            "exception_behavior": {"total": 1, "scorable": 1, "represented": 1, "checked": 0, "score_sum": 0.0},
            "side_effect_or_dependency": {"total": 1, "scorable": 1, "represented": 1, "checked": 1, "score_sum": 1.0},
        },
    }
    profile = analyze.assertion_strength_profile(mutation, execution_passed=True, target_is_private=False)
    assert profile["assertion_strength_scorable_items"] == 2
    assert profile["assertion_strength_checked_items"] == 1
    assert profile["assertion_strength_score"] == 0.5
    assert set(profile["assertion_strength_by_construct"]) == {"return_behavior", "exception_behavior"}
    assert profile["side_effect_research_scope"]["scorable_items"] == 1
    assert set(profile["side_effect_research_by_construct"]) == {"side_effect_or_dependency"}


def test_object_return_rule_summary_keeps_object_rules_separate():
    items = [
        {
            "construct": "return_behavior",
            "status": "executed",
            "outcome": "killed_incidentally",
            "represented": True,
            "score": None,
            "mutants": [{"operator_rule": "opaque_object_degenerate_null", "return_subcategory": "object"}],
        },
        {
            "construct": "return_behavior",
            "status": "executed",
            "outcome": "killed_by_oracle",
            "represented": True,
            "score": 1.0,
            "mutants": [{"operator_rule": "domain_object_concrete_constructor_defaults", "return_subcategory": "object"}],
        },
    ]
    by_rule = analyze.return_object_rule_summary(items)
    assert set(by_rule) == {"opaque_object_degenerate_null", "domain_object_concrete_constructor_defaults"}
    assert by_rule["opaque_object_degenerate_null"]["incidental"] == 1
    assert by_rule["domain_object_concrete_constructor_defaults"]["checked"] == 1


def test_domain_object_default_constructor_admits_public_field_observability():
    source = """
        package benchmark;
        public class Subject {
          public static Result make() {
            return cached;
          }
          public static class Result {
            public int code;
          }
        }
    """
    replacement = assertion_mutation.alternate_return_for_source(source, "Result", "cached")
    assert replacement["expression"] == "new Result()"
    assert replacement["rule"] == "domain_object_default_constructor"
    assert replacement["observability_signal"] == "public_field"


def test_package_private_observability_requires_same_package_test():
    source = """
        package benchmark;
        public class Subject {
          public static Result make() {
            return cached;
          }
          public static class Result {
            int code;
          }
        }
    """
    same_package = assertion_mutation.alternate_return_for_source(source, "Result", "cached", same_package_test=True)
    other_package = assertion_mutation.alternate_return_for_source(source, "Result", "cached", same_package_test=False)
    assert same_package["rule"] == "domain_object_default_constructor"
    assert same_package["observability_signal"] == "package_field_same_package"
    assert other_package["expression"] is None
    assert other_package["rule"] == "unobservable_return_state"
    assert other_package["unobservable_return_state"] is True


def test_to_string_alone_does_not_admit_default_constructor_object_return():
    source = """
        package benchmark;
        public class Subject {
          public static Token make() {
            return token;
          }
          public static class Token {
            public String toString() { return "token"; }
          }
        }
    """
    replacement = assertion_mutation.alternate_return_for_source(source, "Token", "token", same_package_test=True)
    assert replacement["expression"] is None
    assert replacement["rule"] == "unobservable_return_state"
    assert replacement["unobservable_return_state"] is True


def test_method_local_public_field_like_declaration_does_not_admit_object_return():
    source = """
        package benchmark;
        public class Subject {
          public static Token make() {
            return token;
          }
          public static class Token {
            public void helper() {
              publicFieldLike local = null;
            }
          }
          public static class publicFieldLike {}
        }
    """
    replacement = assertion_mutation.alternate_return_for_source(source, "Token", "token", same_package_test=True)
    assert replacement["expression"] is None
    assert replacement["rule"] == "unobservable_return_state"


def test_exception_broad_original_is_tagged_and_excludable_from_scope():
    source = """
        package benchmark;
        public class Subject {
          public static void fail() {
            throw new RuntimeException("bad");
          }
        }
    """
    type_start = source.index("RuntimeException")
    type_end = type_start + len("RuntimeException")
    mutated, meta = assertion_mutation.mutate_exception_type_at(
        source,
        type_start,
        type_end,
        "RuntimeException",
        "public static void fail()",
    )
    assert mutated is not None
    assert meta["subsumed_by_broad_original"] is True
    mutation = {
        "status": "passed",
        "items": [
            {
                "construct": "exception_behavior",
                "status": "executed",
                "outcome": "survived",
                "represented": True,
                "score": 0.0,
                "mutants": [{"operator_rule": meta["operator_rule"], "subsumed_by_broad_original": True}],
            },
            {
                "construct": "exception_behavior",
                "status": "executed",
                "outcome": "killed_by_oracle",
                "represented": True,
                "score": 1.0,
                "mutants": [{"operator_rule": "unchecked_exception_sibling"}],
            },
        ],
    }
    profile = analyze.assertion_strength_profile(mutation, execution_passed=True, target_is_private=False)
    assert profile["assertion_strength_score"] == 1.0
    assert profile["assertion_strength_unmeasurable_by_type_substitution_items"] == 1
    assert profile["exception_behavior_excluding_broad_original_scope"]["score"] == 1.0
    assert profile["exception_behavior_excluding_broad_original_scope"]["scorable_items"] == 1


def test_boundary_unresolvable_argument_rate_tracks_variable_arguments():
    source = """
        package benchmark;
        public class Subject {
          public static void check(int code) {}
        }
    """
    test_code = """
        package benchmark;
        public class GeneratedSmokeTest {
          void test() {
            int[] values = new int[] {1};
            Subject.check(values.length);
            Subject.check(0);
          }
        }
    """
    stats = analyze.boundary_unresolvable_argument_stats(test_code, "public static void check(int code)", source)
    assert stats["total_arguments"] == 2
    assert stats["unresolvable_arguments"] == 1


def test_selected_empty_reused_coverage_stays_not_reached(tmp_path):
    source = """
        package benchmark;
        public class Subject {
          public static int value() {
            return 1;
          }
        }
    """
    workdir = make_workdir(tmp_path, source)
    row = {
        "task_id": "JCR-REGRESSION",
        "language": "java",
        "model_id": "regression-model",
        "prompt_template": "regression",
        "workdir": str(workdir),
        "task": {"signature": "public static int value()"},
        "execution_passed": True,
    }
    item = assertion_mutation.assertion_mutation_items(
        (workdir / "src/main/java/benchmark/Subject.java").read_text(encoding="utf-8"),
        "public static int value()",
    )[0]
    prior = {assertion_mutation.assertion_item_key(item): {**item, "covered_by_original_test": False}}

    mutation = assertion_mutation.collect_row(row, timeout=1, mvn="mvn", selected_item_keys=prior)

    assert mutation["status"] == "passed"
    assert mutation["total_items"] == 1
    assert mutation["reached_items"] == 0
    assert mutation["items"][0]["status"] == "not_reached"
    assert mutation["items"][0]["covered_by_original_test"] is False
    assert mutation["items"][0]["coverage_stage"] == "selected_prior_item_coverage"






def test_regression_case_count_is_large_enough():
    this_file = Path(__file__).read_text(encoding="utf-8")
    assert this_file.count("def test_") >= 15


def run_tests() -> int:
    failures = []
    for name, test in sorted(globals().items()):
        if not name.startswith("test_") or not callable(test):
            continue
        kwargs = {}
        temp_dir = None
        if "tmp_path" in inspect.signature(test).parameters:
            temp_dir = tempfile.TemporaryDirectory()
            kwargs["tmp_path"] = Path(temp_dir.name)
        try:
            test(**kwargs)
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001 - test runner should report assertion details too.
            failures.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
    print(f"{len(globals())} globals scanned; {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
