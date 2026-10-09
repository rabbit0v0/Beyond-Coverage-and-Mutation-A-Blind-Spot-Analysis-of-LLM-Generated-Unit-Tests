#!/usr/bin/env python3
"""Compute target-scoped Java bias profiles for Java pilot results.

The current analyzer combines source and test-input evidence with
extreme-condition and assertion-mutation evidence for BVA, CFA, and OS.
"""

from __future__ import annotations

import argparse
import csv
import functools
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from tree_sitter_language_pack import configure, get_parser
    from tree_sitter_language_pack.options import PackConfig
except Exception:  # pragma: no cover - optional local dependency
    configure = None
    get_parser = None
    PackConfig = None


ROOT = Path(__file__).resolve().parents[1]
PARSER_CACHE = ROOT / ".cache" / "tree-sitter-language-pack"

CATEGORIES = [
    "boundary_value",
    "weak_oracle",
    "branch_condition",
    "state_transition",
    "interaction_dependency",
]

CATEGORY_DISPLAY_NAMES = {
    "boundary_value": "boundary_value",
    "weak_oracle": "weak_oracle",
    "branch_condition": "branch_condition",
    "interaction_dependency": "external_resource_setup",
}

ASSERTION_PATTERNS = [
    r"\bassert[A-Z]\w*\s*\(",
    r"\bassert\s+",
    r"throw\s+new\s+AssertionError\b",
    r"\bfail\s*\(",
]

WEAK_ASSERTION_PATTERNS = [
    r"assertTrue\s*\(\s*true\s*\)",
    r"assertFalse\s*\(\s*false\s*\)",
    r"assertNotNull\s*\([^)]*\)",
    r"assertInstanceOf\s*\([^)]*\)",
    r"if\s*\([^)]*\)\s*;\s*",
]

SEMANTIC_ASSERTION_PATTERNS = [
    r"\bassertEquals\s*\(",
    r"\bassertArrayEquals\s*\(",
    r"\bassertIterableEquals\s*\(",
    r"\bassertThat\s*\(",
    r"\bassertTrue\s*\(\s*(?!true\s*\))[^)]*(?:!=|==|<=|>=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|endsWith\s*\(|instanceof|\.isEmpty\s*\(|\.size\s*\(|\.length)[^)]*\)",
    r"\bassertFalse\s*\(\s*(?!false\s*\))[^)]*(?:!=|==|<=|>=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|endsWith\s*\(|instanceof|\.isEmpty\s*\(|\.size\s*\(|\.length)[^)]*\)",
    r"throw\s+new\s+AssertionError\b",
    r"if\s*\([^)]*(?:!=|==|<|>|\.equals\s*\(|instanceof|\.size\s*\(|\.length)[^)]*\)\s*\{?\s*throw\s+new\s+AssertionError\b",
]

CORRECT_EXCEPTION_ASSERTION_PATTERNS = [
    r"assertThrows\s*\(\s*[A-Za-z_][\w.]*(?:Exception|Error|Throwable)\.class",
]

TRIVIAL_POSITIVE_ASSERTION_RE = re.compile(
    r"assertTrue\s*\(\s*(?:true|[^)]*\.size\s*\(\s*\)\s*>\s*0|[^)]*\.length\s*>\s*0)\s*\)|"
    r"assertFalse\s*\(\s*false\s*\)|"
    r"assertNotNull\s*\(|assertInstanceOf\s*\(",
    re.MULTILINE,
)

BOUNDARY_SOURCE_PATTERNS = {
    "null": r"==\s*null|!=\s*null|\bObjects\.(?:isNull|nonNull)\s*\(",
    "empty": r"\.isEmpty\s*\(|\.length\s*\(\s*\)\s*==\s*0|\.size\s*\(\s*\)\s*==\s*0|\.length\s*==\s*0",
    "zero": (
        r"(?:==|!=|<=|>=|<|>)\s*0\b|\b0\s*(?:==|!=|<=|>=|<|>)|"
        r"\b(?:size|length|count|num|number|index|offset|limit|skip)\w*\b|"
        r"\+\+|--|\+=|-=|\*=|/=|%=|(?<![+\-*/%])[-+*/%](?![+\-*/%=])"
    ),
    "negative": r"<\s*0|<=\s*-1|-\s*1",
}

BOUNDARY_CATEGORIES = [
    "null",
    "empty",
    "false_value",
    "true_value",
    "zero",
    "negative",
    "below_inferred_bound",
    "at_inferred_bound",
    "above_inferred_bound",
    "same_inferred_bound",
    "different_inferred_bound",
]

BOUNDARY_TEST_PATTERNS = {
    "null": r"\bnull\b",
    "empty": r"empty\w*\s*\(|\"\"\b|new\s+\w+\s*\[\s*0\s*\]|List\.of\s*\(\s*\)|Arrays\.asList\s*\(\s*\)|Collections\.empty",
    "false_value": r"\bfalse\b|Boolean\.FALSE\b",
    "true_value": r"\btrue\b|Boolean\.TRUE\b",
    "zero": r"(?<![\w.])0(?![\w.])",
    "negative": r"(?<![\w.])-\s*[1-9]\d*(?![\w.])",
}

BOUNDARY_SOURCE_TO_CATEGORY = {
    "null": "null",
    "empty": "empty",
    "zero": "zero",
    "negative": "negative",
}

STATE_SOURCE_RE = re.compile(
    r"\bthis\.\w+\s*=(?!=)|\b[A-Z][A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\s*(?:=(?!=)|\+\+|--|\+=|-=)|"
    r"\b\w+\s*(?:\+\+|--|\+=|-=)|\.(?:add|put|remove|clear|set)\s*\(|"
    r"\bset[A-Z]\w*\s*\(",
    re.MULTILINE,
)

STATE_TEST_RE = re.compile(
    r"\bget[A-Z]\w*\s*\(|\bassert\w*\s*\([^;]*(?:size|state|count|value|field)|"
    r"\.\w+\s*==|getDeclaredField|\.size\s*\(",
    re.MULTILINE,
)

INTERACTION_SOURCE_RE = re.compile(
    r"\b(?:File|Path|Files|InputStream|OutputStream|Reader|Writer|URL|URI|Socket|Random|Clock|Instant|"
    r"LocalDate|LocalDateTime|System\.currentTimeMillis|System\.nanoTime|System\.getenv|Thread|Executor|"
    r"Supplier|Consumer|Function|Predicate|Callback|HttpClient|RestTemplate|WebClient|DataSource|Connection|"
    r"Logger|Service|Client|Repository|Repo|DAO|Dao|Gateway|Provider|Adapter|Sender|Listener)\b|"
    r"\b[A-Za-z_]\w*(?:Service|Client|Repository|Repo|DAO|Dao|Gateway|Provider|Adapter|Sender|Listener)\b|"
    r"\b(?:service|client|repository|repo|dao|gateway|provider|adapter|sender|listener)\.\w+\s*\(",
    re.MULTILINE,
)

INTERACTION_TEST_RE = re.compile(
    r"fake|stub|mock|override|extends|new\s+(?:Supplier|Consumer|Function|Predicate)|"
    r"ByteArrayInputStream|ByteArrayOutputStream|Clock\.fixed|temp|Temporary|Mockito|when\s*\(|thenReturn\s*\(|"
    r"thenThrow\s*\(|verify\s*\(|ArgumentCaptor|Mock[A-Z]|\bFake[A-Z]|\bStub[A-Z]",
    re.IGNORECASE | re.MULTILINE,
)

PRIMARY_ASSERTION_CONSTRUCTS = {"return_behavior", "exception_behavior"}
# Future work: side-effect adequacy metric.
SIDE_EFFECT_RESEARCH_CONSTRUCTS = {"side_effect_or_dependency", "interaction_dependency"}
RETURN_SUBCATEGORY_ORDER = ["boolean", "numeric", "string", "collection", "enum_or_class", "object"]
OBJECT_RETURN_RULE_ORDER = [
    "opaque_object_degenerate_null",
    "return_null_type_compatible_replacement",
    "domain_object_default_constructor",
    "domain_object_concrete_constructor_defaults",
]
MIN_SUBCATEGORY_N = 10


def return_subcategory_for_operator_rule(operator_rule: str | None) -> str | None:
    rule = str(operator_rule or "")
    if not rule:
        return None
    if rule == "boolean_negate":
        return "boolean"
    if rule in {
        "numeric_zero_or_one_literal_only",
        "floating_zero_or_one_literal_only",
        "integral_expression_plus_one_noop_safe",
        "integral_expression_increment_noop_safe",
        "boxed_integral_literal_only",
    }:
        return "numeric"
    if rule in {"string_empty_or_mutated_literal_only", "string_expression_concatenated_noop_safe"}:
        return "string"
    if rule == "collection_map_array_empty_or_singleton":
        return "collection"
    if rule in {
        "enum_next_declared_constant",
        "class_token_replaced_with_bounded_alternative",
        "class_token_replaced_with_string_class",
        "return_null_type_compatible_replacement_class_type_changed",
    } or rule.startswith("class_"):
        return "enum_or_class"
    if rule in {
        "domain_object_default_constructor",
        "domain_object_concrete_constructor_defaults",
        "domain_object_record_canonical_defaults",
        "domain_object_single_value_perturbed",
        "opaque_object_degenerate_null",
        "return_null_type_compatible_replacement",
        "unobservable_return_state",
    }:
        return "object"
    return None


def enrich_assertion_mutation_return_subcategories(assertion_mutation: dict[str, Any]) -> dict[str, Any]:
    if not assertion_mutation:
        return assertion_mutation
    items = assertion_mutation.get("items") or []
    changed = False
    enriched_items = []
    for item in items:
        if item.get("construct") != "return_behavior":
            enriched_items.append(item)
            continue
        enriched_item = dict(item)
        enriched_mutants = []
        item_subcategory = None
        for mutant in item.get("mutants") or []:
            enriched_mutant = dict(mutant)
            subcategory = enriched_mutant.get("return_subcategory") or return_subcategory_for_operator_rule(enriched_mutant.get("operator_rule"))
            if subcategory:
                enriched_mutant["return_subcategory"] = subcategory
                item_subcategory = item_subcategory or subcategory
                changed = True
            enriched_mutants.append(enriched_mutant)
        enriched_item["mutants"] = enriched_mutants
        if item_subcategory:
            enriched_item["return_subcategory"] = item_subcategory
        enriched_items.append(enriched_item)
    if not changed:
        return assertion_mutation
    return {**assertion_mutation, "items": enriched_items}


def item_has_mutant_flag(item: dict[str, Any], flag: str) -> bool:
    return any(mutant.get(flag) for mutant in item.get("mutants") or [])


def assertion_scope_summary(
    items: list[dict[str, Any]],
    constructs: set[str],
    exclude_broad_original: bool = False,  # retained for API compat; broad-original always excluded
) -> dict[str, Any]:
    all_scoped = [item for item in items if item.get("construct") in constructs]
    # Broad-original exception items are unmeasurable by type substitution: replacing
    # RuntimeException with a subtype (e.g. IllegalArgumentException) is still caught
    # by catch(RuntimeException), so survival/kill tells us nothing about oracle specificity.
    # Exclude them from the denominator entirely; report count as representability limitation.
    unmeasurable = [item for item in all_scoped if item_has_mutant_flag(item, "subsumed_by_broad_original")]
    scoped = [item for item in all_scoped if not item_has_mutant_flag(item, "subsumed_by_broad_original")]
    scorable = [item for item in scoped if item.get("score") is not None]
    reached = [item for item in scoped if item.get("status") != "not_reached"]
    represented = [item for item in scoped if item.get("represented")]
    score_sum = sum(float(item.get("score") or 0) for item in scorable)
    pessimistic_denominator = len(scorable) + sum(1 for item in scoped if item.get("status") in {"not_generated", "out_of_scope"})
    return {
        "total_items": len(all_scoped),  # includes unmeasurable for full accounting
        "reached_items": len(reached),
        "not_reached_items": sum(1 for item in scoped if item.get("status") == "not_reached"),
        "scorable_items": len(scorable),
        "non_representable_items": sum(
            1
            for item in scoped
            if item.get("status") in {"not_generated", "out_of_scope", "compile_error", "timeout", "coverage_unverified"}
            or item.get("outcome") in {"not_generated", "out_of_scope", "compile_error", "timeout", "coverage_unverified"}
        ) + len(unmeasurable),
        "checked_items": sum(1 for item in scorable if float(item.get("score") or 0) > 0),
        "oracle_killed_items": sum(1 for item in scoped if item.get("outcome") == "killed_by_oracle"),
        "incidentally_killed_items": sum(1 for item in scoped if item.get("outcome") == "killed_incidentally"),
        "survived_items": sum(1 for item in scoped if item.get("outcome") == "survived"),
        "survived_path_changed_items": sum(1 for item in scoped if item.get("outcome") == "survived_path_changed"),
        "coverage_unverified_items": sum(1 for item in scoped if item.get("status") == "coverage_unverified"),
        "unmeasurable_by_type_substitution_items": len(unmeasurable),
        "subsumed_by_broad_original_items": len(unmeasurable),  # backward-compat alias
        "unobservable_return_state_items": sum(1 for item in scoped if item_has_mutant_flag(item, "unobservable_return_state")),
        "represented_items": len(represented),
        "score_sum": score_sum,
        "score": score_sum / len(scorable) if scorable else None,
        "pessimistic_score": score_sum / pessimistic_denominator if pessimistic_denominator else None,
        "pessimistic_denominator": pessimistic_denominator,
        "representability_rate": len(represented) / len(reached) if reached else None,
        "scorable_rate": len(scorable) / len(reached) if reached else None,
    }


def return_subcategory_summary(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    score_sums: Counter[str] = Counter()
    for item in items:
        if item.get("construct") != "return_behavior":
            continue
        subcategory = item.get("return_subcategory")
        if not subcategory:
            for mutant in item.get("mutants") or []:
                subcategory = mutant.get("return_subcategory") or return_subcategory_for_operator_rule(mutant.get("operator_rule"))
                if subcategory:
                    break
        if not subcategory:
            continue
        stats = counters[str(subcategory)]
        stats["total"] += 1
        if item.get("status") != "not_reached":
            stats["reached"] += 1
        if item.get("represented"):
            stats["represented"] += 1
        if item.get("score") is not None:
            stats["scorable"] += 1
            score_sums[str(subcategory)] += float(item.get("score") or 0)
            if float(item.get("score") or 0) > 0:
                stats["checked"] += 1
        if any(mutant.get("degenerate_null") for mutant in item.get("mutants") or []):
            stats["degenerate_null"] += 1
        if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or []):
            stats["unobservable_return_state"] += 1
    return {
        subcategory: {
            "total": int(stats["total"]),
            "applicable": int(stats["total"]),
            "reached": int(stats["reached"]),
            "represented": int(stats["represented"]),
            "scorable": int(stats["scorable"]),
            "checked": int(stats["checked"]),
            "score_sum": float(score_sums[subcategory]),
            "score": float(score_sums[subcategory]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "representability": int(stats["represented"]) / int(stats["reached"]) if int(stats["reached"]) else None,
            "degenerate_null": int(stats["degenerate_null"]),
        }
        for subcategory, stats in counters.items()
    }

EXCEPTION_TEST_RE = re.compile(
    r"assertThrows|expected\s*=|catch\s*\([^)]*(?:Exception|Throwable|Error)|try\s*\{[^}]*\}\s*catch",
    re.DOTALL,
)

BRANCH_RE = re.compile(r"\bif\s*\(|\belse\b|\bcase\s+|\bdefault\s*:|\bfor\s*\(|\bwhile\s*\(|\btry\s*\{|\bcatch\s*\(|\bthrow\b|\?")
THROW_RE = re.compile(r"\bthrow\b")


EXCEPTION_LINE_RE = re.compile(r"\bthrow\b|\bthrows\b|\bcatch\s*\(|\b(?:Exception|Error|Throwable)\b")
INTERACTION_LINE_RE = re.compile(INTERACTION_SOURCE_RE.pattern, re.MULTILINE)
STATE_LINE_RE = re.compile(STATE_SOURCE_RE.pattern, re.MULTILINE)
BOUNDARY_LINE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in BOUNDARY_SOURCE_PATTERNS.values()), re.MULTILINE)

COMPILE_FAILURE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "unresolved_symbol_or_api_misuse",
        re.compile(r"cannot find symbol|symbol:\s+class|symbol:\s+method|symbol:\s+variable|no suitable method|constructor .* cannot be applied", re.I),
    ),
    (
        "type_mismatch",
        re.compile(r"incompatible types|bad operand types|cannot be converted|unexpected type", re.I),
    ),
    (
        "checked_exception_not_handled",
        re.compile(r"unreported exception .* must be caught or declared", re.I),
    ),
    (
        "private_or_reflection_access",
        re.compile(r"has private access|setAccessible|IllegalAccessException|InaccessibleObjectException", re.I),
    ),
]

EXECUTION_FAILURE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "wrong_oracle",
        re.compile(r"AssertionError|expected[=<:]|Expected .* but|Expected .*exception|expected message|Expected message|but was", re.I),
    ),
    (
        "uncaught_runtime_exception",
        re.compile(r"Exception while executing|An exception occurred while executing|NullPointerException|IndexOutOfBoundsException|IllegalArgumentException|NumberFormatException", re.I),
    ),
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("task_id") or ""),
        str(row.get("prompt_template") or ""),
        str(row.get("model_id") or ""),
    )


def load_custom_control_flow_index(paths: list[Path]) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in paths:
        candidates = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        for candidate in candidates:
            for row in read_jsonl(candidate):
                extreme = row.get("extreme_condition_mutation") or {}
                if not extreme:
                    continue
                index[row_key(row)] = extreme
    return index


def load_assertion_mutation_index(paths: list[Path]) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in paths:
        candidates = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        for candidate in candidates:
            for row in read_jsonl(candidate):
                assertion_mutation = row.get("assertion_mutation") or {}
                if not assertion_mutation:
                    continue
                index[row_key(row)] = assertion_mutation
    return index


def attach_custom_control_flow(
    row: dict[str, Any],
    custom_control_flow_index: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    if not custom_control_flow_index or row.get("extreme_condition_mutation"):
        return row
    extreme = custom_control_flow_index.get(row_key(row))
    if not extreme:
        return row
    return {**row, "extreme_condition_mutation": extreme}


def attach_assertion_mutation(
    row: dict[str, Any],
    assertion_mutation_index: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    if not assertion_mutation_index or row.get("assertion_mutation"):
        return row
    assertion_mutation = assertion_mutation_index.get(row_key(row))
    if not assertion_mutation:
        return row
    return {**row, "assertion_mutation": assertion_mutation}


def attach_external_evidence(
    row: dict[str, Any],
    custom_control_flow_index: dict[tuple[str, str, str], dict[str, Any]],
    assertion_mutation_index: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    row = attach_custom_control_flow(row, custom_control_flow_index)
    row = attach_assertion_mutation(row, assertion_mutation_index)
    return row


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def failure_log_path(row: dict[str, Any]) -> Path | None:
    workdir = row.get("workdir")
    prompt = row.get("prompt_template")
    if not workdir or not prompt:
        return None
    stage = row.get("error_stage")
    path = Path(workdir)
    if stage in {"compile", "compile_timeout"}:
        return path / f"mvn-test-{prompt}.log"
    if stage in {"execute", "execute_timeout"}:
        return path / f"execute-{prompt}.log"
    return path / f"mvn-test-{prompt}.log"


def first_error_lines(log: str, limit: int = 4) -> str:
    lines = []
    for line in log.splitlines():
        if "[ERROR]" in line or "Exception" in line or "AssertionError" in line or "error:" in line:
            lines.append(line.strip())
        if len(lines) >= limit:
            break
    return " / ".join(lines)


def classify_failure(log: str, stage: str | None) -> tuple[str, str, list[str]]:
    if stage in {"execute_timeout", "compile_timeout"}:
        return "execution_failure", "timeout_or_hang", ["timeout_or_hang"]
    if stage == "compile":
        matches = [name for name, pattern in COMPILE_FAILURE_PATTERNS if pattern.search(log)]
        if matches:
            return "compile_failure", matches[0], matches
        return "compile_failure", "syntax_or_other_compile_error", []
    if stage == "execute":
        matches = [name for name, pattern in EXECUTION_FAILURE_PATTERNS if pattern.search(log)]
        if matches:
            return "execution_failure", matches[0], matches
        return "execution_failure", "other_execution_failure", []
    return "unknown", "unknown", []


def failure_classification_for_row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("execution_passed"):
        return {
            "failure_family": "passed",
            "failure_category": "passed",
            "failure_matched_categories": [],
            "failure_log_path": None,
            "failure_evidence": "",
        }
    if is_panta_placeholder(row):
        return {
            "failure_family": "generation_failure",
            "failure_category": "panta_placeholder_stub",
            "failure_matched_categories": ["panta_placeholder_stub"],
            "failure_log_path": None,
            "failure_evidence": "panta returned a placeholder test stub (assertTrue(true))",
        }
    path = failure_log_path(row)
    log = read_log(path) if path else ""
    family, category, matches = classify_failure(log, row.get("error_stage"))
    return {
        "failure_family": family,
        "failure_category": category,
        "failure_matched_categories": matches,
        "failure_log_path": str(path) if path else None,
        "failure_evidence": first_error_lines(log),
    }


def strip_comments_and_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '"<STR>"', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "'<CHR>'", text)
    return text


def strip_comments_preserving_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//.*", " ", text)
    return text


def java_non_code_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            start = i
            newline = text.find("\n", i + 2)
            i = n if newline == -1 else newline
            spans.append((start, i))
            continue
        if ch == "/" and nxt == "*":
            start = i
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            spans.append((start, i))
            continue
        if ch in {"\"", "'"}:
            quote = ch
            start = i
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            spans.append((start, i))
            continue
        i += 1
    return spans


def position_in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def node_text(code: bytes, node: Any) -> str:
    return code[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def walk(node: Any):
    yield node
    for child in getattr(node, "children", []):
        yield from walk(child)


def ast_features_tree_sitter(test_code: str) -> dict[str, Any] | None:
    if configure is None or get_parser is None or PackConfig is None:
        return None
    try:
        configure(PackConfig(cache_dir=str(PARSER_CACHE)))
        code = test_code.encode("utf-8", errors="replace")
        root = get_parser("java").parse(code).root_node
    except Exception:
        return None

    counts: Counter[str] = Counter()
    call_names: Counter[str] = Counter()
    object_types: Counter[str] = Counter()
    literal_values: Counter[str] = Counter()
    for node in walk(root):
        counts[node.type] += 1
        if node.type == "method_invocation":
            name = node.child_by_field_name("name")
            if name is not None:
                call_names[node_text(code, name)] += 1
        elif node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                object_types[node_text(code, type_node)] += 1
        elif node.type in {"string_literal", "decimal_integer_literal", "decimal_floating_point_literal", "true", "false", "null_literal"}:
            literal_values[node_text(code, node)] += 1

    return {
        "syntax_backend": "tree_sitter",
        "method_declaration_count": counts["method_declaration"],
        "method_invocation_count": counts["method_invocation"],
        "object_creation_count": counts["object_creation_expression"],
        "catch_clause_count": counts["catch_clause"],
        "throw_statement_count": counts["throw_statement"],
        "if_statement_count": counts["if_statement"],
        "loop_statement_count": counts["for_statement"] + counts["enhanced_for_statement"] + counts["while_statement"] + counts["do_statement"],
        "literal_count": sum(literal_values.values()),
        "call_names": dict(call_names.most_common(50)),
        "object_types": dict(object_types.most_common(50)),
        "literal_values": dict(literal_values.most_common(50)),
    }


def ast_features_fallback(test_code: str) -> dict[str, Any]:
    clean = strip_comments_and_strings(test_code)
    call_names = Counter(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", clean))
    object_types = Counter(re.findall(r"\bnew\s+([A-Za-z_][A-Za-z0-9_<>.]*)\s*\(", clean))
    literal_values = Counter(re.findall(r'"(?:<STR>)"|\'(?:<CHR>)\'|\b(?:true|false|null)\b|(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])', clean))
    return {
        "syntax_backend": "regex_structural",
        "method_declaration_count": len(re.findall(r"\b(?:public|private|protected|static|final|\s)+[A-Za-z_][\w<>\[\].?,]*\s+[A-Za-z_]\w*\s*\(", clean)),
        "method_invocation_count": sum(call_names.values()),
        "object_creation_count": sum(object_types.values()),
        "catch_clause_count": len(re.findall(r"\bcatch\s*\(", clean)),
        "throw_statement_count": len(re.findall(r"\bthrow\b", clean)),
        "if_statement_count": len(re.findall(r"\bif\s*\(", clean)),
        "loop_statement_count": len(re.findall(r"\b(?:for|while)\s*\(", clean)),
        "literal_count": sum(literal_values.values()),
        "call_names": dict(call_names.most_common(50)),
        "object_types": dict(object_types.most_common(50)),
        "literal_values": dict(literal_values.most_common(50)),
    }


def ast_features(test_code: str) -> dict[str, Any]:
    return ast_features_tree_sitter(test_code) or ast_features_fallback(test_code)


def find_source(row: dict[str, Any]) -> Path | None:
    task = row.get("task") or {}
    candidates = []
    if task.get("task_dir"):
        candidates.append(Path(task["task_dir"]) / "src/main/java/benchmark/Subject.java")
    if row.get("workdir"):
        candidates.append(Path(row["workdir"]) / "src/main/java/benchmark/Subject.java")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def count_patterns(text: str, patterns: list[str]) -> int:
    return sum(len(re.findall(pattern, text, flags=re.MULTILINE)) for pattern in patterns)


def java_string_literal_values(text: str) -> list[str]:
    values = []
    for raw in re.findall(r'"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL):
        try:
            values.append(bytes(raw, "utf-8").decode("unicode_escape"))
        except UnicodeDecodeError:
            values.append(raw)
    return values


def has_unicode_boundary_text(value: str) -> bool:
    return any((ord(char) > 127) or (ord(char) < 32 and char not in "\t\n\r") for char in value)


def parse_numeric_literal(raw: str) -> float | None:
    cleaned = raw.replace("_", "").strip()
    cleaned = re.sub(r"[lLfFdD]$", "", cleaned)
    try:
        return float(cleaned) if any(char in cleaned for char in ".eE") else int(cleaned)
    except ValueError:
        return None


def numeric_literals(text: str) -> list[float]:
    values = []
    for match in re.findall(r"(?<![\w.])-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?(?![\w.])", text):
        value = parse_numeric_literal(match)
        if value is not None:
            values.append(value)
    return values


def direct_comparison_context(
    text: str,
    match: re.Match[str],
    param_start: int,
    param_end: int,
    other_start: int | None = None,
    other_end: int | None = None,
) -> str | None:
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    for start, end in [(param_start, param_end), (other_start, other_end)]:
        if start is None or end is None:
            continue
        before = text[line_start:start].rstrip()
        after = text[end:line_end].lstrip()
        if before and before[-1] in "+-*/%":
            return None
        if after and after[0] in "+-*/%[.":
            return None
    return text[match.start() : match.end()][:120]


def direct_parameter_inferred_bounds(source: str, signature: str | None) -> dict[int, list[dict[str, Any]]]:
    names = signature_parameter_names(signature)
    param_types = signature_parameter_types(signature)
    bounds: dict[int, list[dict[str, Any]]] = {}
    method_source = target_method_source(source, signature) or source
    searchable_source = strip_comments_preserving_strings(method_source)
    numeric_literal = r"-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?"
    string_literal = r'"(?:\\.|[^"\\])*"'
    for index, name in enumerate(names):
        escaped = re.escape(name)
        param_atom = rf"(?<![\w.]){escaped}(?![\w.\[])"
        numeric_atom = rf"(?<![\w.+\-*/%]){numeric_literal}(?![\w.])"
        param_type = param_types[index] if index < len(param_types) else ""
        is_numeric = is_numeric_parameter_type(param_type)
        is_string = is_string_parameter_type(param_type)
        if is_numeric:
            for match in re.finditer(rf"{param_atom}\s*(==|!=|<=|>=|<|>)\s*({numeric_atom})", searchable_source):
                number_start, number_end = match.span(2)
                context = direct_comparison_context(
                    searchable_source,
                    match,
                    match.start(),
                    match.start() + len(name),
                    number_start,
                    number_end,
                )
                if context is None:
                    continue
                value = parse_numeric_literal(match.group(2))
                if value is not None:
                    bounds.setdefault(index, []).append({"kind": "number", "value": value, "operator": match.group(1), "source": context})
            for match in re.finditer(rf"({numeric_atom})\s*(==|!=|<=|>=|<|>)\s*{param_atom}", searchable_source):
                param_start = match.end() - len(name)
                number_start, number_end = match.span(1)
                context = direct_comparison_context(searchable_source, match, param_start, match.end(), number_start, number_end)
                if context is None:
                    continue
                value = parse_numeric_literal(match.group(1))
                if value is not None:
                    bounds.setdefault(index, []).append({"kind": "number", "value": value, "operator": match.group(2), "source": context})
        if is_string:
            string_patterns = [
                rf"(?<![\w.]){escaped}(?![\w.])\s*(?:==|!=)\s*({string_literal})",
                rf"({string_literal})\s*(?:==|!=)\s*(?<![\w.]){escaped}(?![\w.])",
                rf"(?<![\w.]){escaped}\.equals\s*\(\s*({string_literal})\s*\)",
                rf"({string_literal})\.equals\s*\(\s*(?<![\w.]){escaped}(?![\w.])\s*\)",
                rf"Objects\.equals\s*\(\s*(?<![\w.]){escaped}(?![\w.])\s*,\s*({string_literal})\s*\)",
                rf"Objects\.equals\s*\(\s*({string_literal})\s*,\s*(?<![\w.]){escaped}(?![\w.])\s*\)",
            ]
            for pattern in string_patterns:
                for match in re.finditer(pattern, searchable_source):
                    literal = next((group for group in match.groups() if group), None)
                    if literal:
                        values = java_string_literal_values(literal)
                        if values:
                            bounds.setdefault(index, []).append({"kind": "string", "value": values[0], "operator": "equals", "source": match.group(0)[:120]})
    deduped: dict[int, list[dict[str, Any]]] = {}
    for index, items in bounds.items():
        seen = set()
        for item in items:
            key = (item["kind"], item["value"], item.get("operator"), item.get("source"))
            if key in seen:
                continue
            seen.add(key)
            deduped.setdefault(index, []).append(item)
    return deduped


def source_boundary_constraints(source: str) -> dict[str, Any]:
    string_max_lengths: set[int] = set()
    numeric_bounds: set[float] = set()
    for op1, number1, number2, op2 in re.findall(
        r"(?:\.length\s*\(\s*\)|\.size\s*\(\s*\)|\blength\b|\bsize\b)\s*(<=|<|>=|>)\s*(\d+)|"
        r"(\d+)\s*(<=|<|>=|>)\s*(?:\.length\s*\(\s*\)|\.size\s*\(\s*\)|\blength\b|\bsize\b)",
        source,
    ):
        if op1:
            number = int(number1)
            string_max_lengths.add(number - 1 if op1 == "<" and number > 0 else number)
        else:
            number = int(number2)
            string_max_lengths.add(number - 1 if op2 == ">" and number > 0 else number)

    for match in re.findall(r"(?:<=|<|>=|>|==|!=)\s*(-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?)", source):
        value = parse_numeric_literal(match)
        if value is not None:
            numeric_bounds.add(value)
    for match in re.findall(r"(-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?)\s*(?:<=|<|>=|>|==|!=)", source):
        value = parse_numeric_literal(match)
        if value is not None:
            numeric_bounds.add(value)

    return {
        "string_max_lengths": sorted(string_max_lengths),
        "numeric_bounds": sorted(numeric_bounds),
    }


def assertion_features(test_code: str) -> dict[str, Any]:
    syntax = ast_features(test_code)
    assertion_code = visible_assertion_code(test_code)
    assertion_count = count_patterns(assertion_code, ASSERTION_PATTERNS)
    weak_assertion_count = count_patterns(assertion_code, WEAK_ASSERTION_PATTERNS)
    semantic_assertion_count = count_patterns(assertion_code, SEMANTIC_ASSERTION_PATTERNS)
    correct_exception_assertion_count = count_patterns(assertion_code, CORRECT_EXCEPTION_ASSERTION_PATTERNS)
    target_calls = len(re.findall(r"\bSubject\.\w+\s*\(", test_code))
    result_assignments = len(re.findall(r"\b(?:var|Object|String|int|long|boolean|double|float|char)\s+\w+\s*=\s*Subject\.\w+\s*\(", test_code))
    return {
        **syntax,
        "assertion_count": assertion_count,
        "weak_assertion_count": weak_assertion_count,
        "semantic_assertion_count": semantic_assertion_count,
        "correct_exception_assertion_count": correct_exception_assertion_count,
        "target_calls": target_calls,
        "result_assignments": result_assignments,
        "has_exception_test": bool(EXCEPTION_TEST_RE.search(test_code)),
        "has_state_test": bool(STATE_TEST_RE.search(test_code)),
        "has_interaction_test": bool(INTERACTION_TEST_RE.search(test_code)),
        "uses_reflection": bool(re.search(r"getDeclared(?:Field|Method|Constructor)|setAccessible\s*\(", test_code)),
        "uses_subclass_or_override": bool(re.search(r"\bextends\b|@Override\b", test_code)),
        "swallowed_manual_failure_count": len(swallowed_manual_failure_spans(test_code)),
        "uses_manual_assertion_error": bool(re.search(r"throw\s+new\s+AssertionError\b", assertion_code)),
    }


def is_void_signature(signature: str | None) -> bool:
    if not signature:
        return False
    compact = " ".join(signature.split())
    return bool(re.search(r"\bvoid\s+[A-Za-z_]\w*\s*\(", compact))


def signature_declares_return_value(signature: str | None) -> bool:
    if not signature or "(" not in signature:
        return False
    if is_void_signature(signature):
        return False
    cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    before_params = cleaned[: cleaned.find("(")]
    tokens = re.findall(r"[A-Za-z_][\w.$<>?,\[\]]*", before_params)
    if len(tokens) < 2:
        return False
    method_name = tokens[-1]
    previous = tokens[-2]
    modifiers = {
        "public",
        "protected",
        "private",
        "static",
        "final",
        "abstract",
        "synchronized",
        "native",
        "strictfp",
        "default",
    }
    if method_name == "Subject" and previous in modifiers:
        return False
    return True


def signature_parameter_types(signature: str | None) -> list[str]:
    if not signature or "(" not in signature or ")" not in signature:
        return []
    cleaned_signature = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    params = cleaned_signature[cleaned_signature.find("(") + 1 : cleaned_signature.rfind(")")]
    if not params.strip():
        return []
    out = []
    for param in re.split(r",(?![^<]*>)", params):
        cleaned = " ".join(param.strip().split())
        cleaned = re.sub(r"\bfinal\b|@\w+(?:\([^)]*\))?", "", cleaned).strip()
        parts = cleaned.split()
        if len(parts) >= 2:
            out.append(" ".join(parts[:-1]))
        elif parts:
            out.append(parts[0])
    return out


def signature_parameter_names(signature: str | None) -> list[str]:
    if not signature or "(" not in signature or ")" not in signature:
        return []
    cleaned_signature = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    params = cleaned_signature[cleaned_signature.find("(") + 1 : cleaned_signature.rfind(")")]
    if not params.strip():
        return []
    out = []
    for param in re.split(r",(?![^<]*>)", params):
        cleaned = " ".join(param.strip().split())
        cleaned = re.sub(r"\bfinal\b|@\w+(?:\([^)]*\))?", "", cleaned).strip()
        parts = cleaned.split()
        if len(parts) >= 2:
            out.append(re.sub(r"\[\]$", "", parts[-1]))
    return out


def signature_parameter_declarations(signature: str | None) -> list[str]:
    if not signature or "(" not in signature or ")" not in signature:
        return []
    cleaned_signature = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    params = cleaned_signature[cleaned_signature.find("(") + 1 : cleaned_signature.rfind(")")]
    if not params.strip():
        return []
    return [" ".join(param.strip().split()) for param in re.split(r",(?![^<]*>)", params)]


def nullable_parameter_indexes(source: str, signature: str | None) -> set[int]:
    names = signature_parameter_names(signature)
    declarations = signature_parameter_declarations(signature)
    nullable: set[int] = set()
    for index, name in enumerate(names):
        declaration = declarations[index] if index < len(declarations) else ""
        if re.search(r"@(?:Nullable|CheckForNull|NullAllowed)\b", declaration):
            nullable.add(index)
            continue
        if re.search(rf"\b{re.escape(name)}\s*(?:==|!=)\s*null|null\s*(?:==|!=)\s*{re.escape(name)}\b", source):
            nullable.add(index)
            continue
        if re.search(rf"\bObjects\.(?:isNull|nonNull)\s*\(\s*{re.escape(name)}\s*\)", source):
            nullable.add(index)
            continue
        if re.search(rf"\bObjects\.requireNonNull\s*\(\s*{re.escape(name)}\b", source):
            nullable.add(index)
    return nullable


def signature_method_name(signature: str | None) -> str | None:
    if not signature or "(" not in signature:
        return None
    cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    before_params = cleaned[: cleaned.find("(")]
    matches = re.findall(r"\b([A-Za-z_]\w*)\b", before_params)
    return matches[-1] if matches else None


def is_private_method_signature(signature: str | None) -> bool:
    if not signature or "(" not in signature:
        return False
    cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", " ", signature)
    before_params = cleaned[: cleaned.find("(")]
    return bool(re.search(r"\bprivate\b", before_params))


@functools.lru_cache(maxsize=2048)
def target_method_source(source: str, signature: str | None) -> str | None:
    method_name = signature_method_name(signature)
    if not method_name:
        return None
    pattern = re.compile(rf"\b{re.escape(method_name)}\s*\(")
    for match in pattern.finditer(source):
        open_paren = source.find("(", match.start())
        close_paren = find_matching_paren(source, open_paren)
        if close_paren is None:
            continue
        between_name_and_paren = source[match.start() : open_paren]
        if "." in between_name_and_paren:
            continue
        open_brace = source.find("{", close_paren)
        next_semicolon = source.find(";", close_paren)
        if open_brace == -1 or (next_semicolon != -1 and next_semicolon < open_brace):
            continue
        header_start = max(source.rfind("\n", 0, match.start()), source.rfind("}", 0, match.start())) + 1
        header = source[header_start:open_brace]
        if re.search(rf"\b(?:if|for|while|switch|catch|return|new)\s+{re.escape(method_name)}\s*\(", header):
            continue
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is not None:
            return source[header_start : close_brace + 1]
    return None


def split_java_arguments(args_text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for i, char in enumerate(args_text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            args.append(args_text[start:i].strip())
            start = i + 1
    tail = args_text[start:].strip()
    if tail:
        args.append(tail)
    return args


def find_matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for i in range(open_index, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    i = open_index
    while i < len(text):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i + 2)
            if newline == -1:
                return None
            i = newline + 1
            continue
        if text.startswith("/*", i):
            comment_end = text.find("*/", i + 2)
            if comment_end == -1:
                return None
            i = comment_end + 2
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def swallowed_manual_failure_spans(test_code: str) -> list[tuple[int, int]]:
    """Return spans of manual test failures hidden by a swallowing catch block."""
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\btry\s*\{", test_code):
        open_brace = test_code.find("{", match.start())
        close_brace = find_matching_brace(test_code, open_brace)
        if close_brace is None:
            continue
        suffix_pos = close_brace + 1
        swallows_assertion = False
        while True:
            catch_match = re.match(
                r"\s*catch\s*\(\s*([^)]*?(?:AssertionError|Throwable|Error)[^)]*?)\s+\w+\s*\)\s*\{",
                test_code[suffix_pos:],
                flags=re.DOTALL,
            )
            if not catch_match:
                break
            catch_open = suffix_pos + catch_match.end() - 1
            catch_close = find_matching_brace(test_code, catch_open)
            if catch_close is None:
                break
            catch_body = test_code[catch_open + 1 : catch_close]
            rethrows_or_fails = bool(
                re.search(r"\bthrow\b|\bfail\s*\(|\bassert[A-Z]\w*\s*\(|\bassert\s+", catch_body)
            )
            if not rethrows_or_fails:
                swallows_assertion = True
                break
            suffix_pos = catch_close + 1
        if not swallows_assertion:
            continue
        try_body = test_code[open_brace + 1 : close_brace]
        for failure_match in re.finditer(r"throw\s+new\s+AssertionError\b[^;]*;|\bfail\s*\([^;]*\)\s*;", try_body):
            spans.append((open_brace + 1 + failure_match.start(), open_brace + 1 + failure_match.end()))
    return spans


def visible_assertion_code(test_code: str) -> str:
    """Mask manual failures that would be caught and swallowed inside the test."""
    if "AssertionError" not in test_code and "fail" not in test_code:
        return test_code
    chars = list(test_code)
    for start, end in swallowed_manual_failure_spans(test_code):
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def java_method_definitions(source: str) -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?:^|[;\n{}]\s*)"
        r"(?:@\w+(?:\([^)]*\))?\s*)*"
        r"(?:public|protected|private|static|final|synchronized|abstract|native|strictfp|\s)+"
        r"(?:<[^>{};]+>\s*)?"
        r"[A-Za-z_$][\w$.\[\]<>?,\s]*\s+"
        r"([A-Za-z_$][\w$]*)\s*\(",
        flags=re.MULTILINE,
    )
    for match in pattern.finditer(source):
        name = match.group(1)
        if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
            continue
        open_paren = source.find("(", match.start(1))
        close_paren = find_matching_paren(source, open_paren)
        if close_paren is None:
            continue
        open_brace = source.find("{", close_paren)
        next_semicolon = source.find(";", close_paren)
        if open_brace == -1 or (next_semicolon != -1 and next_semicolon < open_brace):
            continue
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None:
            continue
        methods.append(
            {
                "name": name,
                "args": source[open_paren + 1 : close_paren],
                "body": source[open_brace + 1 : close_brace],
                "start": match.start(),
                "end": close_brace + 1,
            }
        )
    return methods


def visible_assertion_helper_names(test_code: str) -> set[str]:
    """Find local helper methods whose failed check is visible to the test runner."""
    assertion_code = visible_assertion_code(test_code)
    methods = java_method_definitions(assertion_code)
    helper_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for method in methods:
            name = method["name"]
            if name in helper_names:
                continue
            body = method["body"]
            directly_fails = bool(
                re.search(
                    r"\bassert[A-Z]\w*\s*\(|\bassert\s+|\bfail\s*\(|throw\s+new\s+AssertionError\b",
                    body,
                )
            )
            delegates_to_helper = any(re.search(rf"\b{re.escape(helper)}\s*\(", body) for helper in helper_names)
            if directly_fails or delegates_to_helper:
                helper_names.add(name)
                changed = True
    return helper_names


def helper_assertion_calls_touching_target(
    test_code: str,
    method_name: str | None,
    helper_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not method_name:
        return []
    assertion_code = visible_assertion_code(test_code)
    helper_methods = {method["name"]: method for method in java_method_definitions(assertion_code)}
    names = helper_names if helper_names is not None else visible_assertion_helper_names(test_code)
    if not names:
        return []
    target_call_re = re.compile(rf"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*\.{re.escape(method_name)}\s*\(")
    result_vars = target_result_variables(test_code, method_name)
    calls: list[dict[str, Any]] = []
    for helper in sorted(names):
        pattern = re.compile(rf"\b{re.escape(helper)}\s*\(")
        for match in pattern.finditer(assertion_code):
            open_paren = assertion_code.find("(", match.start())
            close_paren = find_matching_paren(assertion_code, open_paren)
            if close_paren is None:
                continue
            args_text = assertion_code[open_paren + 1 : close_paren]
            args = split_java_arguments(args_text)
            touches_target = bool(target_call_re.search(args_text)) or any(
                re.search(rf"\b{re.escape(var)}\b", args_text) for var in result_vars
            )
            if touches_target:
                calls.append(
                    {
                        "name": helper,
                        "args": args,
                        "args_text": args_text,
                        "body": str((helper_methods.get(helper) or {}).get("body") or ""),
                    }
                )
    return calls


def helper_call_is_semantic_type_return_oracle(call: dict[str, Any]) -> bool:
    name = str(call.get("name") or "")
    args = [str(arg or "").strip() for arg in call.get("args") or []]
    body = str(call.get("body") or "")
    if len(args) < 2 or not re.search(r"(?:type|kind|category|minor|major)", name, flags=re.I):
        return False
    expected_args = args[1:]
    compares_expected = bool(
        re.search(r"\bexpected\b", body)
        and re.search(r"(?:==|!=|\.equals\s*\()", body)
    )
    extracts_semantic_type = bool(
        re.search(r"getDeclaredField\s*\(\s*\"[^\"]*(?:type|kind|category|minor|major)[^\"]*\"", body, flags=re.I)
        or re.search(r"\bget(?:Type|Kind|Category|Minor|Major)\s*\(", body)
        or re.search(r"\b(?:actual|result)\s*(?:==|!=|\.equals\s*\()\s*expected\b", body)
    )
    expected_is_type_token = any(
        re.search(r"\b[A-Z][A-Za-z0-9_]*\.[A-Z][A-Z0-9_]*\b|\b[A-Z][A-Z0-9_]{2,}\b|\.class\b", arg)
        for arg in expected_args
    )
    return compares_expected and extracts_semantic_type and expected_is_type_token


def helper_call_is_weak_return_oracle(call: dict[str, Any]) -> bool:
    name = str(call.get("name") or "")
    args_text = str(call.get("args_text") or "")
    return bool(
        re.search(r"(?:not\s*null|nonnull|null|instance|type|class)", name, flags=re.I)
        or re.search(r"!=\s*null|==\s*null|instanceof|\.getClass\s*\(", args_text)
    )


def helper_call_is_meaningful_return_oracle(call: dict[str, Any]) -> bool:
    if helper_call_is_semantic_type_return_oracle(call):
        return True
    if helper_call_is_weak_return_oracle(call):
        return False
    name = str(call.get("name") or "")
    args_text = str(call.get("args_text") or "")
    if re.search(r"(?:equal|same|content|contains|match|starts|ends)", name, flags=re.I):
        return True
    return bool(
        re.search(
            r"==|!=|<=|>=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|"
            r"endsWith\s*\(|\.isEmpty\s*\(|\.size\s*\(|\.length|get[A-Z]\w*\s*\(|\.(?:is|has)[A-Z]\w*\b",
            args_text,
        )
    )


def subject_calls(test_code: str, method_name: str | None) -> list[dict[str, Any]]:
    if not method_name:
        return []
    calls: list[dict[str, Any]] = []
    ignored_spans = java_non_code_spans(test_code)
    patterns = [
        re.compile(rf"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*\.{re.escape(method_name)}\s*\("),
        re.compile(rf"\bnew\s+Subject\s*\([^)]*\)\s*\.{re.escape(method_name)}\s*\("),
    ]
    seen_spans: set[tuple[int, int]] = set()
    for pattern in patterns:
        for match in pattern.finditer(test_code):
            if position_in_spans(match.start(), ignored_spans):
                continue
            open_index = test_code.rfind("(", match.start(), match.end())
            close_index = find_matching_paren(test_code, open_index)
            if close_index is None:
                continue
            span = (match.start(), close_index + 1)
            if span in seen_spans:
                continue
            seen_spans.add(span)
            calls.append(
                {
                    "start": match.start(),
                    "end": close_index + 1,
                    "text": test_code[match.start() : close_index + 1],
                    "args": split_java_arguments(test_code[open_index + 1 : close_index]),
                }
            )
    calls.extend(reflective_subject_calls(test_code, method_name))
    return calls


def reflective_target_method_variables(test_code: str, method_name: str) -> set[str]:
    variables: set[str] = set()
    pattern = re.compile(
        rf"(?:^|[;\{{\n]\s*)(?:final\s+)?(?:java\.lang\.reflect\.)?Method\s+([A-Za-z_]\w*)\s*=\s*[^;]*"
        rf"\.getDeclaredMethod\s*\(\s*\"{re.escape(method_name)}\"",
        flags=re.MULTILINE,
    )
    variables.update(pattern.findall(test_code))
    pattern = re.compile(
        rf"(?:^|[;\{{\n]\s*)(?:final\s+)?(?:var|Object)\s+([A-Za-z_]\w*)\s*=\s*[^;]*"
        rf"\.getDeclaredMethod\s*\(\s*\"{re.escape(method_name)}\"",
        flags=re.MULTILINE,
    )
    variables.update(pattern.findall(test_code))
    return variables


def reflective_subject_calls(test_code: str, method_name: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for variable in reflective_target_method_variables(test_code, method_name):
        pattern = re.compile(rf"\b{re.escape(variable)}\.invoke\s*\(")
        for match in pattern.finditer(test_code):
            open_index = test_code.find("(", match.start())
            close_index = find_matching_paren(test_code, open_index)
            if close_index is None:
                continue
            raw_args = split_java_arguments(test_code[open_index + 1 : close_index])
            target_args = raw_args[1:] if raw_args else []
            calls.append(
                {
                    "start": match.start(),
                    "end": close_index + 1,
                    "text": test_code[match.start() : close_index + 1],
                    "args": target_args,
                    "reflection_receiver_arg": raw_args[0] if raw_args else "",
                    "reflection_method_variable": variable,
                }
            )
    return calls


def java_literal_value(argument: str) -> Any:
    arg = argument.strip()
    if re.fullmatch(r"null", arg):
        return None
    if re.fullmatch(r"(?:true|Boolean\.TRUE)", arg):
        return True
    if re.fullmatch(r"(?:false|Boolean\.FALSE)", arg):
        return False
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', arg):
        return bytes(arg[1:-1], "utf-8").decode("unicode_escape", errors="ignore")
    if re.fullmatch(r"-?\s*\d[\d_]*(?:[lL])?", arg):
        return int(re.sub(r"[^\d-]", "", arg))
    if re.fullmatch(r"-?\s*\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[fFdD]?", arg):
        parsed = parse_numeric_literal(arg)
        return parsed
    if re.search(r"Collections\.empty|List\.of\s*\(\s*\)|Set\.of\s*\(\s*\)|Map\.of\s*\(\s*\)|new\s+\w+\s*\[\s*0\s*\]", arg):
        return {"empty": True, "size": 0}
    return {"raw": arg}


def extract_condition(snippet: str, keyword: str = "if") -> str | None:
    match = re.search(rf"\b{keyword}\s*\(", snippet)
    if not match:
        return None
    open_index = snippet.find("(", match.start())
    close_index = find_matching_paren(snippet, open_index)
    if close_index is None:
        return None
    return snippet[open_index + 1 : close_index].strip()


def value_truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value)
    return None


def simple_condition_outcome(condition: str, env: dict[str, Any]) -> bool | None:
    cond = condition.strip()
    if not cond:
        return None
    if "||" in cond:
        parts = [simple_condition_outcome(part, env) for part in cond.split("||")]
        if any(part is True for part in parts):
            return True
        if all(part is False for part in parts):
            return False
        return None
    if "&&" in cond:
        parts = [simple_condition_outcome(part, env) for part in cond.split("&&")]
        if any(part is False for part in parts):
            return False
        if all(part is True for part in parts):
            return True
        return None
    negated = False
    while cond.startswith("!"):
        negated = not negated
        cond = cond[1:].strip()
        if cond.startswith("(") and cond.endswith(")"):
            cond = cond[1:-1].strip()

    outcome: bool | None = None
    empty_match = re.fullmatch(r"([A-Za-z_]\w*)\.(?:isEmpty|length)\s*\(\s*\)", cond)
    if empty_match and empty_match.group(1) in env:
        value = env[empty_match.group(1)]
        if isinstance(value, str):
            outcome = len(value) == 0
        elif isinstance(value, dict) and "empty" in value:
            outcome = bool(value["empty"])
    length_match = re.fullmatch(r"([A-Za-z_]\w*)\.length\s*(==|!=|<=|>=|<|>)\s*(-?\d+)", cond)
    if outcome is None and length_match and length_match.group(1) in env:
        value = env[length_match.group(1)]
        if isinstance(value, str):
            left = len(value)
            right = int(length_match.group(3))
            outcome = compare_values(left, length_match.group(2), right)
    compare_match = re.fullmatch(
        r"([A-Za-z_]\w*)\s*(==|!=|<=|>=|<|>)\s*(null|true|false|-?\d[\d_]*(?:\.\d[\d_]*)?[lLfFdD]?)",
        cond,
    )
    if outcome is None and compare_match and compare_match.group(1) in env:
        left = env[compare_match.group(1)]
        right = java_literal_value(compare_match.group(3))
        outcome = compare_values(left, compare_match.group(2), right)
    reverse_match = re.fullmatch(
        r"(null|true|false|-?\d[\d_]*(?:\.\d[\d_]*)?[lLfFdD]?)\s*(==|!=|<=|>=|<|>)\s*([A-Za-z_]\w*)",
        cond,
    )
    if outcome is None and reverse_match and reverse_match.group(3) in env:
        left = java_literal_value(reverse_match.group(1))
        right = env[reverse_match.group(3)]
        outcome = compare_values(left, reverse_match.group(2), right)
    if outcome is None and re.fullmatch(r"[A-Za-z_]\w*", cond) and cond in env:
        outcome = value_truthy(env[cond])
    if outcome is not None and negated:
        outcome = not outcome
    return outcome


def compare_values(left: Any, operator: str, right: Any) -> bool | None:
    if isinstance(left, dict) or isinstance(right, dict):
        return None
    try:
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if left is None or right is None:
            return None
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
    except TypeError:
        return None
    return None


def branch_condition_outcomes(
    test_code: str,
    signature: str | None,
    branch_items: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    names = signature_parameter_names(signature)
    method_name = signature_method_name(signature)
    calls = subject_call_arguments(test_code, method_name)
    evidence: dict[int, dict[str, Any]] = {}
    if not names or not calls:
        return evidence
    envs = []
    for args in calls:
        envs.append({name: java_literal_value(args[index]) for index, name in enumerate(names) if index < len(args)})
    for item in branch_items:
        snippet = str(item.get("snippet") or "")
        kind = branch_kind(snippet)
        if kind not in {"if", "else_if"}:
            continue
        condition = extract_condition(snippet, "if")
        if not condition:
            continue
        outcomes = {simple_condition_outcome(condition, env) for env in envs}
        outcomes.discard(None)
        if outcomes:
            evidence[int(item.get("line") or -1)] = {
                "condition": condition,
                "true": True in outcomes,
                "false": False in outcomes,
                "outcome_count": len(outcomes),
            }
    return evidence


def line_start_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", text):
        offsets.append(match.end())
    return offsets


def offset_to_line(offsets: list[int], offset: int) -> int:
    line = 1
    for index, start in enumerate(offsets, start=1):
        if start > offset:
            break
        line = index
    return line


def line_span_for_brace_block(source: str, line_number: int) -> list[int]:
    lines = source.splitlines()
    if line_number < 1 or line_number > len(lines):
        return [line_number]
    offsets = line_start_offsets(source)
    line_start = offsets[line_number - 1]
    line_end = offsets[line_number] if line_number < len(offsets) else len(source)
    open_brace = source.find("{", line_start, line_end)
    if open_brace == -1:
        return [line_number]
    close_brace = find_matching_brace(source, open_brace)
    if close_brace is None:
        return [line_number]
    end_line = offset_to_line(offsets, close_brace)
    return list(range(line_number, end_line + 1))


def line_span_for_control_body(source: str, line_number: int, keyword: str | None = None) -> list[int]:
    lines = source.splitlines()
    if line_number < 1 or line_number > len(lines):
        return [line_number]
    offsets = line_start_offsets(source)
    line_start = offsets[line_number - 1]
    search_start = line_start
    if keyword in {"if", "else_if", "switch", "for", "while", "catch"}:
        open_paren = source.find("(", line_start)
        close_paren = find_matching_paren(source, open_paren) if open_paren != -1 else None
        if close_paren is not None:
            search_start = close_paren + 1
    elif keyword == "else":
        match = re.search(r"\belse\b", source[line_start : offsets[line_number] if line_number < len(offsets) else len(source)])
        if match:
            search_start = line_start + match.end()
    elif keyword == "try":
        match = re.search(r"\btry\b", source[line_start : offsets[line_number] if line_number < len(offsets) else len(source)])
        if match:
            search_start = line_start + match.end()

    index = search_start
    while index < len(source) and source[index].isspace():
        index += 1
    if index < len(source) and source[index] == "{":
        close_brace = find_matching_brace(source, index)
        if close_brace is not None:
            return list(range(line_number, offset_to_line(offsets, close_brace) + 1))
    return [line_number]


def line_span_for_statement(source: str, line_number: int) -> list[int]:
    lines = source.splitlines()
    if line_number < 1 or line_number > len(lines):
        return [line_number]
    offsets = line_start_offsets(source)
    start = offsets[line_number - 1]
    semicolon = source.find(";", start)
    if semicolon == -1:
        return [line_number]
    return list(range(line_number, offset_to_line(offsets, semicolon) + 1))


def private_nested_class_line_ranges(source: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    offsets = line_start_offsets(source)
    for match in re.finditer(r"\bprivate\s+(?:static\s+)?class\s+[A-Za-z_]\w*", source):
        open_brace = source.find("{", match.end())
        if open_brace == -1:
            continue
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None:
            continue
        ranges.append((offset_to_line(offsets, open_brace), offset_to_line(offsets, close_brace)))
    return ranges


def line_in_ranges(line_number: Any, ranges: list[tuple[int, int]]) -> bool:
    return isinstance(line_number, int) and any(start <= line_number <= end for start, end in ranges)


def switch_block_line_ranges(source: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    offsets = line_start_offsets(source)
    for match in re.finditer(r"\bswitch\s*\(", source):
        open_paren = source.find("(", match.start())
        close_paren = find_matching_paren(source, open_paren)
        if close_paren is None:
            continue
        open_brace = source.find("{", close_paren)
        if open_brace == -1:
            continue
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None:
            continue
        ranges.append((offset_to_line(offsets, open_brace), offset_to_line(offsets, close_brace)))
    return ranges


def switch_end_for_case_line(switch_ranges: list[tuple[int, int]], line_number: int) -> int | None:
    for start, end in switch_ranges:
        if start <= line_number <= end:
            return end
    return None


def switch_case_line_spans(items: list[dict[str, Any]], source: str) -> dict[int, list[int]]:
    case_like = [
        item
        for item in items
        if item.get("construct") in {"case", "default"} and isinstance(item.get("line"), int)
    ]
    switch_ranges = switch_block_line_ranges(source)
    spans: dict[int, list[int]] = {}
    for index, item in enumerate(case_like):
        start = int(item["line"])
        end = switch_end_for_case_line(switch_ranges, start) or start
        if index + 1 < len(case_like):
            next_start = int(case_like[index + 1]["line"])
            if switch_end_for_case_line(switch_ranges, next_start) == switch_end_for_case_line(switch_ranges, start):
                end = min(end, next_start - 1)
        spans[start] = list(range(start, max(start, end) + 1))
    return spans


def has_ternary_expression(stripped_line: str) -> bool:
    if "?" not in stripped_line or ":" not in stripped_line:
        return False
    if re.search(r"<[^>\n]*\?[^>\n]*>", stripped_line):
        generic_removed = re.sub(r"<[^>\n]*\?[^>\n]*>", "<>", stripped_line)
        return "?" in generic_removed and ":" in generic_removed
    return not re.search(r"\b(?:class|interface|enum|public|private|protected)\b[^{;]*\([^)]*\)", stripped_line)


def enrich_control_flow_candidate_lines(source: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    case_spans = switch_case_line_spans(items, source)
    private_nested_ranges = private_nested_class_line_ranges(source)
    enriched = []
    for item in items:
        line = item.get("line")
        construct = str(item.get("construct") or "")
        candidate_lines = list(item.get("candidate_pit_lines") or ([line] if isinstance(line, int) else []))
        if isinstance(line, int):
            if construct == "try":
                candidate_lines.extend(line_span_for_control_body(source, line, "try"))
            elif construct == "catch":
                candidate_lines.extend(line_span_for_control_body(source, line, "catch"))
            elif construct in {"case", "default"}:
                candidate_lines.extend(case_spans.get(line, [line]))
            elif construct == "ternary":
                candidate_lines.extend(line_span_for_statement(source, line))
            elif construct == "exception_path" and re.search(r"\bthrow\s+", str(item.get("snippet") or "")):
                candidate_lines.extend(line_span_for_statement(source, line))
        scope = "private_nested" if line_in_ranges(line, private_nested_ranges) else "target_scope"
        enriched.append(
            {
                **item,
                "candidate_pit_lines": sorted({value for value in candidate_lines if isinstance(value, int)}),
                "scope": scope,
                "excluded_from_main_control_flow": scope == "private_nested",
            }
        )
    return enriched


def control_flow_obligation_items(source: str, signature: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    source_lines = source.splitlines()
    current_switch: dict[str, Any] | None = None
    last_condition_line: int | None = None
    for line_number, line in enumerate(source_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("import ", "package ", "//", "/*", "*")):
            continue
        if re.search(r"\belse\s+if\s*\(", stripped):
            condition = extract_condition(stripped, "if")
            items.append({"category": "branch_condition", "signal": "else_if_condition", "construct": "else_if", "condition": condition, "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
            last_condition_line = line_number
            continue
        if re.search(r"\bif\s*\(", stripped):
            condition = extract_condition(stripped, "if")
            items.append({"category": "branch_condition", "signal": "if_condition", "construct": "if", "condition": condition, "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
            last_condition_line = line_number
            continue
        switch_match = re.search(r"\bswitch\s*\(", stripped)
        if switch_match:
            condition = extract_condition(stripped, "switch")
            current_switch = {"condition": condition, "case_values": set()}
            continue
        case_match = re.search(r"\bcase\s+([^:]+)\s*:", stripped)
        if case_match:
            case_value = case_match.group(1).strip()
            if current_switch is not None:
                current_switch["case_values"].add(case_value)
            items.append({"category": "branch_condition", "signal": "case", "construct": "case", "case_value": case_value, "switch_condition": (current_switch or {}).get("condition"), "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
            continue
        if re.search(r"\bdefault\s*:", stripped):
            items.append({"category": "branch_condition", "signal": "default", "construct": "default", "switch_condition": (current_switch or {}).get("condition"), "case_values": sorted((current_switch or {}).get("case_values") or []), "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
            continue
        enhanced_for = re.search(r"\bfor\s*\([^;:]+:\s*[^)]+\)", stripped)
        if enhanced_for:
            items.append({"category": "branch_condition", "signal": "enhanced_for_iteration", "construct": "enhanced_for_iteration", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
        elif re.search(r"\bfor\s*\(|\bwhile\s*\(", stripped):
            condition = extract_condition(stripped, "for") if re.search(r"\bfor\s*\(", stripped) else extract_condition(stripped, "while")
            for outcome in ["true", "false"]:
                items.append({"category": "branch_condition", "signal": f"loop_condition_{outcome}", "construct": "loop_condition", "expected_outcome": outcome, "condition": condition, "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
        if re.search(r"\btry\s*\{", stripped):
            items.append({"category": "branch_condition", "signal": "try_normal", "construct": "try", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
        if re.search(r"\bcatch\s*\(", stripped):
            items.append({"category": "branch_condition", "signal": "catch_exception", "construct": "catch", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
        if re.search(r"\bthrow\b", stripped):
            items.append({"category": "branch_condition", "signal": "exception_path", "construct": "exception_path", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
        if has_ternary_expression(stripped):
            items.append({"category": "branch_condition", "signal": "ternary_true", "construct": "ternary", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
            items.append({"category": "branch_condition", "signal": "ternary_false", "construct": "ternary", "line": line_number, "candidate_pit_lines": [line_number], "snippet": stripped[:240]})
    return enrich_control_flow_candidate_lines(source, items)


def literal_equivalent(left: Any, right_code: str) -> bool:
    if not is_simple_case_literal(right_code):
        return False
    right = java_literal_value(right_code)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, bool) and isinstance(right, bool):
        return left == right
    return str(left) == right_code.strip()


def is_simple_case_literal(case_value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:null|true|false|-?\d[\d_]*(?:\.\d[\d_]*)?[lLfFdD]?|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*",
            case_value,
        )
    )


def switch_case_outcomes(test_code: str, signature: str | None, branch_items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    names = signature_parameter_names(signature)
    method_name = signature_method_name(signature)
    calls = subject_call_arguments(test_code, method_name)
    evidence: dict[int, dict[str, Any]] = {}
    if not names or not calls:
        return evidence
    envs = [{name: java_literal_value(args[index]) for index, name in enumerate(names) if index < len(args)} for args in calls]
    for item in branch_items:
        construct = str(item.get("construct") or "")
        switch_condition = str(item.get("switch_condition") or "").strip()
        if construct not in {"case", "default"} or switch_condition not in names:
            continue
        values = [env[switch_condition] for env in envs if switch_condition in env]
        if construct == "case":
            case_value = str(item.get("case_value") or "")
            if is_simple_case_literal(case_value) and any(literal_equivalent(value, case_value) for value in values):
                evidence[int(item.get("line") or -1)] = {"case_value": case_value, "matched": True}
        else:
            case_values = [str(value) for value in item.get("case_values") or []]
            if case_values and all(is_simple_case_literal(case_value) for case_value in case_values) and values and any(
                not any(literal_equivalent(value, case_value) for case_value in case_values)
                for value in values
            ):
                evidence[int(item.get("line") or -1)] = {"default": True}
    return evidence


def has_meaningful_target_assertion(features: dict[str, Any]) -> bool:
    asserted = features.get("asserted_observable_behaviors") or {}
    return bool(
        int(features.get("meaningful_return_assertion_count") or 0) > 0
        or int(features.get("correct_exception_assertion_count") or 0) > 0
        or asserted.get("state_change")
        or asserted.get("side_effect_or_dependency")
    )


def subject_instance_variables(test_code: str) -> set[str]:
    return set(
        re.findall(
            r"(?:^|[;\{\n]\s*)(?:final\s+)?Subject\s+([A-Za-z_]\w*)\s*=\s*new\s+Subject\s*\(",
            test_code,
            flags=re.MULTILINE,
        )
    )


def subject_call_arguments(test_code: str, method_name: str | None) -> list[list[str]]:
    return [call["args"] for call in subject_calls(test_code, method_name)]


def source_constant_literals(source: str) -> dict[str, str]:
    constants: dict[str, str] = {}
    literal = r'(?:true|false|null|-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
    pattern = re.compile(
        rf"\b(?:public|protected|private|static|final|\s)*\b(?:byte|short|int|long|float|double|boolean|String|char)\s+"
        rf"([A-Za-z_]\w*)\s*=\s*({literal})\s*;",
        flags=re.MULTILINE,
    )
    for name, value in pattern.findall(source):
        constants[name] = value
        constants[f"Subject.{name}"] = value
    return constants


def replace_source_constants(argument: str, constants: dict[str, str]) -> str:
    placeholders: list[str] = []

    def keep_string(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"__STRING_LITERAL_{len(placeholders) - 1}__"

    resolved = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', keep_string, argument)
    for name, value in sorted(constants.items(), key=lambda item: len(item[0]), reverse=True):
        resolved = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w.])", value, resolved)
    for index, literal in enumerate(placeholders):
        resolved = resolved.replace(f"__STRING_LITERAL_{index}__", literal)
    return resolved


def method_body_prefix_before(test_code: str, position: int) -> str:
    window_start = max(0, position - 8000)
    window = test_code[window_start:position]
    best_start = 0
    for marker in [
        "\n    public static void ",
        "\n    private static void ",
        "\n    public void ",
        "\n    private void ",
        "\n    static void ",
        "\n    void ",
    ]:
        marker_pos = window.rfind(marker)
        if marker_pos != -1:
            brace_pos = window.find("{", marker_pos)
            if brace_pos != -1:
                best_start = max(best_start, brace_pos + 1)
    return window[best_start:]


def is_simple_boundary_expression(expr: str) -> bool:
    stripped = expr.strip()
    if re.fullmatch(
        r"(?:null|true|false|Boolean\.(?:TRUE|FALSE)|-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?|"
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')',
        stripped,
    ):
        return True
    if re.fullmatch(r"(?:Integer|Long|Short|Byte)\.(?:MIN_VALUE|MAX_VALUE)", stripped):
        return True
    return bool(
        re.fullmatch(
            r"(?:Collections\.empty\w*|List\.of|Set\.of|Map\.of|Arrays\.asList)\s*\([^;{}]*\)|"
            r"new\s+(?:\w+\.)*\w+(?:<[^>]*>)?\s*\([^;{}]*\)|"
            r"[A-Za-z_]\w*\s*\([^;{}]*\S[^;{}]*\)|"
            r"new\s+\w+\s*\[\s*0\s*\]|"
            r"new\s+\w+\s*\[\s*\]\s*\{[^;{}]*\}",
            stripped,
        )
    )


def is_zero_arg_object_construction(expr: str) -> bool:
    return bool(re.fullmatch(r"new\s+(?:\w+\.)*\w+(?:<[^>]*>)?\s*\(\s*\)", expr.strip()))


def is_nonempty_object_construction(expr: str) -> bool:
    return bool(re.fullmatch(r"new\s+(?:\w+\.)*\w+(?:<[^>]*>)?\s*\([^;{}]*\S[^;{}]*\)", expr.strip()))


def is_simple_nonempty_factory_call(expr: str) -> bool:
    stripped = expr.strip()
    if re.fullmatch(r"(?:Collections\.empty\w*|List\.of|Set\.of|Map\.of|Arrays\.asList)\s*\([^;{}]*\)", stripped):
        return False
    return bool(re.fullmatch(r"[A-Za-z_]\w*\s*\([^;{}]*\S[^;{}]*\)", stripped))


def local_object_mutated_after_setup(prefix: str, declaration_end: int, name: str) -> bool:
    after_decl = prefix[declaration_end:]
    escaped = re.escape(name)
    return bool(
        re.search(rf"\b{escaped}\s*\.\s*[A-Za-z_]\w*\s*=", after_decl)
        or re.search(rf"\b{escaped}\s*\.\s*(?:set[A-Z]\w*|add|put|remove|clear|offer|push)\s*\(", after_decl)
    )


def local_collection_value_expression(after_decl: str, name: str) -> str | None:
    escaped = re.escape(name)
    call_pattern = re.compile(rf"\b{escaped}\s*\.\s*(add|put|clear)\s*\(")
    values: list[str] = []
    saw_mutation = False
    for match in call_pattern.finditer(after_decl):
        method = match.group(1)
        open_paren = after_decl.find("(", match.start())
        close_paren = find_matching_paren(after_decl, open_paren)
        if close_paren is None:
            continue
        semicolon = after_decl.find(";", close_paren)
        next_statement = after_decl.find(";", match.start())
        if semicolon == -1 or next_statement != semicolon:
            continue
        saw_mutation = True
        if method == "clear":
            values = []
            continue
        raw_args = split_java_arguments(after_decl[open_paren + 1 : close_paren])
        if raw_args:
            values.append(raw_args[0].strip())
    if not saw_mutation:
        return None
    return "List.of(" + ", ".join(value for value in values if value) + ")"


def local_boundary_value_expressions(test_code: str, position: int) -> dict[str, str]:
    prefix = method_body_prefix_before(test_code, position)
    values: dict[str, str] = {}
    declarations: list[tuple[int, str, str]] = []
    statement_start = 0
    for match in re.finditer(r";", prefix):
        statement = prefix[statement_start : match.start()].strip()
        statement_start = match.end()
        declaration = re.search(
            r"(?:^|[\{\n]\s*)(?:final\s+)?(?:var|[A-Za-z_][\w.$<>?,\[\]\s]*?)\s+"
            r"([A-Za-z_]\w*)\s*=\s*(.+)$",
            statement,
            flags=re.DOTALL,
        )
        assignment = re.search(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", statement, flags=re.DOTALL)
        if declaration:
            name, expr = declaration.group(1), declaration.group(2).strip()
            declarations.append((statement_start, name, expr))
            if is_simple_boundary_expression(expr):
                if not (is_zero_arg_object_construction(expr) and local_object_mutated_after_setup(prefix, statement_start, name)):
                    values[name] = expr
        elif assignment:
            name, expr = assignment.group(1), assignment.group(2).strip()
            if is_simple_boundary_expression(expr):
                if not (is_zero_arg_object_construction(expr) and local_object_mutated_after_setup(prefix, statement_start, name)):
                    values[name] = expr

    for decl_start, name, expr in declarations:
        if not re.search(
            r"new\s+(?:\w+\.)*(?:ArrayList|LinkedList|HashSet|LinkedHashSet|HashMap|LinkedHashMap|TreeSet|TreeMap)\s*<[^>]*>\s*\(\s*\)|"
            r"new\s+(?:\w+\.)*(?:ArrayList|LinkedList|HashSet|LinkedHashSet|HashMap|LinkedHashMap|TreeSet|TreeMap)\s*\(\s*\)|"
            r"(?:Collections\.empty\w*|List\.of|Set\.of|Map\.of)\s*\(\s*\)",
            expr,
        ):
            continue
        after_decl = prefix[decl_start:]
        collection_expr = local_collection_value_expression(after_decl, name)
        if collection_expr is not None:
            values[name] = collection_expr
        else:
            values[name] = "List.of()"
    return values


def replace_local_boundary_variables(argument: str, local_values: dict[str, str]) -> str:
    arg = argument.strip()
    if re.fullmatch(r"[A-Za-z_]\w*", arg) and arg in local_values:
        return local_values[arg]
    return argument


def simple_iterable_literal_elements(expr: str) -> list[str]:
    stripped = expr.strip()
    array_match = re.fullmatch(
        r"new\s+(?:[A-Za-z_][\w.$<>?]*\s*)?(?:\[\s*\])?\s*\{\s*(.*)\s*\}",
        stripped,
        flags=re.DOTALL,
    )
    if not array_match:
        array_match = re.fullmatch(
            r"new\s+[A-Za-z_][\w.$<>?]*\s*\[\s*\]\s*\{\s*(.*)\s*\}",
            stripped,
            flags=re.DOTALL,
        )
    call_match = re.fullmatch(
        r"(?:java\.util\.)?(?:Arrays\.asList|List\.(?:<[^>]+>)?of|Set\.(?:<[^>]+>)?of)\s*\(\s*(.*)\s*\)",
        stripped,
        flags=re.DOTALL,
    )
    body = ""
    if array_match:
        body = array_match.group(1)
    elif call_match:
        body = call_match.group(1)
    else:
        return []
    if not body.strip():
        return []
    elements = [part.strip() for part in split_java_arguments(body) if part.strip()]
    return [element for element in elements if is_simple_boundary_expression(element)]


def local_enhanced_for_value_expressions(test_code: str, position: int) -> dict[str, list[str]]:
    local_values = local_boundary_value_expressions(test_code, position)
    loop_values: dict[str, list[str]] = {}
    loop_header = re.compile(
        r"\bfor\s*\(\s*(?:final\s+)?(?:var|[A-Za-z_][\w.$<>?,\[\]\s]*?)\s+"
        r"([A-Za-z_]\w*)\s*:\s*([^)]+?)\s*\)\s*\{",
        flags=re.DOTALL,
    )
    for match in loop_header.finditer(test_code):
        open_brace = test_code.find("{", match.end() - 1)
        close_brace = find_matching_brace(test_code, open_brace)
        if close_brace is None or not (open_brace < position < close_brace):
            continue
        loop_var = match.group(1)
        iterable_expr = replace_local_boundary_variables(match.group(2).strip(), local_values)
        elements = simple_iterable_literal_elements(iterable_expr)
        if elements:
            loop_values[loop_var] = elements
    return loop_values


def helper_target_call_arguments(test_code: str, method_name: str | None) -> list[list[str]]:
    if not method_name:
        return []
    helper_specs: list[dict[str, Any]] = []
    method_header = re.compile(
        r"(?:^|[;\{\}\n]\s*)(?:private|public|protected)?\s*(?:static\s+)?"
        r"[A-Za-z_][\w.$<>?,\[\]\s]*\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*"
        r"(?:throws\s+[A-Za-z_][\w.$]*(?:\s*,\s*[A-Za-z_][\w.$]*)*)?\s*\{",
        flags=re.MULTILINE,
    )
    for match in method_header.finditer(test_code):
        helper_name = match.group(1)
        if helper_name in {method_name, "main"}:
            continue
        open_brace = test_code.find("{", match.end() - 1)
        close_brace = find_matching_brace(test_code, open_brace)
        if close_brace is None:
            continue
        body = test_code[open_brace + 1 : close_brace]
        target_calls = subject_calls(body, method_name)
        if len(target_calls) != 1:
            continue
        params = signature_parameter_names(f"void __helper({match.group(2)})")
        if not params:
            continue
        helper_specs.append(
            {
                "name": helper_name,
                "start": match.start(),
                "end": close_brace + 1,
                "params": params,
                "target_args": target_calls[0]["args"],
            }
        )

    expanded: list[list[str]] = []
    for spec in helper_specs:
        call_pattern = re.compile(rf"\b{re.escape(spec['name'])}\s*\(")
        for call_match in call_pattern.finditer(test_code):
            if spec["start"] <= call_match.start() <= spec["end"]:
                continue
            open_index = test_code.find("(", call_match.start())
            close_index = find_matching_paren(test_code, open_index)
            if close_index is None:
                continue
            helper_args = split_java_arguments(test_code[open_index + 1 : close_index])
            if len(helper_args) < len(spec["params"]):
                continue
            local_values = local_boundary_value_expressions(test_code, call_match.start())
            env = {
                name: replace_local_boundary_variables(helper_args[index], local_values)
                for index, name in enumerate(spec["params"])
            }
            resolved_args = []
            for target_arg in spec["target_args"]:
                arg = target_arg.strip()
                if re.fullmatch(r"[A-Za-z_]\w*", arg) and arg in env:
                    resolved_args.append(env[arg])
                else:
                    resolved_args.append(arg)
            expanded.append(resolved_args)
    return expanded


def target_call_argument_records_with_simple_helpers(test_code: str, method_name: str | None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in subject_calls(test_code, method_name):
        local_values = local_boundary_value_expressions(test_code, int(call["start"]))
        loop_values = local_enhanced_for_value_expressions(test_code, int(call["start"]))
        resolved_args = [replace_local_boundary_variables(arg, local_values) for arg in call["args"]]
        variants = [resolved_args]
        for arg_index, arg in enumerate(resolved_args):
            stripped = arg.strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", stripped) or stripped not in loop_values:
                continue
            variants = [
                [*variant[:arg_index], element, *variant[arg_index + 1 :]]
                for variant in variants
                for element in loop_values[stripped]
            ]
        for variant in variants:
            calls.append(
                {
                    "args": call["args"],
                    "resolved_args": variant,
                }
            )
    for args in helper_target_call_arguments(test_code, method_name):
        calls.append({"args": args, "resolved_args": args})
    return calls


def target_call_arguments_with_simple_helpers(test_code: str, method_name: str | None) -> list[list[str]]:
    return [record["resolved_args"] for record in target_call_argument_records_with_simple_helpers(test_code, method_name)]


def is_positive_normal_argument(arg: str, categories: set[str]) -> bool:
    if not arg:
        return False
    if categories & {
        "null",
        "empty",
        "false_value",
        "true_value",
        "zero",
        "negative",
    }:
        return False
    if re.fullmatch(r"null|true|false|Boolean\.(?:TRUE|FALSE)", arg):
        return False
    numeric_match = re.fullmatch(r"\+?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?", arg)
    if numeric_match:
        value = parse_numeric_literal(arg)
        return value is not None and 0 < value < 2147483647
    string_values = java_string_literal_values(arg)
    if string_values:
        return any(0 < len(value) < 80 and not has_unicode_boundary_text(value) for value in string_values)
    if re.search(r"List\.of\s*\(\s*\)|Arrays\.asList\s*\(\s*\)|Collections\.empty|new\s+\w+\s*\[\s*0\s*\]|new\s+\w+\s*\[\s*\]\s*\{\s*\}", arg):
        return False
    if re.search(r"List\.of\s*\([^)]*\S[^)]*\)|Arrays\.asList\s*\([^)]*\S[^)]*\)|new\s+\w+\s*\[\s*\]\s*\{[^}]+\}", arg):
        return True
    if is_zero_arg_object_construction(arg):
        return False
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", arg):
        return False
    return bool(re.search(r"\w", arg))


def is_direct_boolean_parameter_type(param_type: str | None) -> bool:
    return bool(re.fullmatch(r"(?:boolean|Boolean|java\.lang\.Boolean)", (param_type or "").strip()))


def is_primitive_parameter_type(param_type: str | None) -> bool:
    t = re.sub(r"\s+", "", param_type or "")
    if not t or "[]" in t:
        return False
    return t in {"byte", "short", "int", "long", "float", "double", "char", "boolean"}


def is_string_parameter_type(param_type: str | None) -> bool:
    t = re.sub(r"\s+", "", param_type or "")
    return bool(re.fullmatch(r"(?:java\.lang\.)?(?:String|CharSequence)", t))


def is_class_parameter_type(param_type: str | None) -> bool:
    t = re.sub(r"\s+", "", param_type or "")
    return bool(re.fullmatch(r"(?:java\.lang\.)?Class(?:<.*>)?", t))


def is_collection_or_array_parameter_type(param_type: str | None) -> bool:
    t = (param_type or "").lower()
    return any(
        re.search(rf"(?:^|[.<\s]){name}(?:$|[<>\s,])", t)
        for name in ["list", "arraylist", "linkedlist", "set", "hashset", "treeset", "map", "hashmap", "treemap", "collection", "iterable", "iterator", "queue", "deque"]
    ) or "[]" in t


def is_numeric_parameter_type(param_type: str | None) -> bool:
    t = re.sub(r"\s+", "", param_type or "")
    if not t or "[]" in t or is_collection_or_array_parameter_type(param_type):
        return False
    t = re.sub(r"<.*>$", "", t)
    base = t.split(".")[-1]
    return base in {
        "byte",
        "short",
        "int",
        "long",
        "float",
        "double",
        "Byte",
        "Short",
        "Integer",
        "Long",
        "Float",
        "Double",
        "BigDecimal",
        "BigInteger",
    }


def is_general_object_parameter_type(param_type: str | None) -> bool:
    return bool(param_type) and not (
        is_primitive_parameter_type(param_type)
        or is_numeric_parameter_type(param_type)
        or is_direct_boolean_parameter_type(param_type)
        or is_string_parameter_type(param_type)
        or is_collection_or_array_parameter_type(param_type)
    )


def concrete_numeric_argument_categories(argument: str) -> set[str]:
    arg = argument.strip()
    categories: set[str] = set()
    if re.fullmatch(r"(?:Integer|Long|Short|Byte)\.MIN_VALUE", arg):
        return {"negative"}
    if re.fullmatch(r"(?:Integer|Long|Short|Byte)\.MAX_VALUE", arg):
        return set()
    value_of_match = re.fullmatch(
        r"(?:Integer|Long|Short|Byte|Double|Float|BigInteger|BigDecimal)\.valueOf\s*\(\s*([+-]?\s*\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?)\s*\)",
        arg,
    )
    if value_of_match:
        arg = value_of_match.group(1)
    if not re.fullmatch(r"[+-]?\s*\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?", arg):
        return categories
    value = parse_numeric_literal(arg)
    if value is None:
        return categories
    if value == 0:
        categories.add("zero")
    elif value < 0:
        categories.add("negative")
    return {category for category in categories if category in BOUNDARY_CATEGORIES}


def boundary_categories_from_argument(
    argument: str,
    constraints: dict[str, Any] | None = None,
    param_type: str | None = None,
) -> set[str]:
    categories: set[str] = set()
    arg = argument.strip()
    constraints = constraints or {"string_max_lengths": [], "numeric_bounds": []}
    if not arg:
        return categories
    if re.fullmatch(r"null", arg):
        return {"null"}
    if is_numeric_parameter_type(param_type):
        return concrete_numeric_argument_categories(arg)
    if is_direct_boolean_parameter_type(param_type):
        if re.fullmatch(r"(?:false|Boolean\.FALSE)", arg):
            return {"false_value"}
        if re.fullmatch(r"(?:true|Boolean\.TRUE)", arg):
            return {"true_value"}
        return categories
    if is_general_object_parameter_type(param_type) and (
        is_nonempty_object_construction(arg) or is_simple_nonempty_factory_call(arg)
    ):
        return set()
    if is_class_parameter_type(param_type) and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.class", arg):
        return set()
    empty_string = re.fullmatch(r'""|String\.valueOf\s*\(\s*""\s*\)', arg)
    empty_collection = re.fullmatch(
        r"(?:java\.util\.)?Collections\.(?:<[^>]+>)?empty\w*\s*\(\s*\)|"
        r"(?:java\.util\.)?List\.(?:<[^>]+>)?of\s*\(\s*\)|"
        r"(?:java\.util\.)?Arrays\.asList\s*\(\s*\)|"
        r"new\s+(?:java\.util\.)?(?:ArrayList|LinkedList|HashSet|TreeSet|HashMap|TreeMap|LinkedHashMap|LinkedHashSet|Vector|Stack|PriorityQueue|ArrayDeque)(?:\s*<[^>]*>)?\s*\(\s*\)|"
        r"new\s+\w+\s*\[\s*0\s*\]|new\s+\w+\s*\[\s*\]\s*\{\s*\}",
        arg,
    )
    if param_type is None:
        if empty_string or empty_collection:
            categories.add("empty")
    elif (is_string_parameter_type(param_type) and empty_string) or (is_collection_or_array_parameter_type(param_type) and empty_collection):
        categories.add("empty")
    string_values = java_string_literal_values(arg)
    allow_embedded_numeric_literals = not is_collection_or_array_parameter_type(param_type)
    if allow_embedded_numeric_literals and re.search(r"(?<![\w.])0(?:[lLfFdD])?(?![\w.])", arg):
        categories.add("zero")
    if is_direct_boolean_parameter_type(param_type) and re.search(r"\bfalse\b|Boolean\.FALSE\b", arg):
        categories.add("false_value")
    if is_direct_boolean_parameter_type(param_type) and re.search(r"\btrue\b|Boolean\.TRUE\b", arg):
        categories.add("true_value")
    if allow_embedded_numeric_literals and re.search(r"(?<![\w.])-\s*[1-9]\d*(?:[lLfFdD])?(?![\w.])", arg):
        categories.add("negative")
    if is_positive_normal_argument(arg, categories):
        categories.add("positive_normal")
    return {category for category in categories if category in BOUNDARY_CATEGORIES}


def inferred_bound_key(bound: dict[str, Any]) -> str:
    return f"{bound.get('kind')}:{bound.get('operator')}:{bound.get('value')}:{bound.get('source')}"


def inferred_string_literals_key(bounds: list[dict[str, Any]]) -> str:
    values = sorted({str(bound.get("value") or "") for bound in bounds if bound.get("kind") == "string"})
    digest = hashlib.sha1(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    return f"string_literals_all:{digest}"


def inferred_bound_hits_from_argument(argument: str, bounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not bounds:
        return hits
    numbers = numeric_literals(argument)
    strings = java_string_literal_values(argument)
    string_bounds = [bound for bound in bounds if bound.get("kind") == "string"]
    string_values = {str(bound.get("value") or "") for bound in string_bounds}
    for bound in bounds:
        bound_key = inferred_bound_key(bound)
        if bound.get("kind") == "number":
            try:
                value = float(bound["value"])
            except (TypeError, ValueError):
                continue
            for number in numbers:
                if number < value:
                    hits.append({"category": "below_inferred_bound", "bound_key": bound_key, "bound": bound})
                elif number == value:
                    hits.append({"category": "at_inferred_bound", "bound_key": bound_key, "bound": bound})
                elif number > value:
                    hits.append({"category": "above_inferred_bound", "bound_key": bound_key, "bound": bound})
        elif bound.get("kind") == "string":
            value = str(bound.get("value") or "")
            for string in strings:
                if string == value:
                    hits.append({"category": "same_inferred_bound", "bound_key": bound_key, "bound": bound})
    if string_bounds:
        group_key = inferred_string_literals_key(string_bounds)
        for string in strings:
            if string not in string_values:
                hits.append(
                    {
                        "category": "different_inferred_bound",
                        "bound_key": group_key,
                        "bound": {
                            "kind": "string",
                            "operator": "not_equal_to_all_literals",
                            "values": sorted(string_values),
                            "source": "all_string_literals_for_parameter",
                        },
                    }
                )
    deduped: list[dict[str, Any]] = []
    seen = set()
    for hit in hits:
        key = (hit["category"], hit["bound_key"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def inferred_bound_categories_from_argument(argument: str, bounds: list[dict[str, Any]]) -> set[str]:
    return {hit["category"] for hit in inferred_bound_hits_from_argument(argument, bounds)}


def target_result_variables(test_code: str, method_name: str | None) -> set[str]:
    if not method_name:
        return set()
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    pattern = re.compile(
        rf"(?:^|[;\{{]\s*)(?:final\s+)?(?:var|[A-Za-z_][\w.$<>?,\[\]\s]*)\s+"
        rf"([A-Za-z_]\w*)\s*=\s*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\(",
        re.MULTILINE,
    )
    return set(pattern.findall(test_code))


def statement_windows(text: str) -> list[str]:
    windows = []
    for part in re.split(r";|\n\s*\n", text):
        stripped = part.strip()
        if stripped:
            windows.append(stripped)
    return windows


def has_target_return_assertion(test_code: str, method_name: str | None) -> bool:
    if not method_name:
        return False
    assertion_code = visible_assertion_code(test_code)
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    inline_assert = re.search(
        rf"\bassert\w*\s*\([^;{{}}]*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\(",
        assertion_code,
        flags=re.DOTALL,
    )
    inline_failure_guard = re.search(
        rf"\bif\s*\([^;{{}}]*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\([^;{{}}]*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
        assertion_code,
        flags=re.DOTALL,
    )
    if inline_assert or inline_failure_guard:
        return True
    if helper_assertion_calls_touching_target(test_code, method_name):
        return True
    result_vars = target_result_variables(test_code, method_name)
    if not result_vars:
        return False
    for var in result_vars:
        if re.search(
            rf"\bassert\w*\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*(?:==|!=|<|>|\.equals\s*\(|instanceof|\.size\s*\(|\.length|get\w*\s*\()",
            assertion_code,
            flags=re.DOTALL,
        ):
            return True
        if re.search(
            rf"\bif\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*(?:==|!=|<|>|\.equals\s*\(|instanceof|\.size\s*\(|\.length|get\w*\s*\()[^{{}};]*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
            assertion_code,
            flags=re.DOTALL,
        ):
            return True
    return False


def count_target_return_assertions(test_code: str, method_name: str | None) -> int:
    if not method_name:
        return 0
    assertion_code = visible_assertion_code(test_code)
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    count = len(
        re.findall(
            rf"\bassert\w*\s*\([^;{{}}]*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\(",
            assertion_code,
            flags=re.DOTALL,
        )
    )
    count += len(
        re.findall(
            rf"\bif\s*\([^;{{}}]*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\([^;{{}}]*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
            assertion_code,
            flags=re.DOTALL,
        )
    )
    count += len(helper_assertion_calls_touching_target(test_code, method_name))
    for var in target_result_variables(test_code, method_name):
        count += len(
            re.findall(
                rf"\bassert\w*\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*(?:==|!=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|endsWith\s*\(|instanceof|\.size\s*\(|\.length|get\w*\s*\()",
                assertion_code,
                flags=re.DOTALL,
            )
        )
        count += len(
            re.findall(
                rf"\bif\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*(?:==|!=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|endsWith\s*\(|instanceof|\.size\s*\(|\.length|get\w*\s*\()[^{{}};]*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
                assertion_code,
                flags=re.DOTALL,
            )
        )
    return count


def target_call_argument_variants(test_code: str, method_name: str | None) -> int:
    variants = {
        tuple(args)
        for args in subject_call_arguments(test_code, method_name)
    }
    return len(variants)


def count_meaningful_target_return_assertions(test_code: str, method_name: str | None) -> int:
    """Count target-return checks that inspect value/content, not only existence/type."""
    if not method_name:
        return 0
    assertion_code = visible_assertion_code(test_code)
    count = 0
    meaningful_assert_names = r"assert(?:Equals|ArrayEquals|IterableEquals|Same|NotEquals|That)"
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    count += len(
        re.findall(
            rf"\b{meaningful_assert_names}\s*\([^;{{}}]*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\(",
            assertion_code,
            flags=re.DOTALL,
        )
    )
    count += len(
        re.findall(
            rf"\bcheck\s*\(\s*!?\s*(?:{receiver_pattern})\.{re.escape(method_name)}\s*\(",
            assertion_code,
            flags=re.DOTALL,
        )
    )
    count += sum(
        1
        for call in helper_assertion_calls_touching_target(test_code, method_name)
        if helper_call_is_meaningful_return_oracle(call)
    )
    for var in target_result_variables(test_code, method_name):
        count += len(
            re.findall(
                rf"\b{meaningful_assert_names}\s*\([^;{{}}]*\b{re.escape(var)}\b",
                assertion_code,
                flags=re.DOTALL,
            )
        )
        count += len(
            re.findall(
                rf"\bcheck\s*\(\s*!?\s*\b{re.escape(var)}\b\s*,",
                assertion_code,
                flags=re.DOTALL,
            )
        )
        count += len(
            re.findall(
                rf"\bcheck\s*\(\s*!?\s*\b{re.escape(var)}\b\.(?:is|has)[A-Z]\w*\b",
                assertion_code,
                flags=re.DOTALL,
            )
        )
        count += len(
            re.findall(
                rf"(?:assertTrue|assertFalse)\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*"
                rf"(?:==|!=|<=|>=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|"
                rf"endsWith\s*\(|\.isEmpty\s*\(|\.size\s*\(|\.length|get\w*\s*\()",
                assertion_code,
                flags=re.DOTALL,
            )
        )
        count += len(
            re.findall(
                rf"\bif\s*\([^;{{}}]*\b{re.escape(var)}\b[^;{{}}]*"
                rf"(?:==|!=|<=|>=|<|>|\.equals\s*\(|contains\s*\(|matches\s*\(|startsWith\s*\(|"
                rf"endsWith\s*\(|\.isEmpty\s*\(|\.size\s*\(|\.length|get\w*\s*\()[^{{}};]*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
                assertion_code,
                flags=re.DOTALL,
            )
        )
    return count


def count_weak_target_return_assertions(test_code: str, method_name: str | None) -> int:
    """Count visible target-return checks that only establish existence/type/trivial shape."""
    if not method_name:
        return 0
    assertion_code = visible_assertion_code(test_code)
    count = 0
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    target_call = rf"(?:{receiver_pattern})\.{re.escape(method_name)}\s*\("
    weak_targets = [
        ("call", target_call),
        *[("var", re.escape(var)) for var in target_result_variables(test_code, method_name)],
    ]
    count += sum(
        1
        for call in helper_assertion_calls_touching_target(test_code, method_name)
        if helper_call_is_weak_return_oracle(call)
    )
    assertion_lines = [
        line
        for line in assertion_code.splitlines()
        if re.search(r"\b(?:assert|check|fail|if)\b", line) or "AssertionError" in line
    ]
    for target_kind, target in weak_targets:
        target_expr = target if target_kind == "call" else rf"\b{target}\b"
        for line in assertion_lines:
            if not re.search(target_expr, line):
                continue
            if re.search(r"\bassert(?:NotNull|InstanceOf)\s*\(", line):
                count += 1
            elif re.search(r"\bassertTrue\s*\(", line) and re.search(r"!=\s*null|\.size\s*\(\s*\)\s*>\s*0|\.length\s*>\s*0", line):
                count += 1
            elif re.search(r"\bassertFalse\s*\(", line) and "== null" in line:
                count += 1
            elif re.search(r"\bif\s*\(", line) and "== null" in line and re.search(r"AssertionError\b|fail\s*\(", line):
                count += 1
    return count


def has_target_state_assertion(test_code: str, method_name: str | None) -> bool:
    if not method_name or not subject_calls(test_code, method_name):
        return False
    result_vars = target_result_variables(test_code, method_name)
    state_patterns = [
        r"getDeclaredField|\.get\w*\s*\(|\.size\s*\(|\.length\b",
        r"assert\w*\s*\([^;]*(?:state|count|value|field|size)",
    ]
    if any(re.search(pattern, test_code, flags=re.MULTILINE) for pattern in state_patterns):
        return True
    return any(re.search(rf"\b{re.escape(var)}\b\.\w+\s*\(", test_code) for var in result_vars)


def has_explicit_state_sequence_assertion(test_code: str, method_name: str | None, semantic_assertions: int) -> bool:
    if not method_name:
        return False
    calls = subject_calls(test_code, method_name)
    if len(calls) < 2:
        return False
    sequence_language = bool(
        re.search(
            r"\b(?:before|after|initial|final|again|second|third|then|sequence|previous|next|"
            r"repeated|twice|idempotent|accumulat|reset|transition)\b",
            test_code,
            flags=re.I,
        )
    )
    multiple_state_assertions = semantic_assertions >= 2 and bool(
        re.search(
            r"(?:assert\w*|if)\s*\([^;]*(?:state|count|value|field|size|get\w*\s*\(|\.size\s*\(|\.length)",
            test_code,
            flags=re.I | re.DOTALL,
        )
    )
    return sequence_language or multiple_state_assertions


def state_setup_and_assertion_items(test_code: str, method_name: str | None, state_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calls = subject_calls(test_code, method_name)
    first_call_start = min((int(call["start"]) for call in calls), default=len(test_code))
    before_call = test_code[:first_call_start]
    after_call = test_code[first_call_start:]
    setup_items = []
    asserted_items = []
    for item in state_items:
        target = str(item.get("state_target") or "")
        if not target:
            continue
        capitalized = target[:1].upper() + target[1:]
        setup_pattern = re.compile(
            rf"\b{re.escape(target)}\s*=|\.set{re.escape(capitalized)}\s*\(|getDeclaredField\s*\(\s*\"{re.escape(target)}\"|"
            rf"\bSubject\s*\([^)]*\S[^)]*\)",
            re.MULTILINE,
        )
        assertion_pattern = re.compile(
            rf"\bassert\w*\s*\([^;]*(?:{re.escape(target)}|get{re.escape(capitalized)}|is{re.escape(capitalized)}|has{re.escape(capitalized)}|"
            rf"getDeclaredField\s*\(\s*\"{re.escape(target)}\")|"
            rf"(?:get{re.escape(capitalized)}|is{re.escape(capitalized)}|has{re.escape(capitalized)})\s*\(",
            re.MULTILINE | re.DOTALL,
        )
        item_out = {**item}
        if setup_pattern.search(before_call) or setup_pattern.search(test_code):
            setup_items.append(item_out)
        if assertion_pattern.search(after_call):
            asserted_items.append(item_out)
    return setup_items, asserted_items


def state_setup_categories_for_target(test_code: str, target: str, before_call_end: int) -> set[str]:
    before_call = test_code[:before_call_end]
    escaped_target = re.escape(target)
    categories: set[str] = set()
    assignment_patterns = [
        rf"\b{escaped_target}\s*=\s*([^;]+);",
        rf"\bSubject\.{escaped_target}\s*=\s*([^;]+);",
        rf"\b{escaped_target}\.(?:add|put|remove|clear|set)\s*\(([^;]*)\)\s*;",
    ]
    simple_target = target.split(".")[-1]
    if simple_target != target:
        escaped_simple = re.escape(simple_target)
        assignment_patterns.extend(
            [
                rf"\b{escaped_simple}\s*=\s*([^;]+);",
                rf"\bSubject\.{escaped_simple}\s*=\s*([^;]+);",
            ]
        )
    for pattern in assignment_patterns:
        for match in re.finditer(pattern, before_call, flags=re.MULTILINE | re.DOTALL):
            value = match.group(1) if match.lastindex else match.group(0)
            categories.update(boundary_categories_from_argument(value))
            if ".clear" in match.group(0):
                categories.add("empty")
            if re.search(r"\.(?:add|put|set)\s*\(", match.group(0)):
                categories.add("positive_normal")
    reflection_pattern = re.compile(
        rf"getDeclaredField\s*\(\s*\"{re.escape(simple_target)}\"\s*\).*?"
        r"\.set(?:Int|Long|Boolean|Double|Float|Object)?\s*\([^,]+,\s*([^)]+)\)",
        re.MULTILINE | re.DOTALL,
    )
    for match in reflection_pattern.finditer(before_call):
        categories.update(boundary_categories_from_argument(match.group(1)))
    return categories


def state_assertion_for_target(test_code: str, method_name: str | None, target: str) -> bool:
    calls = subject_calls(test_code, method_name)
    first_call_start = min((int(call["start"]) for call in calls), default=len(test_code))
    after_call = test_code[first_call_start:]
    simple_target = target.split(".")[-1]
    capitalized = simple_target[:1].upper() + simple_target[1:]
    assertion_pattern = re.compile(
        rf"\bassert\w*\s*\([^;]*(?:{re.escape(target)}|{re.escape(simple_target)}|"
        rf"get{re.escape(capitalized)}|is{re.escape(capitalized)}|has{re.escape(capitalized)}|"
        rf"getDeclaredField\s*\(\s*\"{re.escape(simple_target)}\")",
        re.MULTILINE | re.DOTALL,
    )
    return bool(assertion_pattern.search(after_call))


def state_case_setup_and_assertion_items(
    test_code: str,
    method_name: str | None,
    state_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calls = subject_calls(test_code, method_name)
    first_call_start = min((int(call["start"]) for call in calls), default=len(test_code))
    setup_items = []
    asserted_items = []
    for item in state_items:
        target = str(item.get("state_target") or "")
        case = str(item.get("state_case") or "")
        if not target or not case:
            continue
        setup_categories = state_setup_categories_for_target(test_code, target, first_call_start)
        has_setup = case in setup_categories
        has_assertion = state_assertion_for_target(test_code, method_name, target)
        item_out = {**item, "setup_categories": sorted(setup_categories)}
        if has_setup:
            setup_items.append(item_out)
        if has_setup and has_assertion:
            asserted_items.append(item_out)
    return setup_items, asserted_items


def state_sequence_order_score(test_code: str, method_name: str | None, state_items: list[dict[str, Any]]) -> float | None:
    if not state_items:
        return None
    calls = subject_calls(test_code, method_name)
    explicit_sequence = bool(
        re.search(
            r"\b(?:before|after|initial|final|again|second|third|then|sequence|previous|next|"
            r"repeated|twice|idempotent|accumulat|reset|transition|order)\b",
            test_code,
            flags=re.I,
        )
    )
    if len(calls) >= 2 and explicit_sequence:
        return 1.0
    if len(calls) >= 2:
        return 0.5
    return 0.0


def has_target_interaction_evidence(test_code: str, method_name: str | None) -> bool:
    if not method_name or not subject_calls(test_code, method_name):
        return False
    return bool(INTERACTION_TEST_RE.search(test_code))


def is_broad_exception_type(exception_type: str) -> bool:
    simple = exception_type.split(".")[-1]
    return simple in {"Exception", "Throwable", "Error"}


def typed_exception_assertion_helper_names(test_code: str) -> set[str]:
    """Find local assert-throws helpers that verify the requested exception type."""
    helpers: set[str] = set()
    methods = java_method_definitions(visible_assertion_code(test_code))
    type_predicates = {
        str(method.get("name") or "")
        for method in methods
        if re.search(r"(?:Exception|Error|Throwable)", str(method.get("name") or ""))
        and re.search(r"\.isInstance\s*\(", str(method.get("body") or ""))
    }
    for method in methods:
        args = str(method.get("args") or "")
        body = str(method.get("body") or "")
        has_expected_type = bool(
            re.search(r"\bClass\s*<[^>]*(?:Throwable|Exception|Error)[^>]*>\s+expected\b", args)
            or re.search(r"\bClass\s*<[^>]+>\s+expected\b", args)
        )
        verifies_type = bool(
            re.search(r"\bexpected\.isInstance\s*\(", body)
            or re.search(r"\bexpected\s*(?:==|\.equals\s*\()\s*\w+\.getClass\s*\(", body)
        )
        verifies_named_type = any(
            re.search(rf"\b{re.escape(predicate)}\s*\(", body)
            for predicate in type_predicates
        )
        catches_exception = bool(re.search(r"\bcatch\s*\([^)]*(?:Throwable|Exception|Error)\b", body))
        fails_when_missing = bool(re.search(r"\b(?:throw\s+new\s+AssertionError|fail\s*\()", body))
        if catches_exception and fails_when_missing and (
            (has_expected_type and verifies_type) or verifies_named_type
        ):
            helpers.add(str(method.get("name") or ""))
    return helpers


def captured_exception_message_oracle_count(test_code: str, method_name: str | None) -> int:
    """Count target exceptions returned by a helper and checked by exact message."""
    if not method_name:
        return 0
    assertion_code = visible_assertion_code(test_code)
    methods = java_method_definitions(assertion_code)
    capture_helpers: set[str] = set()
    for method in methods:
        name = str(method.get("name") or "")
        body = str(method.get("body") or "")
        caught = re.search(r"\bcatch\s*\([^)]*(?:Throwable|Exception|Error)\s+(\w+)\s*\)", body)
        if (
            re.search(r"(?:expect|capture|assert|check).*(?:Exception|Error|Throw)", name, re.I)
            and caught
            and re.search(rf"\breturn\s+{re.escape(caught.group(1))}\s*;", body)
            and re.search(r"\b(?:throw\s+new\s+AssertionError|fail\s*\()", body)
        ):
            capture_helpers.add(name)
    if not capture_helpers:
        return 0
    target_call_pattern = re.compile(
        rf"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*\.{re.escape(method_name)}\s*\("
    )
    count = 0
    for method in methods:
        body = str(method.get("body") or "")
        for helper_name in capture_helpers:
            for match in re.finditer(rf"\b{re.escape(helper_name)}\s*\(", body):
                open_paren = body.find("(", match.start())
                close_paren = find_matching_paren(body, open_paren)
                if close_paren is None or not target_call_pattern.search(body[match.start() : close_paren + 1]):
                    continue
                assignment = re.search(
                    r"(?:[A-Za-z_][\w.$<>?, ]*\s+)?([A-Za-z_]\w*)\s*=\s*$",
                    body[max(0, match.start() - 160) : match.start()],
                )
                if not assignment:
                    continue
                var = re.escape(assignment.group(1))
                after_call = body[close_paren + 1 :]
                exact_message_check = bool(
                    re.search(
                        rf"\b(?:assertEquals|checkEquals)\s*\(\s*\"[^\"]+\"\s*,\s*{var}\.getMessage\s*\(\s*\)",
                        after_call,
                        flags=re.DOTALL,
                    )
                    or re.search(
                        rf"\"[^\"]+\"\.equals\s*\(\s*{var}\.getMessage\s*\(\s*\)\s*\)",
                        after_call,
                        flags=re.DOTALL,
                    )
                )
                if exact_message_check:
                    count += 1
    return count


def target_exception_evidence(test_code: str, method_name: str | None) -> dict[str, Any]:
    if not method_name:
        return {
            "has_target_exception_test": False,
            "target_exception_assertions": 0,
            "target_strong_exception_assertions": 0,
            "target_weak_exception_assertions": 0,
            "target_weak_exception_handlers": 0,
        }
    receivers = ["Subject", *sorted(subject_instance_variables(test_code))]
    receiver_pattern = "|".join(re.escape(receiver) for receiver in receivers)
    target_call_pattern = rf"(?:{receiver_pattern})\.{re.escape(method_name)}\s*\("
    assertion_count = 0
    strong_count = 0
    weak_assertion_count = 0
    weak_handlers = 0
    throw_assertion_names = {"assertThrows", *typed_exception_assertion_helper_names(test_code)}
    for helper_name in sorted(throw_assertion_names):
        helper_pattern = re.compile(rf"\b{re.escape(helper_name)}\s*\(")
        for match in helper_pattern.finditer(test_code):
            close = find_matching_paren(test_code, test_code.find("(", match.start()))
            if close is not None and re.search(target_call_pattern, test_code[match.start() : close + 1]):
                assertion_count += 1
                call_text = test_code[match.start() : close + 1]
                type_match = re.search(
                    rf"{re.escape(helper_name)}\s*\(\s*([A-Za-z_][\w.]*)\.class",
                    call_text,
                )
                if helper_name != "assertThrows" or (
                    type_match and not is_broad_exception_type(type_match.group(1))
                ):
                    strong_count += 1
                else:
                    weak_assertion_count += 1
    captured_message_oracles = captured_exception_message_oracle_count(test_code, method_name)
    assertion_count += captured_message_oracles
    strong_count += captured_message_oracles
    for match in re.finditer(r"\btry\s*\{", test_code):
        open_brace = test_code.find("{", match.start())
        close_brace = find_matching_brace(test_code, open_brace)
        if close_brace is None:
            continue
        try_body = test_code[open_brace + 1 : close_brace]
        if not re.search(target_call_pattern, try_body):
            continue
        catch_match = re.match(
            r"\s*catch\s*\(\s*([A-Za-z_][\w.]*(?:Exception|Error|Throwable))\s+\w+\s*\)\s*\{",
            test_code[close_brace + 1 :],
            flags=re.DOTALL,
        )
        if not catch_match:
            continue
        catch_open = close_brace + 1 + catch_match.end() - 1
        catch_close = find_matching_brace(test_code, catch_open)
        catch_body = test_code[catch_open + 1 : catch_close] if catch_close is not None else ""
        before_try = test_code[max(0, match.start() - 300) : match.start()]
        after_catch = test_code[catch_close + 1 : catch_close + 400] if catch_close is not None else ""
        has_fail_guard = bool(
            re.search(
                r"\bfail\s*\(|throw\s+new\s+AssertionError\b|assertFalse\s*\(\s*\"?[^\"]*expected|assertTrue\s*\(\s*\"?[^\"]*expected",
                try_body,
                flags=re.I,
            )
        )
        catch_type = catch_match.group(1)
        flag_match = re.search(r"\bboolean\s+([A-Za-z_]\w*)\s*=\s*false\s*;", before_try)
        has_post_catch_flag_guard = False
        if flag_match:
            flag = re.escape(flag_match.group(1))
            has_post_catch_flag_guard = bool(
                re.search(rf"\b{flag}\s*=\s*true\s*;", catch_body)
                and re.search(
                    rf"\bif\s*\(\s*!\s*{flag}\s*\)\s*\{{?[^{{}};]*(?:throw\s+new\s+AssertionError\b|fail\s*\()",
                    after_catch,
                    flags=re.DOTALL,
                )
            )
        fail_guard_visible = has_fail_guard and not re.search(r"\b(?:AssertionError|Throwable|Error)\b", catch_type)
        flag_guard_visible = has_post_catch_flag_guard and not re.search(r"\b(?:AssertionError|Throwable|Error)\b", catch_type)
        catch_has_only_weak_handling = bool(
            re.search(r"\bprintStackTrace\s*\(|System\.(?:out|err)\.print", catch_body)
        ) or not re.search(r"\bassert\w*\s*\(|throw\s+new\s+AssertionError\b", catch_body)
        if fail_guard_visible or flag_guard_visible:
            assertion_count += 1
            if is_broad_exception_type(catch_type):
                weak_assertion_count += 1
            else:
                strong_count += 1
        elif catch_has_only_weak_handling:
            weak_handlers += 1
    return {
        "has_target_exception_test": (assertion_count + weak_handlers) > 0,
        "target_exception_assertions": assertion_count,
        "target_strong_exception_assertions": strong_count,
        "target_weak_exception_assertions": weak_assertion_count,
        "target_weak_exception_handlers": weak_handlers,
    }


def boundary_categories_from_signature(signature: str | None) -> set[str]:
    categories: set[str] = set()
    for param_type in signature_parameter_types(signature):
        is_direct_boolean = is_direct_boolean_parameter_type(param_type)
        is_primitive = is_primitive_parameter_type(param_type)
        if not is_primitive:
            categories.add("null")
            categories.add("positive_normal")
        if is_string_parameter_type(param_type):
            categories.update({"empty", "positive_normal"})
        if is_numeric_parameter_type(param_type):
            categories.update({"zero", "negative", "positive_normal"})
        if is_collection_or_array_parameter_type(param_type):
            categories.update({"empty", "positive_normal"})
        if is_direct_boolean:
            categories.update({"false_value", "true_value"})
    return categories


def applicable_boundary_categories(source: str, signature: str | None) -> set[str]:
    categories = boundary_categories_from_signature(signature)
    for signal, pattern in BOUNDARY_SOURCE_PATTERNS.items():
        if re.search(pattern, source, flags=re.MULTILINE):
            category = BOUNDARY_SOURCE_TO_CATEGORY.get(signal, signal)
            categories.add(category)
    for bounds in direct_parameter_inferred_bounds(source, signature).values():
        if any(bound.get("kind") == "number" for bound in bounds):
            categories.update({"below_inferred_bound", "at_inferred_bound", "above_inferred_bound"})
        if any(bound.get("kind") == "string" for bound in bounds):
            categories.update({"same_inferred_bound", "different_inferred_bound"})
    return {category for category in categories if category in BOUNDARY_CATEGORIES}


def applicable_boundary_items(source: str, signature: str | None) -> list[dict[str, Any]]:
    param_types = signature_parameter_types(signature)
    inferred_bounds = direct_parameter_inferred_bounds(source, signature)
    items: list[dict[str, Any]] = []
    if not param_types:
        return items
    for index, param_type in enumerate(param_types):
        categories = boundary_categories_from_signature(f"void __x({param_type} p)")
        for category in sorted(category for category in categories if category in BOUNDARY_CATEGORIES):
            item = {
                "param_index": index,
                "param_type": param_type,
                "category": category,
            }
            items.append(item)
        for bound in inferred_bounds.get(index, []):
            if bound.get("kind") == "number":
                inferred_categories = ["below_inferred_bound", "at_inferred_bound", "above_inferred_bound"]
            elif bound.get("kind") == "string":
                inferred_categories = ["same_inferred_bound"]
            else:
                inferred_categories = []
            for category in inferred_categories:
                items.append(
                    {
                        "param_index": index,
                        "param_type": param_type,
                        "category": category,
                        "bound_key": inferred_bound_key(bound),
                        "inferred_bound": bound,
                    }
                )
        string_bounds = [bound for bound in inferred_bounds.get(index, []) if bound.get("kind") == "string"]
        if string_bounds:
            items.append(
                {
                    "param_index": index,
                    "param_type": param_type,
                    "category": "different_inferred_bound",
                    "bound_key": inferred_string_literals_key(string_bounds),
                    "inferred_bound": {
                        "kind": "string",
                        "operator": "not_equal_to_all_literals",
                        "values": sorted({str(bound.get("value") or "") for bound in string_bounds}),
                        "source": "all_string_literals_for_parameter",
                    },
                }
            )
    return items


def boundary_item_scores(parameter_evidence: list[dict[str, Any]], applicable_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    covered_pairs = {
        (int(item.get("param_index")), str(category), None)
        for item in parameter_evidence
        for category in item.get("categories", [])
        if category not in {"below_inferred_bound", "at_inferred_bound", "above_inferred_bound", "same_inferred_bound", "different_inferred_bound"}
    }
    covered_pairs.update(
        (int(item.get("param_index")), str(hit.get("category")), str(hit.get("bound_key")))
        for item in parameter_evidence
        for hit in item.get("inferred_bound_hits", [])
    )
    scored = []
    matched = []
    missed = []
    for item in applicable_items:
        pair = (int(item["param_index"]), str(item["category"]), item.get("bound_key"))
        out = {**item, "covered": pair in covered_pairs}
        scored.append(out)
        if out["covered"]:
            matched.append(out)
        else:
            missed.append(out)
    return scored, matched, missed


def globally_mentioned_boundary_categories(test_code: str) -> set[str]:
    mentioned = set()
    for line in test_code.splitlines():
        if len(line) > 2000:
            line = line[:2000]
        for name, pattern in BOUNDARY_TEST_PATTERNS.items():
            if re.search(pattern, line):
                mentioned.add(name)
    return {category for category in mentioned if category in BOUNDARY_CATEGORIES}


def boundary_parameter_evidence(test_code: str, signature: str | None, source: str = "") -> list[dict[str, Any]]:
    method_name = signature_method_name(signature)
    param_types = signature_parameter_types(signature)
    calls = target_call_argument_records_with_simple_helpers(test_code, method_name)
    constraints = source_boundary_constraints(source)
    inferred_bounds = direct_parameter_inferred_bounds(source, signature)
    constants = source_constant_literals(source)
    evidence: list[dict[str, Any]] = []
    for call_index, call in enumerate(calls):
        args = call["args"]
        resolved_args = call["resolved_args"]
        for index, arg in enumerate(args):
            resolved_arg = replace_source_constants(resolved_args[index] if index < len(resolved_args) else arg, constants)
            param_type = param_types[index] if index < len(param_types) else ""
            arg_categories = boundary_categories_from_argument(resolved_arg, constraints, param_type)
            inferred_hits = inferred_bound_hits_from_argument(resolved_arg, inferred_bounds.get(index, []))
            arg_categories.update(hit["category"] for hit in inferred_hits)
            if index < len(param_types):
                param_type_categories = boundary_categories_from_signature(
                    f"void __x({param_types[index]} p)"
                )
                categories = arg_categories & param_type_categories
                categories.update(
                    arg_categories
                    & {
                        "below_inferred_bound",
                        "at_inferred_bound",
                        "above_inferred_bound",
                        "same_inferred_bound",
                        "different_inferred_bound",
                    }
                )
            else:
                categories = arg_categories
            if categories:
                evidence.append(
                    {
                        "call_index": call_index,
                        "param_index": index,
                        "param_type": param_type,
                        "argument": arg[:160],
                        "resolved_argument": resolved_arg[:160],
                        "categories": sorted(category for category in categories if category in BOUNDARY_CATEGORIES),
                        "inferred_bound_hits": [
                            {
                                "category": hit["category"],
                                "bound_key": hit["bound_key"],
                                "bound": hit["bound"],
                            }
                            for hit in inferred_hits
                            if hit["category"] in categories
                        ],
                    }
                )
    return evidence


def is_resolved_boundary_argument(argument: str) -> bool:
    arg = argument.strip()
    if not arg:
        return True
    literal = java_literal_value(arg)
    if not (isinstance(literal, dict) and "raw" in literal):
        return True
    if re.fullmatch(r"(?:[A-Za-z_$][\w$]*\.)?[A-Za-z_$][\w$]*\.class", arg):
        return True
    if re.fullmatch(r"new\s+[A-Za-z_$][\w$.<>?,\s]*\s*\(\s*\)", arg):
        return True
    if re.fullmatch(r"new\s+[A-Za-z_$][\w$.<>?,\s]*\s*\[\s*(?:0)?\s*\](?:\s*\{\s*\})?", arg):
        return True
    if re.fullmatch(r"(?:java\.util\.)?(?:Collections\.empty\w+|List\.of|Set\.of|Map\.of|Arrays\.asList)\s*\([^)]*\)", arg):
        return True
    return False


def boundary_unresolvable_argument_stats(test_code: str, signature: str | None, source: str = "") -> dict[str, Any]:
    method_name = signature_method_name(signature)
    calls = target_call_argument_records_with_simple_helpers(test_code, method_name)
    constants = source_constant_literals(source)
    total = 0
    unresolved = 0
    examples: list[dict[str, Any]] = []
    for call_index, call in enumerate(calls):
        for index, arg in enumerate(call.get("resolved_args") or call.get("args") or []):
            total += 1
            resolved_arg = replace_source_constants(str(arg), constants)
            if not is_resolved_boundary_argument(resolved_arg):
                unresolved += 1
                if len(examples) < 10:
                    examples.append(
                        {
                            "call_index": call_index,
                            "param_index": index,
                            "argument": str(arg)[:160],
                            "resolved_argument": resolved_arg[:160],
                        }
                    )
    return {
        "total_arguments": total,
        "unresolvable_arguments": unresolved,
        "unresolvable_argument_rate": unresolved / total if total else None,
        "examples": examples,
    }


def covered_boundary_categories(test_code: str, signature: str | None, source: str = "") -> set[str]:
    param_types = signature_parameter_types(signature)
    evidence = boundary_parameter_evidence(test_code, signature, source)
    covered: set[str] = set()
    for item in evidence:
        covered.update(item["categories"])
    if not evidence and not param_types:
        covered = globally_mentioned_boundary_categories(test_code)
    return {category for category in covered if category in BOUNDARY_CATEGORIES}


def classify_boundary_strength(test_code: str, source: str, signature: str | None, features: dict[str, Any]) -> dict[str, Any]:
    if is_private_method_signature(signature):
        return {
            "boundary_strength": "not_applicable",
            "boundary_strength_score": None,
            "boundary_applicable_categories": [],
            "boundary_covered_categories": [],
            "boundary_applicable_items": [],
            "boundary_matched_items": [],
            "boundary_missed_items": [],
            "boundary_parameter_scores": [],
            "boundary_parameter_evidence": [],
            "boundary_global_mentioned_categories": [],
            "boundary_source_constraints": {"string_max_lengths": [], "numeric_bounds": []},
            "boundary_applicable_count": 0,
            "boundary_covered_count": 0,
            "boundary_asserted_count": 0,
            "boundary_target_method": signature_method_name(signature),
            "boundary_target_call_count": 0,
            "boundary_total_argument_count": 0,
            "boundary_unresolvable_argument_count": 0,
            "boundary_unresolvable_argument_rate": None,
            "boundary_unresolvable_argument_examples": [],
        }
    applicable = applicable_boundary_categories(source, signature)
    constraints = source_boundary_constraints(source)
    parameter_evidence = boundary_parameter_evidence(test_code, signature, source)
    applicable_items = applicable_boundary_items(source, signature)
    boundary_items, matched_items, missed_items = boundary_item_scores(parameter_evidence, applicable_items)
    unresolved_stats = boundary_unresolvable_argument_stats(test_code, signature, source)
    global_mentions = globally_mentioned_boundary_categories(test_code) & applicable
    method_name = signature_method_name(signature)
    target_argument_calls = subject_call_arguments(test_code, method_name)
    item_categories = {str(item["category"]) for item in boundary_items}
    covered_item_categories = {str(item["category"]) for item in matched_items}
    if applicable_items:
        by_param: dict[int, dict[str, Any]] = {}
        for item in applicable_items:
            by_param.setdefault(int(item["param_index"]), {"applicable": 0, "covered": 0})
            by_param[int(item["param_index"])]["applicable"] += 1
        for item in matched_items:
            by_param[int(item["param_index"])]["covered"] += 1
        parameter_scores = [
            {
                "param_index": index,
                "applicable": values["applicable"],
                "covered": values["covered"],
                "score": values["covered"] / values["applicable"] if values["applicable"] else None,
            }
            for index, values in sorted(by_param.items())
        ]
        score = avg([item["score"] for item in parameter_scores])
        label = ratio_strength_label(score)
    else:
        label, score = "not_applicable", None
        parameter_scores = []
    return {
        "boundary_strength": label,
        "boundary_strength_score": score,
        "boundary_applicable_categories": sorted(item_categories),
        "boundary_covered_categories": sorted(covered_item_categories),
        "boundary_applicable_items": boundary_items,
        "boundary_matched_items": matched_items,
        "boundary_missed_items": missed_items,
        "boundary_parameter_scores": parameter_scores,
        "boundary_parameter_evidence": parameter_evidence,
        "boundary_global_mentioned_categories": sorted(global_mentions),
        "boundary_source_constraints": constraints,
        "boundary_applicable_count": len(applicable_items),
        "boundary_covered_count": len(matched_items),
        "boundary_asserted_count": len(matched_items),
        "boundary_target_method": method_name,
        "boundary_target_call_count": len(target_argument_calls),
        "boundary_total_argument_count": unresolved_stats["total_arguments"],
        "boundary_unresolvable_argument_count": unresolved_stats["unresolvable_arguments"],
        "boundary_unresolvable_argument_rate": unresolved_stats["unresolvable_argument_rate"],
        "boundary_unresolvable_argument_examples": unresolved_stats["examples"],
    }


def observable_obligations(source: str, obligations: dict[str, dict[str, Any]], signature: str | None) -> dict[str, bool]:
    target_source = target_method_source(source, signature) or ""
    returns_value = signature_declares_return_value(signature) and bool(re.search(r"\breturn\s+[^;]+;", target_source))
    return {
        "return_behavior": returns_value,
        "exception_behavior": bool(obligations["exception_path"]["total"]),
        "side_effect_or_dependency": any(
            is_assertable_external_side_effect_item(item)
            for item in source_obligation_items(source, signature).get("interaction_dependency", [])
        ),
    }


def target_method_body(source: str, signature: str | None) -> str | None:
    method_name = signature_method_name(signature)
    if not method_name:
        return None
    match = re.search(rf"\b{re.escape(method_name)}\s*\([^)]*\)", source)
    if not match:
        return None
    open_brace = source.find("{", match.start())
    close_brace = find_matching_brace(source, open_brace)
    if close_brace is None:
        return None
    return source[open_brace + 1 : close_brace]


def is_no_observable_void_target(source: str, signature: str | None) -> bool:
    if not is_void_signature(signature):
        return False
    body = target_method_body(source, signature)
    if body is None:
        return False
    stripped = strip_comments_and_strings(body)
    stripped = re.sub(r"\s+", "", stripped)
    return stripped == ""


def has_observable_oracle_contract(
    source: str,
    obligations: dict[str, dict[str, Any]],
    obligation_items: dict[str, list[dict[str, Any]]],
    signature: str | None,
) -> bool:
    if is_no_observable_void_target(source, signature):
        return False
    observable = observable_obligations(source, obligations, signature)
    if any(observable.values()):
        return True
    return bool(oracle_obligation_items(source, obligation_items, signature))


def return_obligation_items(source: str, signature: str | None) -> list[dict[str, Any]]:
    if not signature_declares_return_value(signature):
        return []
    target_source = target_method_source(source, signature)
    if target_source is None:
        return [
            {
                "category": "weak_oracle",
                "signal": "return_behavior",
                "line": None,
                "snippet": "Non-void target signature requires a checked return value.",
            }
        ]
    target_start = source.find(target_source)
    target_start_line = source[:target_start].count("\n") if target_start >= 0 else 0
    method_name = signature_method_name(signature)
    methods = java_method_definitions(target_source)
    outer_method = next((method for method in methods if method["name"] == method_name), None)
    nested_method_spans = [
        (method["start"], method["end"])
        for method in methods
        if outer_method and method is not outer_method and outer_method["start"] < method["start"] < outer_method["end"]
    ]
    items = []
    local_offset = 0
    for local_line_number, line in enumerate(target_source.splitlines(keepends=True), start=1):
        stripped = line.strip()
        belongs_to_nested_method = any(start <= local_offset < end for start, end in nested_method_spans)
        if not belongs_to_nested_method and re.search(r"\breturn\s+[^;]+;", stripped):
            items.append(
                {
                    "category": "weak_oracle",
                    "signal": "return_behavior",
                    "line": target_start_line + local_line_number if target_start >= 0 else None,
                    "snippet": stripped[:240],
                }
            )
        local_offset += len(line)
    return items


def oracle_obligation_items(source: str, obligation_items: dict[str, list[dict[str, Any]]], signature: str | None) -> list[dict[str, Any]]:
    items = []
    items.extend(return_obligation_items(source, signature))
    for item in obligation_items.get("exception_path", []):
        items.append({**item, "category": "weak_oracle", "signal": "exception_behavior"})
    for item in obligation_items.get("interaction_dependency", []):
        if is_assertable_external_side_effect_item(item):
            items.append({**item, "category": "weak_oracle", "signal": "side_effect_or_dependency"})
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for item in items:
        key = (str(item.get("signal") or ""), item.get("line"), str(item.get("snippet") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def is_assertable_external_side_effect_item(item: dict[str, Any]) -> bool:
    snippet = str(item.get("snippet") or "")
    case = str(item.get("dependency_case") or "")
    return bool(
        case == "void_or_side_effect"
        or re.search(r"\bFiles\.(?:write|delete|copy|move)\w*\s*\(", snippet)
        or re.search(r"\bnew\s+(?:FileOutputStream|OutputStreamWriter|FileWriter|BufferedWriter)\s*\(", snippet)
        or re.search(r"\.(?:getOutputStream|send|execute)\s*\(", snippet)
    )


def oracle_item_base_score(
    item: dict[str, Any],
    *,
    assertion_count: int,
    matched_item: dict[str, Any] | None,
) -> tuple[int, str]:
    if assertion_count == 0:
        return 0, "no_visible_assertion"
    if matched_item is None:
        return 0, "no_matched_visible_oracle"
    score = int(matched_item.get("oracle_match_score") or 0)
    reason = str(matched_item.get("oracle_match_reason") or "matched_obligation")
    return max(0, min(2, score)), reason


def score_oracle_obligations(
    oracle_items: dict[str, Any],
    *,
    features: dict[str, Any],
    source: str,
    obligations: dict[str, dict[str, Any]],
    signature: str | None,
    assertion_count: int,
) -> list[dict[str, Any]]:
    matched_by_index = {
        int(item["oracle_item_index"]): item
        for item in oracle_items["oracle_matched_items"]
        if item.get("oracle_item_index") is not None
    }
    scored = []
    for index, item in enumerate(oracle_items["oracle_obligation_items"]):
        indexed_item = {**item, "oracle_item_index": index}
        matched_item = matched_by_index.get(index)
        score, reason = oracle_item_base_score(
            indexed_item,
            assertion_count=assertion_count,
            matched_item=matched_item,
        )
        scored.append(
            {
                **indexed_item,
                "matched": matched_item is not None,
                "score": score,
                "strength": strength_label(score),
                "score_reasons": [reason],
            }
        )
    return scored


def count_direct_external_side_effect_oracles(test_code: str, method_name: str | None) -> int:
    """Count visible assertions/verifications for assertable external side effects."""
    if not method_name or not subject_calls(test_code, method_name):
        return 0
    assertion_code = visible_assertion_code(test_code)
    count = 0
    count += len(re.findall(r"\bverify\s*\([^;]+?;", assertion_code, flags=re.DOTALL))
    count += len(re.findall(r"\bArgumentCaptor\b|captor\.capture\s*\(", assertion_code))
    count += len(
        re.findall(
            r"\b(?:assert(?:Equals|ArrayEquals|IterableEquals|That|True|False)|check(?:Equals|Same|True|False)?)\s*\([^;{}]*"
            r"(?:Files\.(?:readString|readAllBytes|readAllLines)|ByteArrayOutputStream|"
            r"\.toByteArray\s*\(|\.toString\s*\(|\.exists\s*\(|\.isFile\s*\(|\.length\s*\(|"
            r"Arrays\.equals\s*\(|written|payload|body|header|path|keystore)",
            assertion_code,
            flags=re.I | re.DOTALL,
        )
    )
    count += count_recording_external_side_effect_assertions(test_code, assertion_code)
    return count


def count_recording_external_side_effect_assertions(test_code: str, assertion_code: str) -> int:
    """Count assertions over fields populated by overriding external side-effect methods."""
    assigned_fields: set[str] = set()
    method_pattern = re.compile(
        r"@Override\s+"
        r"(?:public|protected|private)?\s*"
        r"(?:[A-Za-z_$][\w$.\[\]<>?,]+\s+)+"
        r"(send|write|close|execute|delete|copy|move|getOutputStream)\s*\(",
        flags=re.MULTILINE,
    )
    for match in method_pattern.finditer(test_code):
        open_paren = test_code.find("(", match.start())
        close_paren = find_matching_paren(test_code, open_paren)
        if close_paren is None:
            continue
        open_brace = test_code.find("{", close_paren)
        if open_brace < 0:
            continue
        close_brace = find_matching_brace(test_code, open_brace)
        if close_brace is None:
            continue
        body = test_code[open_brace + 1 : close_brace]
        for assignment in re.finditer(
            r"\b(?:this\.)?([A-Za-z_]\w*)\s*(?:=|\+\+|--|\+=|-=)",
            body,
        ):
            name = assignment.group(1)
            if re.search(
                r"(?:last|sent|send|written|captur|record|call|count|event|data|payload|body|header|request|response|closed|value|id)",
                name,
                flags=re.I,
            ):
                assigned_fields.add(name)
    if not assigned_fields:
        return 0

    count = 0
    assertion_pattern = re.compile(
        r"\b(?:assert[A-Z]\w*|check(?:Equals|Same|True|False)?)\s*\([^;{}]*\)",
        flags=re.DOTALL,
    )
    for match in assertion_pattern.finditer(assertion_code):
        assertion = match.group(0)
        if any(re.search(rf"\b{re.escape(field)}\b", assertion) for field in assigned_fields):
            count += 1
    return count


def count_weak_external_side_effect_oracles(test_code: str, method_name: str | None) -> int:
    """Count visible but weak external side-effect checks, mainly existence-only checks."""
    if not method_name or not subject_calls(test_code, method_name):
        return 0
    assertion_code = visible_assertion_code(test_code)
    return len(
        re.findall(
            r"\bassert(?:True|False)\s*\([^;{}]*(?:\.exists\s*\(|Files\.(?:exists|notExists)\s*\()",
            assertion_code,
            flags=re.I | re.DOTALL,
        )
    )


def exception_oracle_message_evidence(test_code: str, method_name: str | None) -> dict[str, Any]:
    """Extract messages asserted for exception-producing target calls."""
    if not method_name:
        return {"messages": [], "message_groups": []}
    assertion_code = visible_assertion_code(test_code)
    methods = java_method_definitions(assertion_code)
    helper_methods = {str(method.get("name") or ""): method for method in methods}
    helper_names = typed_exception_assertion_helper_names(test_code) | {
        name
        for name in helper_methods
        if re.search(r"(?:expect|capture|assert|check).*(?:Exception|Error|Throw)", name, re.I)
    }
    target_call_pattern = re.compile(
        rf"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*\.{re.escape(method_name)}\s*\("
    )
    expected_message_indexes: dict[str, list[int]] = defaultdict(list)
    for name, method in helper_methods.items():
        args = split_java_arguments(str(method.get("args") or ""))
        body = str(method.get("body") or "")
        for index, arg in enumerate(args):
            param_match = re.search(r"([A-Za-z_]\w*)\s*$", arg)
            if not param_match:
                continue
            param = re.escape(param_match.group(1))
            if re.search(rf"\b{param}\b[^;{{}}]*\.getMessage\s*\(|\.getMessage\s*\([^;{{}}]*\b{param}\b", body):
                expected_message_indexes[name].append(index)

    messages: list[str] = []
    message_groups: list[list[str]] = []
    for method in methods:
        body = str(method.get("body") or "")
        for helper_name in helper_names:
            for match in re.finditer(rf"\b{re.escape(helper_name)}\s*\(", body):
                open_paren = body.find("(", match.start())
                close_paren = find_matching_paren(body, open_paren)
                if close_paren is None:
                    continue
                call_text = body[match.start() : close_paren + 1]
                if not target_call_pattern.search(call_text):
                    continue
                args = split_java_arguments(body[open_paren + 1 : close_paren])
                call_messages: list[str] = []
                for index in expected_message_indexes.get(helper_name, []):
                    if index < len(args):
                        call_messages.extend(java_string_literal_values(args[index]))
                assignment = re.search(
                    r"(?:[A-Za-z_][\w.$<>?, ]*\s+)?([A-Za-z_]\w*)\s*=\s*$",
                    body[max(0, match.start() - 160) : match.start()],
                )
                if not assignment:
                    continue
                var = re.escape(assignment.group(1))
                for message_match in re.finditer(rf"\b{var}\.getMessage\s*\(\s*\)", body[close_paren + 1 :]):
                    absolute = close_paren + 1 + message_match.start()
                    context = body[max(close_paren + 1, absolute - 300) : absolute + 300]
                    call_messages.extend(java_string_literal_values(context))
                if call_messages:
                    unique_call_messages = list(dict.fromkeys(message for message in call_messages if message))
                    message_groups.append(unique_call_messages)
                    messages.extend(unique_call_messages)
    return {
        "messages": list(dict.fromkeys(message for message in messages if message)),
        "message_groups": message_groups,
    }


def exception_item_matches_message(source: str, item: dict[str, Any], messages: list[str]) -> bool:
    line = int(item.get("line") or 0)
    source_lines = source.splitlines()
    if line <= 0 or line > len(source_lines):
        statement = str(item.get("snippet") or "")
    else:
        statement_lines = []
        for source_line in source_lines[line - 1 : min(len(source_lines), line + 6)]:
            statement_lines.append(source_line.strip())
            if ";" in source_line:
                break
        statement = " ".join(statement_lines)
    templates = java_string_literal_values(statement)
    if not templates:
        return False
    template = "".join(templates)
    formatted_pattern = re.sub(r"%[0-9$#+\- .,(]*[a-zA-Z]", ".*?", re.escape(template))
    static_template = re.sub(r"%[0-9$#+\- .,(]*[a-zA-Z]", "", template).strip()
    for message in messages:
        if re.fullmatch(formatted_pattern, message):
            return True
        if len(message) >= 8 and message in template:
            return True
        if len(static_template) >= 8 and static_template in message:
            return True
    return False


def switch_case_return_reached_by_test(source: str, item: dict[str, Any], test_code: str) -> bool | None:
    """Return explicit enum-case reachability, or None when it cannot be inferred safely."""
    line = int(item.get("line") or 0)
    source_lines = source.splitlines()
    if line <= 0 or line > len(source_lines):
        return None
    labels: list[str] = []
    first_case_line: int | None = None
    for index in range(line - 1, max(-1, line - 25), -1):
        text = source_lines[index]
        matches = re.findall(r"\bcase\s+([A-Za-z_]\w*)\s*:", text)
        if matches:
            labels[0:0] = matches
            first_case_line = index
            break
        if index < line - 1 and re.search(r"\b(?:return|break|throw)\b", text):
            return None
    if first_case_line is None:
        return None
    for index in range(first_case_line - 1, max(-1, first_case_line - 12), -1):
        text = source_lines[index].strip()
        matches = re.findall(r"\bcase\s+([A-Za-z_]\w*)\s*:", text)
        if matches:
            labels[0:0] = matches
            continue
        if text:
            break

    prefix = "\n".join(source_lines[: first_case_line + 1])
    switch_matches = list(re.finditer(r"\bswitch\s*\(([^)]*(?:\([^)]*\)[^)]*)?)\)", prefix))
    switch_expression = switch_matches[-1].group(1) if switch_matches else ""
    inferred_enum_type = None
    switch_accessors = list(
        re.finditer(r"\bswitch\s*\([^;{}\n]*?\.([A-Za-z_]\w*)\s*\(\s*\)\s*\)", prefix)
    )
    accessor = switch_accessors[-1] if switch_accessors else re.search(
        r"\.([A-Za-z_]\w*)\s*\(\s*\)\s*$", switch_expression.strip()
    )
    if accessor:
        declaration = re.search(rf"\b([A-Za-z_]\w*)\s+{re.escape(accessor.group(1))}\s*\(", source)
        if declaration:
            inferred_enum_type = declaration.group(1)
    elif re.fullmatch(r"[A-Za-z_]\w*", switch_expression.strip()):
        declaration = re.search(
            rf"\b([A-Za-z_]\w*)\s+{re.escape(switch_expression.strip())}\s*=",
            source,
        )
        if declaration:
            inferred_enum_type = declaration.group(1)

    enum_candidates: list[str] = []
    for match in re.finditer(r"\benum\s+([A-Za-z_]\w*)[^\{]*\{", source):
        open_brace = source.find("{", match.start())
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None:
            continue
        body = source[open_brace + 1 : close_brace]
        if all(re.search(rf"\b{re.escape(label)}\b", body) for label in labels):
            enum_candidates.append(match.group(1))
    enum_type = (
        inferred_enum_type
        if inferred_enum_type in enum_candidates
        else enum_candidates[0] if len(enum_candidates) == 1 else None
    )
    if not enum_type or not re.search(rf"\b(?:Subject\.)?{re.escape(enum_type)}\s*\.", test_code):
        return None
    return any(
        re.search(rf"\b(?:Subject\.)?{re.escape(enum_type)}\s*\.\s*{re.escape(label)}\b", test_code)
        for label in labels
    )


VERIFIED_UNREACHABLE_RETURNS = {
    # Self-contained benchmark methods audited during consistency review.
    "2228d2c582e59962545851d8f068f9262018ae057ddf62c5e50b1843874e9081": {26, 42, 56, 58, 60},
    "082bc3037cae58f29c7728a568c93f9473920d6fb5b251b45578f8fbbdafb615": {21},
}


def verified_return_reached(source: str, signature: str | None, item: dict[str, Any]) -> bool | None:
    """Apply reviewed reachability only while the exact target source is unchanged."""
    target_source = target_method_source(source, signature)
    if target_source is None:
        return None
    item_line = int(item.get("line") or 0)
    source_hash = hashlib.sha256(target_source.encode()).hexdigest()
    unreachable_lines = VERIFIED_UNREACHABLE_RETURNS.get(source_hash)
    return False if unreachable_lines is not None and item_line in unreachable_lines else None


def matched_oracle_items(
    test_code: str,
    features: dict[str, Any],
    source: str,
    obligation_items: dict[str, list[dict[str, Any]]],
    signature: str | None,
) -> dict[str, Any]:
    items = oracle_obligation_items(source, obligation_items, signature)
    method_name = signature_method_name(signature)
    exception_evidence = target_exception_evidence(test_code, method_name)
    exception_message_evidence = exception_oracle_message_evidence(test_code, method_name)
    exception_messages = list(exception_message_evidence["messages"])
    direct_external = count_direct_external_side_effect_oracles(test_code, method_name)
    weak_external = count_weak_external_side_effect_oracles(test_code, method_name)

    budgets = {
        "return_behavior": {
            "direct": int(features.get("meaningful_return_assertion_count") or 0),
            "weak": int(features.get("weak_return_assertion_count") or 0),
        },
        "exception_behavior": {
            "direct": int(exception_evidence["target_strong_exception_assertions"]),
            "weak": int(exception_evidence["target_weak_exception_assertions"]),
        },
        "side_effect_or_dependency": {
            "direct": direct_external,
            "weak": weak_external,
        },
    }
    reasons = {
        "return_behavior": {
            "direct": "direct_return_or_content_assertion",
            "weak": "weak_return_existence_or_type_assertion",
        },
        "exception_behavior": {
            "direct": "specific_exception_type_assertion",
            "weak": "broad_exception_assertion",
        },
        "side_effect_or_dependency": {
            "direct": "verified_external_side_effect",
            "weak": "weak_external_side_effect_existence_assertion",
        },
    }

    matched = []
    missed = []
    used_direct = Counter()
    used_weak = Counter()
    used_message_direct = Counter()
    exception_items = [item for item in items if item.get("signal") == "exception_behavior"]
    matched_message_groups = sum(
        any(exception_item_matches_message(source, item, group) for item in exception_items)
        for group in exception_message_evidence["message_groups"]
    )
    reserved_exception_direct = min(
        int(budgets["exception_behavior"]["direct"]),
        matched_message_groups,
    )
    for index, item in enumerate(items):
        signal = str(item.get("signal"))
        indexed_item = {**item, "oracle_item_index": index}
        budget = budgets.get(signal, {"direct": 0, "weak": 0})
        if signal == "return_behavior" and switch_case_return_reached_by_test(source, indexed_item, test_code) is False:
            missed.append({**indexed_item, "oracle_match_reason": "switch_case_not_exercised"})
            continue
        if signal == "return_behavior" and verified_return_reached(source, signature, indexed_item) is False:
            missed.append({**indexed_item, "oracle_match_reason": "reviewed_return_not_exercised"})
            continue
        message_match = bool(
            signal == "exception_behavior"
            and exception_messages
            and exception_item_matches_message(source, indexed_item, exception_messages)
        )
        if message_match and used_direct[signal] < int(budget.get("direct") or 0):
            matched.append(
                {
                    **indexed_item,
                    "oracle_match_rule": signal,
                    "oracle_match_score": 2,
                    "oracle_match_reason": "specific_exception_message_assertion",
                }
            )
            used_direct[signal] += 1
            used_message_direct[signal] += 1
        elif (
            used_direct[signal] - used_message_direct[signal]
            < int(budget.get("direct") or 0) - (reserved_exception_direct if signal == "exception_behavior" else 0)
        ):
            matched.append(
                {
                    **indexed_item,
                    "oracle_match_rule": signal,
                    "oracle_match_score": 2,
                    "oracle_match_reason": reasons.get(signal, {}).get("direct", "direct_obligation_oracle"),
                }
            )
            used_direct[signal] += 1
        elif used_weak[signal] < int(budget.get("weak") or 0):
            matched.append(
                {
                    **indexed_item,
                    "oracle_match_rule": signal,
                    "oracle_match_score": 1,
                    "oracle_match_reason": reasons.get(signal, {}).get("weak", "weak_visible_oracle"),
                }
            )
            used_weak[signal] += 1
        else:
            missed.append(indexed_item)

    return {
        "oracle_obligation_items": items,
        "oracle_matched_items": matched,
        "oracle_missed_items": missed,
        "oracle_match_budgets": budgets,
    }


def asserted_observable_behaviors(test_code: str, features: dict[str, Any], signature: str | None) -> dict[str, bool]:
    has_semantic = int(features["semantic_assertion_count"] or 0) > 0
    method_name = signature_method_name(signature)
    exception_evidence = target_exception_evidence(test_code, method_name)
    return_asserted = bool(has_semantic and has_target_return_assertion(test_code, method_name))
    exception_asserted = bool(exception_evidence["has_target_exception_test"])
    state_asserted = bool(has_semantic and has_target_state_assertion(test_code, method_name))
    side_effect_asserted = bool(
        has_target_interaction_evidence(test_code, method_name)
        and (
            has_semantic
            or re.search(r"\b(?:called|invoked|calls?|arguments?|params?|count|captured|logged|written)\b", test_code, re.I)
        )
    )
    return {
        "return_behavior": return_asserted,
        "exception_behavior": exception_asserted,
        "state_change": state_asserted,
        "side_effect_or_dependency": side_effect_asserted,
    }


def classify_oracle_strength(
    test_code: str,
    features: dict[str, Any],
    obligations: dict[str, dict[str, Any]],
    obligation_items: dict[str, list[dict[str, Any]]],
    source: str,
    signature: str | None,
    mutation_score: Any,
) -> dict[str, Any]:
    observable = observable_obligations(source, obligations, signature)
    if not has_observable_oracle_contract(source, obligations, obligation_items, signature):
        return {
            "oracle_strength": "not_applicable",
            "oracle_strength_score": None,
            "observable_obligations": observable,
            "asserted_observable_behaviors": {
                "return_behavior": False,
                "exception_behavior": False,
                "state_change": False,
                "side_effect_or_dependency": False,
            },
            "applicable_observable_count": 0,
            "asserted_observable_count": 0,
            "oracle_obligation_items": [],
            "oracle_matched_items": [],
            "oracle_missed_items": [],
            "oracle_obligation_scores": [],
            "oracle_match_budgets": {},
            "oracle_cap_reasons": ["no_observable_contract"],
        }
    asserted = asserted_observable_behaviors(test_code, features, signature)
    applicable = [name for name, applies in observable.items() if applies]
    asserted_applicable = [name for name in applicable if asserted.get(name)]
    has_trivial_positive = bool(TRIVIAL_POSITIVE_ASSERTION_RE.search(test_code))
    assertion_count = int(features["assertion_count"] or 0)
    weak_assertion_count = int(features["weak_assertion_count"] or 0)
    semantic_assertion_count = int(features["semantic_assertion_count"] or 0)
    correct_exception_assertion_count = int(features["correct_exception_assertion_count"] or 0)
    oracle_items = matched_oracle_items(test_code, features, source, obligation_items, signature)
    total_items = len(oracle_items["oracle_obligation_items"])
    matched_items = len(oracle_items["oracle_matched_items"])
    obligation_scores = score_oracle_obligations(
        oracle_items,
        features=features,
        source=source,
        obligations=obligations,
        signature=signature,
        assertion_count=assertion_count,
    )
    score_values = [float(item["score"]) for item in obligation_scores]
    score = sum(score_values) / len(score_values) if score_values else None
    label = strength_label(int(score + 0.5)) if score is not None else "not_applicable"
    cap_reasons = sorted({reason for item in obligation_scores for reason in item.get("score_reasons", [])[1:]})

    return {
        "oracle_strength": label,
        "oracle_strength_score": score,
        "observable_obligations": observable,
        "asserted_observable_behaviors": asserted,
        "applicable_observable_count": total_items,
        "asserted_observable_count": matched_items,
        "oracle_obligation_items": oracle_items["oracle_obligation_items"][:100],
        "oracle_matched_items": oracle_items["oracle_matched_items"][:100],
        "oracle_missed_items": oracle_items["oracle_missed_items"][:100],
        "oracle_obligation_scores": obligation_scores[:100],
        "oracle_match_budgets": oracle_items["oracle_match_budgets"],
        "oracle_cap_reasons": cap_reasons,
    }


def classify_exception_strength(
    test_code: str,
    features: dict[str, Any],
    obligations: dict[str, dict[str, Any]],
    pit: dict[str, Counter],
    execution_passed: bool,
    signature: str | None,
) -> dict[str, Any]:
    exception_obligations = int(obligations["exception_path"].get("total") or 0)
    if exception_obligations == 0:
        return {
            "exception_strength": "not_applicable",
            "exception_strength_score": None,
            "exception_obligations": 0,
            "exception_covered": 0,
            "exception_asserted_count": 0,
            "exception_assertions": 0,
            "strong_exception_assertions": 0,
            "weak_exception_handlers": 0,
            "has_target_exception_test": False,
        }

    pit_counts = pit.get("exception_path", Counter())
    pit_total = int(pit_counts.get("total", 0))
    pit_no_coverage = int(pit_counts.get("NO_COVERAGE", 0))
    pit_covered = max(0, pit_total - pit_no_coverage)
    method_name = signature_method_name(signature)
    target_evidence = target_exception_evidence(test_code, method_name)
    exception_assertions = int(target_evidence["target_exception_assertions"])
    weak_exception_handlers = int(target_evidence["target_weak_exception_handlers"])
    message_or_cause_checks = len(
        re.findall(
            r"\bgetMessage\s*\(|\.getCause\s*\(|\bmessage\b|contains\s*\(|startsWith\s*\(|endsWith\s*\(",
            test_code,
            flags=re.I,
        )
    )
    path_specific_inputs = int(features.get("boundary_covered_count") or 0)
    strong_exception_assertions = min(
        exception_assertions,
        int(target_evidence["target_strong_exception_assertions"]) + message_or_cause_checks + path_specific_inputs,
    )

    if not execution_passed:
        asserted_count = 0
    else:
        reached_count = pit_covered if pit_total else (exception_obligations if target_evidence["has_target_exception_test"] else 0)
        asserted_count = min(exception_obligations, reached_count, exception_assertions)
    score = asserted_count / exception_obligations
    label = ratio_strength_label(score)

    return {
        "exception_strength": label,
        "exception_strength_score": score,
        "exception_obligations": exception_obligations,
        "exception_covered": min(exception_obligations, pit_covered if pit_total else (exception_obligations if target_evidence["has_target_exception_test"] else 0)),
        "exception_asserted_count": asserted_count,
        "exception_assertions": exception_assertions,
        "strong_exception_assertions": strong_exception_assertions,
        "weak_exception_handlers": weak_exception_handlers,
        "has_target_exception_test": target_evidence["has_target_exception_test"],
    }


def strength_label(score: int | None) -> str:
    if score is None:
        return "not_applicable"
    return ["no_visible_oracle", "weak_visible_oracle", "direct_obligation_oracle"][max(0, min(2, score))]


def ratio_strength_label(score: float | None) -> str:
    return "not_applicable" if score is None else "applicable"


def assertion_strength_profile(
    assertion_mutation: dict[str, Any],
    execution_passed: bool,
    target_is_private: bool = False,
) -> dict[str, Any]:
    assertion_mutation = enrich_assertion_mutation_return_subcategories(assertion_mutation)
    raw_status = assertion_mutation.get("status")
    empty_scope = assertion_scope_summary([], PRIMARY_ASSERTION_CONSTRUCTS)
    empty_exception_scope = assertion_scope_summary([], {"exception_behavior"})
    empty_side_scope = assertion_scope_summary([], SIDE_EFFECT_RESEARCH_CONSTRUCTS)
    if not execution_passed:
        return {
            "assertion_mutation_status": "skipped_original_not_executed",
            "assertion_mutation_raw_status": raw_status,
            "assertion_strength": "not_applicable",
            "assertion_strength_score": None,
            "assertion_strength_pessimistic_score": None,
            "assertion_strength_total_items": 0,
            "assertion_strength_reached_items": 0,
            "assertion_strength_scorable_items": 0,
            "assertion_strength_non_representable_items": 0,
            "assertion_strength_checked_items": 0,
            "assertion_strength_oracle_killed_items": 0,
            "assertion_strength_incidentally_killed_items": 0,
            "assertion_strength_survived_items": 0,
            "assertion_strength_path_changed_items": 0,
            "assertion_strength_coverage_unverified_items": 0,
            "assertion_strength_unmeasurable_by_type_substitution_items": 0,
            "assertion_strength_represented_items": 0,
            "assertion_strength_representability_rate": None,
            "assertion_strength_scorable_rate": None,
            "assertion_strength_status_counts": assertion_mutation.get("status_counts") or {},
            "assertion_strength_by_construct": {},
            "assertion_strength_return_subcategories": {},
            "assertion_strength_return_object_rules": {},
            "exception_behavior_excluding_broad_original_scope": empty_exception_scope,
            "side_effect_research_scope": empty_side_scope,
            "side_effect_research_by_construct": {},
            "assertion_strength_items": [],
        }
    if target_is_private:
        return {
            "assertion_mutation_status": "skipped_private_target_method",
            "assertion_mutation_raw_status": raw_status,
            "assertion_strength": "not_applicable",
            "assertion_strength_score": None,
            "assertion_strength_pessimistic_score": None,
            "assertion_strength_total_items": 0,
            "assertion_strength_reached_items": 0,
            "assertion_strength_scorable_items": 0,
            "assertion_strength_non_representable_items": 0,
            "assertion_strength_checked_items": 0,
            "assertion_strength_oracle_killed_items": 0,
            "assertion_strength_incidentally_killed_items": 0,
            "assertion_strength_survived_items": 0,
            "assertion_strength_path_changed_items": 0,
            "assertion_strength_coverage_unverified_items": 0,
            "assertion_strength_unmeasurable_by_type_substitution_items": 0,
            "assertion_strength_represented_items": 0,
            "assertion_strength_representability_rate": None,
            "assertion_strength_scorable_rate": None,
            "assertion_strength_status_counts": assertion_mutation.get("status_counts") or {},
            "assertion_strength_by_construct": {},
            "assertion_strength_return_subcategories": {},
            "assertion_strength_return_object_rules": {},
            "exception_behavior_excluding_broad_original_scope": empty_exception_scope,
            "side_effect_research_scope": empty_side_scope,
            "side_effect_research_by_construct": {},
            "assertion_strength_items": [],
        }
    all_items = assertion_mutation.get("items") or []
    primary_scope = assertion_scope_summary(all_items, PRIMARY_ASSERTION_CONSTRUCTS)
    exception_excluding_broad_scope = assertion_mutation.get("exception_behavior_excluding_broad_original_scope") or assertion_scope_summary(
        all_items,
        {"exception_behavior"},
        exclude_broad_original=True,
    )
    side_effect_scope = assertion_scope_summary(all_items, SIDE_EFFECT_RESEARCH_CONSTRUCTS)
    by_construct = assertion_mutation.get("by_construct") or {}
    primary_by_construct = {
        construct: stats
        for construct, stats in by_construct.items()
        if construct in PRIMARY_ASSERTION_CONSTRUCTS
    }
    side_effect_by_construct = {
        construct: stats
        for construct, stats in by_construct.items()
        if construct in SIDE_EFFECT_RESEARCH_CONSTRUCTS
    }
    return_subcategories = normalize_return_subcategory_summary(
        assertion_mutation.get("return_behavior_by_subcategory") or return_subcategory_summary(all_items)
    )
    return_object_rules = assertion_mutation.get("return_behavior_object_by_rule") or return_object_rule_summary(all_items)
    if assertion_mutation.get("status") != "passed":
        return {
            "assertion_mutation_status": assertion_mutation.get("status"),
            "assertion_mutation_raw_status": raw_status,
            "assertion_strength": "not_applicable",
            "assertion_strength_score": primary_scope["score"],
            "assertion_strength_pessimistic_score": primary_scope["pessimistic_score"],
            "assertion_strength_total_items": primary_scope["total_items"],
            "assertion_strength_reached_items": primary_scope["reached_items"],
            "assertion_strength_scorable_items": primary_scope["scorable_items"],
            "assertion_strength_non_representable_items": primary_scope["non_representable_items"],
            "assertion_strength_checked_items": primary_scope["checked_items"],
            "assertion_strength_oracle_killed_items": primary_scope["oracle_killed_items"],
            "assertion_strength_incidentally_killed_items": primary_scope["incidentally_killed_items"],
            "assertion_strength_survived_items": primary_scope["survived_items"],
            "assertion_strength_path_changed_items": primary_scope["survived_path_changed_items"],
            "assertion_strength_coverage_unverified_items": primary_scope["coverage_unverified_items"],
            "assertion_strength_unmeasurable_by_type_substitution_items": primary_scope["unmeasurable_by_type_substitution_items"],
            "assertion_strength_represented_items": primary_scope["represented_items"],
            "assertion_strength_representability_rate": primary_scope["representability_rate"],
            "assertion_strength_scorable_rate": primary_scope["scorable_rate"],
            "assertion_strength_status_counts": assertion_mutation.get("status_counts") or {},
            "assertion_strength_by_construct": primary_by_construct,
            "assertion_strength_return_subcategories": return_subcategories,
            "assertion_strength_return_object_rules": return_object_rules,
            "exception_behavior_excluding_broad_original_scope": exception_excluding_broad_scope,
            "side_effect_research_scope": side_effect_scope,
            "side_effect_research_by_construct": side_effect_by_construct,
            "assertion_strength_items": all_items,
        }

    score_value = primary_scope["score"]
    return {
        "assertion_mutation_status": assertion_mutation.get("status"),
        "assertion_mutation_raw_status": raw_status,
        "assertion_strength": ratio_strength_label(score_value),
        "assertion_strength_score": score_value,
        "assertion_strength_pessimistic_score": primary_scope["pessimistic_score"],
        "assertion_strength_total_items": primary_scope["total_items"],
        "assertion_strength_reached_items": primary_scope["reached_items"],
        "assertion_strength_scorable_items": primary_scope["scorable_items"],
        "assertion_strength_non_representable_items": primary_scope["non_representable_items"],
        "assertion_strength_checked_items": primary_scope["checked_items"],
        "assertion_strength_oracle_killed_items": primary_scope["oracle_killed_items"],
        "assertion_strength_incidentally_killed_items": primary_scope["incidentally_killed_items"],
        "assertion_strength_survived_items": primary_scope["survived_items"],
        "assertion_strength_path_changed_items": primary_scope["survived_path_changed_items"],
        "assertion_strength_coverage_unverified_items": primary_scope["coverage_unverified_items"],
        "assertion_strength_unmeasurable_by_type_substitution_items": primary_scope["unmeasurable_by_type_substitution_items"],
        "assertion_strength_represented_items": primary_scope["represented_items"],
        "assertion_strength_representability_rate": primary_scope["representability_rate"],
        "assertion_strength_scorable_rate": primary_scope["scorable_rate"],
        "assertion_strength_status_counts": assertion_mutation.get("status_counts") or {},
        "assertion_strength_by_construct": primary_by_construct,
        "assertion_strength_return_subcategories": return_subcategories,
        "assertion_strength_return_object_rules": return_object_rules,
        "exception_behavior_excluding_broad_original_scope": exception_excluding_broad_scope,
        "side_effect_research_scope": side_effect_scope,
        "side_effect_research_by_construct": side_effect_by_construct,
        "assertion_strength_items": all_items[:100],
    }


def return_object_rule_summary(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    score_sums: Counter[str] = Counter()
    for item in items:
        if item.get("construct") != "return_behavior":
            continue
        rules = [
            str(mutant.get("operator_rule") or "")
            for mutant in item.get("mutants") or []
            if return_subcategory_for_operator_rule(mutant.get("operator_rule")) == "object"
        ]
        if not rules:
            continue
        rule = ";".join(sorted(set(rules)))
        stats = counters[rule]
        stats["total"] += 1
        if item.get("status") != "not_reached":
            stats["reached"] += 1
        if item.get("represented"):
            stats["represented"] += 1
        if item.get("score") is not None:
            stats["scorable"] += 1
            score_sums[rule] += float(item.get("score") or 0)
            if float(item.get("score") or 0) > 0:
                stats["checked"] += 1
        if item.get("outcome") == "killed_incidentally":
            stats["incidental"] += 1
        if item.get("status") == "not_reached":
            stats["not_reached"] += 1
        if item.get("status") == "not_generated" or item.get("outcome") == "not_generated":
            stats["not_generated"] += 1
        if item.get("status") == "compile_error" or item.get("outcome") == "compile_error":
            stats["compile_error"] += 1
        if item.get("status") == "timeout" or item.get("outcome") == "timeout":
            stats["timeout"] += 1
        if item.get("status") == "out_of_scope" or item.get("outcome") == "out_of_scope":
            stats["out_of_scope"] += 1
        if (
            item.get("status") in {"not_generated", "out_of_scope", "compile_error", "timeout", "coverage_unverified"}
            or item.get("outcome") in {"not_generated", "out_of_scope", "compile_error", "timeout", "coverage_unverified"}
        ):
            stats["non_representable"] += 1
        if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or []):
            stats["unobservable_return_state"] += 1
        for mutant in item.get("mutants") or []:
            signal = mutant.get("observability_signal")
            if signal:
                stats[f"observability_signal_{signal}"] += 1
    return {
        rule: {
            "total": int(stats["total"]),
            "applicable": int(stats["total"]),
            "reached": int(stats["reached"]),
            "represented": int(stats["represented"]),
            "scorable": int(stats["scorable"]),
            "checked": int(stats["checked"]),
            "score_sum": float(score_sums[rule]),
            "score": float(score_sums[rule]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "representability": int(stats["represented"]) / int(stats["reached"]) if int(stats["reached"]) else None,
            "incidental": int(stats["incidental"]),
            "incidental_rate": int(stats["incidental"]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "unobservable_return_state": int(stats["unobservable_return_state"]),
            "not_reached_items": int(stats["not_reached"]),
            "non_representable_items": int(stats["non_representable"]),
            "compile_error_items": int(stats["compile_error"]),
            "timeout_items": int(stats["timeout"]),
            "not_generated_items": int(stats["not_generated"]),
            "out_of_scope_items": int(stats["out_of_scope"]),
            "observability_signals": {
                key.removeprefix("observability_signal_"): int(value)
                for key, value in stats.items()
                if key.startswith("observability_signal_")
            },
        }
        for rule, stats in counters.items()
    }


def normalize_return_subcategory_summary(subcategories: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for subcategory, raw_stats in (subcategories or {}).items():
        if not isinstance(raw_stats, dict):
            continue
        stats = dict(raw_stats)
        total = int(stats.get("applicable") or stats.get("total") or 0)
        not_reached = int(stats.get("not_reached_items") or 0)
        reached = int(stats.get("reached") or max(0, total - not_reached))
        represented = int(stats.get("represented") or 0)
        scorable = int(stats.get("scorable") or 0)
        score_sum = float(stats.get("score_sum") or 0)
        stats.update(
            {
                "total": total,
                "applicable": total,
                "reached": reached,
                "represented": represented,
                "scorable": scorable,
                "checked": int(stats.get("checked") or 0),
                "score_sum": score_sum,
                "score": score_sum / scorable if scorable else None,
                "representability": represented / reached if reached else None,
                "degenerate_null": int(stats.get("degenerate_null") or stats.get("degenerate_null_items") or 0),
                "unobservable_return_state": int(
                    stats.get("unobservable_return_state") or stats.get("unobservable_return_state_items") or 0
                ),
            }
        )
        normalized[str(subcategory)] = stats
    return normalized


def dependency_value_signals(value_code: str) -> set[str]:
    signals: set[str] = set()
    if re.search(r"\bnull\b|Optional\.empty\s*\(", value_code):
        signals.add("null_value")
    if re.search(r"Collections\.empty|List\.of\s*\(\s*\)|Set\.of\s*\(\s*\)|Map\.of\s*\(\s*\)|\"\"|new\s+byte\s*\[\s*0\s*\]", value_code):
        signals.add("empty")
    if re.search(r"\bfalse\b|Boolean\.FALSE\b|(?<![\w.])0(?![\w.])", value_code):
        signals.add("zero")
    if re.search(r"\btrue\b|Boolean\.TRUE\b", value_code):
        signals.add("positive_normal")
    if re.search(r"(?<![\w.])-\s*[1-9]\d*(?![\w.])", value_code):
        signals.add("negative")
    if re.search(r"\b(?:HTTP_OK|HTTP_CREATED|HTTP_ACCEPTED|HTTP_NO_CONTENT)\b|(?<![\w.])2\d\d(?![\w.])", value_code):
        signals.add("http_success_status")
    if re.search(r"\b(?:HTTP_BAD_REQUEST|HTTP_UNAUTHORIZED|HTTP_FORBIDDEN|HTTP_NOT_FOUND|HTTP_INTERNAL_ERROR|HTTP_UNAVAILABLE)\b|(?<![\w.])[45]\d\d(?![\w.])", value_code):
        signals.add("http_error_status")
    if re.search(r"MAX_VALUE|MIN_VALUE|\"[^\"]{80,}\"|new\s+\w+\s*\[\s*(?:100|1000|10000|\d{4,})\s*\]", value_code):
        signals.add("large_or_boundary")
    if re.search(r"\bthrow\s+new\s+(?!AssertionError\b)\w*(?:Exception|Error)\b|\bthenThrow\s*\(|\bdoThrow\s*\(|\bwillThrow\s*\(", value_code):
        signals.add("exception")
    non_normal = {"null_value", "empty", "zero", "negative", "http_error_status", "large_or_boundary", "exception"}
    if (
        re.search(r"\bnew\s+[A-Za-z_]\w*|Optional\.of\s*\(|List\.of\s*\([^)]*\S|Set\.of\s*\([^)]*\S|Map\.of\s*\([^)]*\S|\"[^\"]+\"|'[^']+'|(?<![\w.])-?[1-9]\d*(?![\w.])|\btrue\b", value_code)
        and "exception" not in signals
    ):
        signals.add("positive_normal")
    if not signals - non_normal and re.search(r"\b[A-Za-z_]\w+\b", value_code) and not re.fullmatch(r"\s*(?:null|false|0)\s*", value_code):
        signals.add("positive_normal")
    return signals


def dependency_outcome_signal_evidence(test_code: str) -> dict[str, list[str]]:
    evidence: dict[str, list[str]] = defaultdict(list)
    scoped_statements = []

    for pattern in [
        r"\bwhen\s*\([^;]+?\.thenReturn\s*\(([^;]+?)\)\s*;",
        r"\bgiven\s*\([^;]+?\.willReturn\s*\(([^;]+?)\)\s*;",
        r"\bdoReturn\s*\(([^;]+?)\)\s*\.when\s*\(",
        r"\bthenAnswer\s*\([^;]+?\)\s*;",
        r"\bwillAnswer\s*\([^;]+?\)\s*;",
    ]:
        for match in re.finditer(pattern, test_code, flags=re.MULTILINE | re.DOTALL):
            scoped_statements.append(match.group(0))
            value = match.group(1) if match.lastindex else match.group(0)
            for signal in dependency_value_signals(value):
                evidence[signal].append(match.group(0)[:240])

    for pattern in [
        r"\bthenThrow\s*\([^;]+?\)\s*;",
        r"\bdoThrow\s*\([^;]+?\)\s*\.when\s*\(",
        r"\bwillThrow\s*\([^;]+?\)\s*;",
    ]:
        for match in re.finditer(pattern, test_code, flags=re.MULTILINE | re.DOTALL):
            evidence["exception"].append(match.group(0)[:240])

    scoped_dependency_context = bool(
        re.search(
            r"\b(?:fake|stub|mock)\w*\b|@Override\b|\bextends\b|\bimplements\b|"
            r"\bnew\s+(?:Supplier|Consumer|Function|Predicate|Callback)\b|->",
            test_code,
            flags=re.I | re.MULTILINE,
        )
    )
    if scoped_dependency_context:
        for match in re.finditer(r"\breturn\s+([^;]+?)\s*;", test_code, flags=re.MULTILINE | re.DOTALL):
            for signal in dependency_value_signals(match.group(1)):
                evidence[signal].append(match.group(0)[:240])
        for match in re.finditer(r"->\s*([^;{}\n]+)", test_code, flags=re.MULTILINE):
            for signal in dependency_value_signals(match.group(1)):
                evidence[signal].append(match.group(0)[:240])
        for match in re.finditer(r"\bthrow\s+new\s+(?!AssertionError\b)\w*(?:Exception|Error)\b[^;]*;", test_code):
            evidence["exception"].append(match.group(0)[:240])

    for pattern in [
        r"new\s+ByteArrayInputStream\s*\(([^;]+?)\)",
        r"new\s+ByteArrayOutputStream\s*\(([^;]*?)\)",
        r"Files\.createTemp\w*\s*\([^;]*?\)",
        r"TemporaryFolder|@TempDir|temp(?:Dir|File|Path)",
        r"Clock\.fixed\s*\([^;]+?\)",
    ]:
        for match in re.finditer(pattern, test_code, flags=re.MULTILINE | re.DOTALL | re.I):
            value = match.group(1) if match.lastindex else match.group(0)
            signals = dependency_value_signals(value)
            if "ByteArrayInputStream" in match.group(0) or "ByteArrayOutputStream" in match.group(0):
                signals.add("positive_normal")
            if "Temp" in match.group(0) or "temp" in match.group(0):
                signals.add("positive_normal")
            if "Clock.fixed" in match.group(0):
                signals.add("positive_normal")
            for signal in signals:
                evidence[signal].append(match.group(0)[:240])

    for match in re.finditer(r"\bverify\s*\([^;]+?;", test_code, flags=re.MULTILINE | re.DOTALL):
        evidence["void_or_side_effect"].append(match.group(0)[:240])
    for match in re.finditer(r"\bArgumentCaptor\b|captor\.capture\s*\(", test_code):
        evidence["void_or_side_effect"].append(match.group(0)[:240])

    return {signal: snippets[:10] for signal, snippets in evidence.items()}


def dependency_outcome_signals(test_code: str) -> set[str]:
    return set(dependency_outcome_signal_evidence(test_code))


def dependency_setup_case_evidence_by_kind(test_code: str) -> dict[str, dict[str, list[str]]]:
    evidence = dependency_outcome_signal_evidence(test_code)
    by_kind: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    has_mock = bool(
        re.search(
            r"\b(?:mock|spy|when|given|doReturn|thenReturn|willReturn|thenThrow|doThrow|verify|ArgumentCaptor)\b|"
            r"\b(?:fake|stub)\w*\b|@Override\b|\bimplements\b|URLStreamHandler",
            test_code,
            flags=re.I,
        )
    )
    if has_mock:
        for case, snippets in evidence.items():
            for kind in ["network_or_http"]:
                by_kind[kind][case].extend(snippets)
        if re.search(r"\bverify\s*\(|ArgumentCaptor|captor\.capture", test_code):
            for kind in ["network_or_http"]:
                by_kind[kind]["void_or_side_effect"].append("mock verification or argument capture")
    if re.search(r"ByteArrayInputStream|ByteArrayOutputStream|@TempDir|TemporaryFolder|temp(?:Dir|File|Path)|Files\.createTemp", test_code, flags=re.I):
        for case, snippets in evidence.items():
            by_kind["filesystem_or_stream"][case].extend(snippets)
        by_kind["filesystem_or_stream"]["positive_normal"].append("temporary or in-memory file/stream setup")
        if re.search(r"ByteArrayOutputStream|Files\.read|readString|assert\w*\s*\([^;]*(?:temp|Path|File|ByteArray)", test_code, flags=re.I | re.DOTALL):
            by_kind["filesystem_or_stream"]["void_or_side_effect"].append("file/stream output assertion or capture")
    return {kind: dict(cases) for kind, cases in by_kind.items()}


def dependency_source_cases(source: str, items: list[dict[str, Any]] | None = None) -> set[str]:
    contexts = [str(item.get("snippet") or "") for item in items or []] or [source]
    return external_source_cases_for_context("\n".join(contexts), source)


def assigned_type_from_snippet(snippet: str) -> str:
    match = re.search(
        r"\b(?:final\s+)?([A-Za-z_][\w.$<>?,\[\]]+(?:\s*<[^;=]+>)?(?:\s*\[\])?)\s+[A-Za-z_]\w+\s*=",
        snippet,
    )
    return match.group(1) if match else ""


def external_source_cases_for_context(snippet: str, context: str) -> set[str]:
    cases: set[str] = set()
    assigned_type = assigned_type_from_snippet(snippet).lower()

    if re.search(r"\bgetResponseCode\s*\(", snippet):
        cases.update({"http_success_status", "http_error_status", "exception"})
        return cases

    if re.search(r"\bFiles\.(?:write|delete|copy|move)\w*\s*\(|\bnew\s+FileOutputStream\s*\(", snippet):
        cases.update({"void_or_side_effect", "exception"})
    elif re.search(r"\bFiles\.(?:read|newInputStream)\w*\s*\(|\bnew\s+(?:FileInputStream|InputStreamReader|BufferedReader|FileReader)\s*\(", snippet):
        cases.update({"positive_normal", "empty", "exception"})
    elif re.search(r"\bnew\s+(?:Socket|ServerSocket)\s*\(|\.(?:connect|getInputStream|getOutputStream)\s*\(|\b\w+\.send\s*\(|\b\w+\.execute\s*\(", snippet):
        cases.update({"positive_normal", "exception"})
    if assigned_type:
        if any(name in assigned_type for name in ["int", "integer", "long", "short", "byte", "float", "double", "bigdecimal", "biginteger"]):
            cases.update({"zero", "negative", "positive_normal"})
        elif "string" in assigned_type or "charsequence" in assigned_type:
            cases.update({"empty", "positive_normal"})
        elif "boolean" in assigned_type:
            cases.update({"zero", "positive_normal"})
        elif not re.search(r"\bvoid\b", assigned_type):
            cases.update({"null_value", "positive_normal"})

    clean_context = strip_comments_and_strings(context)
    if re.search(r"\b(?:status|code|timeout|limit|bound|large|MAX_VALUE|MIN_VALUE)\b", clean_context, flags=re.I):
        cases.add("large_or_boundary")
    if re.search(r"\bthrow\s+new\b|\bthrows\b|\bcatch\s*\(|\b(?:IOException|MalformedURLException|FileNotFoundException|SocketException)\b", clean_context):
        cases.add("exception")
    if not cases:
        cases.add("positive_normal")
    return cases


def is_process_backed_stream(snippet: str) -> bool:
    return bool(
        re.search(
            r"\b(?:process|getProcess|proc)\s*\.\s*(?:getInputStream|getErrorStream|getOutputStream)\s*\(|"
            r"\bProcessBuilder\b|\bRuntime\.getRuntime\s*\(\s*\)\.exec\b",
            snippet,
            flags=re.I,
        )
    )


def is_http_or_socket_send_execute(snippet: str) -> bool:
    match = re.search(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\.\s*(send|execute)\s*\(", snippet)
    if not match:
        return False
    receiver = match.group(1).lower()
    if "asynctask" in receiver:
        return False
    return bool(re.search(r"(?:http|client|request|socket|conn|connection|url|rest|web)", receiver))


def classify_branch_condition_strength(
    row: dict[str, Any],
    features: dict[str, Any],
    obligations: dict[str, dict[str, Any]],
    profile: dict[str, dict[str, Any]],
    execution_passed: bool,
) -> dict[str, Any]:
    stats = profile["branch_condition"]
    total = int(stats.get("total") or obligations["branch_condition"].get("total") or 0)
    if total == 0:
        return {
            "branch_condition_strength": "not_applicable",
            "branch_condition_strength_score": None,
            "branch_condition_obligations": 0,
            "branch_condition_covered_items": 0,
            "branch_condition_reached_items": 0,
            "branch_condition_unreached_items": 0,
            "branch_condition_other_status_items": 0,
            "branch_condition_unmutated_items": 0,
            "branch_condition_mutation_covered_items": 0,
            "branch_condition_mutation_coverage_rate": None,
            "if_else_if_condition_total": 0,
            "if_else_if_condition_scorable": 0,
            "if_else_if_condition_checked_sum": 0,
            "if_else_if_condition_checked_score": None,
            "if_else_if_condition_avg_item_score": None,
            "if_else_if_condition_pessimistic_score": None,
            "if_else_if_condition_checked_items": 0,
            "if_else_if_condition_represented": 0,
            "if_else_if_condition_representability_rate": None,
            "if_else_if_condition_both_reached": 0,
            "if_else_if_condition_one_reached": 0,
            "if_else_if_condition_killed": 0,
            "if_else_if_condition_by_construct": {},
            "if_else_if_condition_pit_covered_by_construct": {},
            "if_else_if_condition_pit_total_by_construct": {},
            "if_else_if_condition_scored_items": [],
            "branch_condition_asserted_items": 0,
            "branch_condition_reach_score": None,
            "branch_condition_true_outcomes": 0,
            "branch_condition_false_outcomes": 0,
            "branch_condition_both_outcomes": 0,
            "branch_condition_outcome_evidence_count": 0,
        }

    reached_items_from_profile = int(stats.get("reached_obligation_item_count") or 0)
    unreached_items = int(stats.get("unreached_obligation_item_count") or 0)
    other_status_items = int(stats.get("other_status_obligation_item_count") or 0)
    unmutated_items = int(stats.get("unmutated_obligation_item_count") or 0)
    mutation_covered_items = int(stats.get("mutation_covered_obligation_item_count") or 0)
    outcome_evidence = features.get("branch_condition_outcome_evidence") or {}
    true_outcomes = sum(1 for evidence in outcome_evidence.values() if evidence.get("true"))
    false_outcomes = sum(1 for evidence in outcome_evidence.values() if evidence.get("false"))
    both_outcomes = sum(1 for evidence in outcome_evidence.values() if evidence.get("true") and evidence.get("false"))
    condition_checked_score = stats.get("if_else_if_condition_score")
    condition_avg_item_score = stats.get("if_else_if_condition_avg_item_score")
    condition_checked_sum = float(stats.get("if_else_if_condition_score_sum") or 0)
    condition_pessimistic_score = (
        condition_checked_sum / int(stats.get("if_else_if_condition_total") or 0)
        if int(stats.get("if_else_if_condition_total") or 0)
        else None
    )
    if not execution_passed:
        condition_checked_score = None if int(stats.get("if_else_if_condition_total") or 0) == 0 else 0
        condition_avg_item_score = condition_checked_score
        condition_pessimistic_score = condition_checked_score
        condition_checked_sum = 0

    if not execution_passed:
        reached_items = 0
    else:
        reached_items = reached_items_from_profile
    reached_items = min(total, max(0, reached_items))
    asserted_items = reached_items
    score = reached_items / total
    label = ratio_strength_label(score)

    return {
        "branch_condition_strength": label,
        "branch_condition_strength_score": score,
        "branch_condition_obligations": total,
        "branch_condition_covered_items": asserted_items,
        "branch_condition_reached_items": reached_items,
        "branch_condition_unreached_items": unreached_items if execution_passed else 0,
        "branch_condition_other_status_items": other_status_items if execution_passed else 0,
        "branch_condition_unmutated_items": unmutated_items if execution_passed else total,
        "branch_condition_mutation_covered_items": mutation_covered_items if execution_passed else 0,
        "branch_condition_mutation_coverage_rate": (
            mutation_covered_items / total if execution_passed and total else None
        ),
        "if_else_if_condition_total": int(stats.get("if_else_if_condition_total") or 0),
        "if_else_if_condition_scorable": int(stats.get("if_else_if_condition_scorable") or 0),
        "if_else_if_condition_checked_sum": condition_checked_sum,
        "if_else_if_condition_checked_score": condition_checked_score,
        "if_else_if_condition_avg_item_score": condition_avg_item_score,
        "if_else_if_condition_pessimistic_score": condition_pessimistic_score,
        "if_else_if_condition_checked_items": int(stats.get("if_else_if_condition_checked_items") or 0) if execution_passed else 0,
        "if_else_if_condition_represented": int(stats.get("if_else_if_condition_represented") or 0) if execution_passed else 0,
        "if_else_if_condition_representability_rate": stats.get("if_else_if_condition_representability_rate") if execution_passed else None,
        "if_else_if_condition_both_reached": int(stats.get("if_else_if_condition_both_reached") or 0) if execution_passed else 0,
        "if_else_if_condition_one_reached": int(stats.get("if_else_if_condition_one_reached") or 0) if execution_passed else 0,
        "if_else_if_condition_killed": int(stats.get("if_else_if_condition_killed") or 0) if execution_passed else 0,
        "if_else_if_condition_by_construct": stats.get("if_else_if_condition_by_construct") or {},
        "if_else_if_condition_pit_covered_by_construct": stats.get("if_else_if_condition_pit_covered_by_construct") or {},
        "if_else_if_condition_pit_total_by_construct": stats.get("if_else_if_condition_pit_total_by_construct") or {},
        "if_else_if_condition_scored_items": stats.get("if_else_if_condition_scored_items") or [],
        "branch_condition_asserted_items": asserted_items,
        "branch_condition_reach_score": reached_items / total,
        "branch_condition_true_outcomes": true_outcomes,
        "branch_condition_false_outcomes": false_outcomes,
        "branch_condition_both_outcomes": both_outcomes,
        "branch_condition_outcome_evidence_count": len(outcome_evidence),
    }


def classify_state_transition_strength(
    features: dict[str, Any],
    obligations: dict[str, dict[str, Any]],
    profile: dict[str, dict[str, Any]],
    execution_passed: bool,
) -> dict[str, Any]:
    stats = profile["state_transition"]
    total = int(stats.get("total") or obligations["state_transition"].get("total") or 0)
    if total == 0:
        return {
            "state_transition_strength": "not_applicable",
            "state_transition_strength_score": None,
            "state_transition_obligations": 0,
            "state_transition_covered_items": 0,
            "state_transition_asserted_items": 0,
            "state_transition_setup_items": 0,
            "state_transition_setup_score": None,
            "state_transition_sequence_order_score": None,
            "state_transition_setup_evidence_items": [],
            "state_transition_assertion_evidence_items": [],
            "state_transition_sequence_assertion": False,
            "state_transition_near_all_items": False,
        }

    covered_items = max(0, int(stats["total"]) - int(stats["missed"]))
    asserted = features.get("asserted_observable_behaviors") or {}
    state_asserted = bool(asserted.get("state_change"))
    target_calls = int(features.get("boundary_target_call_count") or features.get("target_calls") or 0)
    semantic_assertions = int(features.get("semantic_assertion_count") or 0)
    method_name = features.get("boundary_target_method")
    state_items = list(stats.get("all_obligation_items") or stats.get("obligation_items") or [])
    setup_state_items, directly_asserted_state_items = state_case_setup_and_assertion_items(
        str(features.get("test_code") or ""),
        method_name,
        state_items,
    )
    sequence_assertion = bool(
        state_asserted
        and has_explicit_state_sequence_assertion(str(features.get("test_code") or ""), method_name, semantic_assertions)
    )
    near_all_items = (
        covered_items >= total
        if total <= 2
        else covered_items >= max(total - 1, int(total * 0.9))
    )

    if not execution_passed:
        asserted_items = 0
        setup_items = 0
    else:
        asserted_items = min(total, len(directly_asserted_state_items))
        setup_items = min(total, len(setup_state_items))
    score = asserted_items / total
    setup_score = setup_items / total
    sequence_order_score = state_sequence_order_score(str(features.get("test_code") or ""), method_name, state_items)
    label = ratio_strength_label(score)

    return {
        "state_transition_strength": label,
        "state_transition_strength_score": score,
        "state_transition_obligations": total,
        "state_transition_covered_items": covered_items,
        "state_transition_asserted_items": asserted_items,
        "state_transition_setup_items": setup_items,
        "state_transition_setup_score": setup_score,
        "state_transition_sequence_order_score": sequence_order_score,
        "state_transition_setup_evidence_items": setup_state_items[:50],
        "state_transition_assertion_evidence_items": directly_asserted_state_items[:50],
        "state_transition_sequence_assertion": sequence_assertion,
        "state_transition_near_all_items": near_all_items,
    }


def classify_interaction_dependency_strength(
    test_code: str,
    features: dict[str, Any],
    obligations: dict[str, dict[str, Any]],
    profile: dict[str, dict[str, Any]],
    execution_passed: bool,
    source: str,
) -> dict[str, Any]:
    stats = profile["interaction_dependency"]
    dependency_items = list(stats.get("all_obligation_items") or stats.get("obligation_items") or [])
    total = len(dependency_items) or int(stats.get("total") or obligations["interaction_dependency"].get("total") or 0)
    if total == 0:
        return {
            "interaction_dependency_strength": "not_applicable",
            "interaction_dependency_strength_score": None,
            "interaction_dependency_obligations": 0,
            "interaction_dependency_covered_items": 0,
            "interaction_dependency_asserted_items": 0,
            "interaction_dependency_outcome_signals": [],
            "interaction_dependency_outcome_evidence": {},
            "interaction_dependency_applicable_cases": [],
            "interaction_dependency_achieved_cases": [],
            "interaction_dependency_applicable_kind_cases": [],
            "interaction_dependency_achieved_kind_cases": [],
            "interaction_dependency_case_scores": {},
            "interaction_dependency_checked_items": [],
            "interaction_dependency_unchecked_items": [],
            "interaction_dependency_setup_evidence_by_kind": {},
        }

    covered_items = max(0, int(stats["total"]) - int(stats["missed"]))
    outcome_evidence = dependency_outcome_signal_evidence(test_code)
    setup_by_kind = dependency_setup_case_evidence_by_kind(test_code)
    checked_items = []
    unchecked_items = []
    applicable_cases = Counter()
    achieved_cases = Counter()
    applicable_kind_cases = Counter()
    achieved_kind_cases = Counter()
    for item in dependency_items:
        kind = str(item.get("dependency_kind") or dependency_kind(str(item.get("snippet") or "")))
        case = str(item.get("dependency_case") or "positive_normal")
        kind_case = f"{kind}::{case}"
        applicable_cases[case] += 1
        applicable_kind_cases[kind_case] += 1
        compatible_evidence = setup_by_kind.get(kind, {}).get(case, [])
        if not compatible_evidence and case == "positive_normal":
            compatible_evidence = setup_by_kind.get(kind, {}).get("void_or_side_effect", [])
        item_out = {**item, "dependency_kind": kind, "dependency_case": case}
        if compatible_evidence:
            achieved_cases[case] += 1
            achieved_kind_cases[kind_case] += 1
            checked_items.append({**item_out, "setup_evidence": compatible_evidence[:5]})
        else:
            unchecked_items.append(item_out)

    if not execution_passed:
        asserted_items = 0
        score = 0
        checked_items = []
    else:
        asserted_items = len(checked_items)
        score = asserted_items / total
    label = ratio_strength_label(score)

    return {
        "interaction_dependency_strength": label,
        "interaction_dependency_strength_score": score,
        "interaction_dependency_obligations": total,
        "interaction_dependency_covered_items": covered_items,
        "interaction_dependency_asserted_items": asserted_items,
        "interaction_dependency_outcome_signals": sorted(outcome_evidence),
        "interaction_dependency_outcome_evidence": outcome_evidence,
        "interaction_dependency_applicable_cases": sorted(applicable_cases.elements()),
        "interaction_dependency_achieved_cases": sorted(achieved_cases.elements()) if execution_passed else [],
        "interaction_dependency_applicable_kind_cases": sorted(applicable_kind_cases.elements()),
        "interaction_dependency_achieved_kind_cases": sorted(achieved_kind_cases.elements()) if execution_passed else [],
        "interaction_dependency_case_scores": {
            case: achieved_cases[case] / applicable_cases[case] if execution_passed and applicable_cases[case] else 0
            for case in sorted(applicable_cases)
        },
        "interaction_dependency_checked_items": checked_items[:50],
        "interaction_dependency_unchecked_items": unchecked_items[:50],
        "interaction_dependency_setup_evidence_by_kind": setup_by_kind,
    }


def classify_side_effect_strength(features: dict[str, Any]) -> dict[str, Any]:
    state_total = int(features.get("state_transition_obligations") or 0)
    state_checked = int(features.get("state_transition_asserted_items") or 0)
    external_total = int(features.get("interaction_dependency_obligations") or 0)
    external_checked = int(features.get("interaction_dependency_asserted_items") or 0)
    total = state_total + external_total
    checked = state_checked + external_checked
    score = checked / total if total else None
    return {
        "side_effect_strength": ratio_strength_label(score),
        "side_effect_strength_score": score,
        "side_effect_obligations": total,
        "side_effect_checked_items": checked,
        "side_effect_state_obligations": state_total,
        "side_effect_state_checked_items": state_checked,
        "side_effect_external_obligations": external_total,
        "side_effect_external_checked_items": external_checked,
    }


def source_obligations(source: str, signature: str | None = None) -> dict[str, dict[str, Any]]:
    boundary_hits = {
        name: bool(re.search(pattern, source, flags=re.MULTILINE))
        for name, pattern in BOUNDARY_SOURCE_PATTERNS.items()
    }
    boundary_total = sum(boundary_hits.values())
    branch_total = len(control_flow_obligation_items(source, None))
    exception_total = len(THROW_RE.findall(source))
    state_total = 0
    interaction_total = len(external_dependency_obligation_items(source, signature))
    return {
        "boundary_value": {"total": boundary_total, "signals": [k for k, v in boundary_hits.items() if v]},
        "exception_path": {"total": exception_total, "signals": ["throw_or_declared_exception"] if exception_total else []},
        "weak_oracle": {"total": 1, "signals": ["observable_test_oracle"]},
        "branch_condition": {"total": branch_total, "signals": ["branch_guard"] if branch_total else []},
        "state_transition": {"total": state_total, "signals": ["static_or_global_state_side_effect"] if state_total else []},
        "interaction_dependency": {"total": interaction_total, "signals": ["external_side_effect"] if interaction_total else []},
    }


def line_items_for_pattern(source_lines: list[str], category: str, signal: str, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    items = []
    for line_number, line in enumerate(source_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("import ", "package ", "//", "/*", "*")):
            continue
        if pattern.search(line):
            items.append(
                {
                    "category": category,
                    "signal": signal,
                    "line": line_number,
                    "snippet": stripped[:240],
                }
            )
    return items


def has_state_accessor(source: str, field_name: str) -> bool:
    capitalized = field_name[:1].upper() + field_name[1:]
    accessor_names = [
        rf"get{re.escape(capitalized)}",
        rf"is{re.escape(capitalized)}",
        rf"has{re.escape(capitalized)}",
        rf"{re.escape(field_name)}",
    ]
    accessor_re = re.compile(
        r"\b(?:public|protected)?\s*(?:static\s+)?[A-Za-z_][\w.$<>?,\[\]]*\s+"
        rf"(?:{'|'.join(accessor_names)})\s*\(",
        re.MULTILINE,
    )
    return bool(accessor_re.search(source))


def state_mutation_target(snippet: str) -> str | None:
    for pattern in [
        r"\bthis\.([A-Za-z_]\w*)\s*=",
        r"\b([A-Za-z_]\w*)\s*=(?!=)",
        r"\b([A-Za-z_]\w*)\s*(?:\+\+|--|\+=|-=)",
        r"\b([A-Za-z_]\w*)\.(?:add|put|remove|clear|set)\s*\(",
    ]:
        match = re.search(pattern, snippet)
        if match:
            return match.group(1)
    return None


def static_state_fields(source: str) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    brace_depth = 0
    for line in source.splitlines():
        stripped = line.strip()
        current_depth = brace_depth
        brace_depth += line.count("{") - line.count("}")
        if current_depth != 1:
            continue
        if not stripped or stripped.startswith(("import ", "package ", "//", "*")):
            continue
        if " static " not in f" {stripped} ":
            continue
        if "(" in stripped and not re.search(r"\)\s*(?:=|;)", stripped):
            continue
        field_match = re.match(
            r"(?:public|protected|private|static|final|volatile|transient|\s)*"
            r"([A-Za-z_][\w.$<>?,\[\]]+(?:\s*<[^;=]+>)?(?:\s*\[\])?)\s+([A-Za-z_]\w*)\s*(?:=|;)",
            stripped,
        )
        if not field_match:
            continue
        field_type = field_match.group(1).lower()
        field_name = field_match.group(2)
        is_constant_like = (
            " final " in f" {stripped} "
            and any(name in field_type for name in ["int", "long", "short", "byte", "float", "double", "boolean", "string", "char"])
            and not is_collection_or_array_parameter_type(field_match.group(1))
        )
        if is_constant_like:
            continue
        if " private " in f" {stripped} " and not has_state_accessor(source, field_name):
            continue
        fields[field_name] = {
            "name": field_name,
            "type": field_match.group(1),
            "declaration": stripped[:240],
        }
    return fields


def static_state_names(source: str) -> set[str]:
    return set(static_state_fields(source))


def state_initial_cases_from_type(field_type: str) -> list[str]:
    t = field_type.lower()
    if "boolean" in t:
        return ["false_value", "true_value"]
    if is_collection_or_array_parameter_type(field_type):
        return ["empty", "positive_normal", "duplicate"]
    if any(name in t for name in ["int", "integer", "long", "short", "byte", "float", "double", "bigdecimal", "biginteger"]):
        return ["zero", "negative", "positive_normal"]
    if "string" in t or "charsequence" in t:
        return ["empty", "positive_normal", "overflow_or_long"]
    return ["positive_normal"]


def target_method_line_bounds(source: str, signature: str | None) -> tuple[int, int] | None:
    method = target_method_source(source, signature)
    if not method:
        return None
    start = source.find(method)
    if start < 0:
        return None
    start_line = source[:start].count("\n") + 1
    end_line = start_line + method.count("\n")
    return start_line, end_line


def is_in_target_line_range(line_number: int, bounds: tuple[int, int] | None) -> bool:
    if bounds is None:
        return True
    return bounds[0] <= line_number <= bounds[1]


def is_local_declaration_assignment(snippet: str) -> bool:
    return bool(
        re.match(
            r"(?:final\s+)?[A-Za-z_][\w.$<>?,\[\]]+(?:\s*<[^;=]+>)?\s+[A-Za-z_]\w+\s*=",
            snippet.strip(),
        )
    )


def state_obligation_items(source: str, signature: str | None = None) -> list[dict[str, Any]]:
    state_fields = static_state_fields(source)
    state_names = set(state_fields)
    line_bounds = target_method_line_bounds(source, signature)
    items = []
    for item in line_items_for_pattern(source.splitlines(), "state_transition", "static_or_global_state_side_effect", STATE_SOURCE_RE):
        line = int(item.get("line") or 0)
        snippet = str(item.get("snippet") or "")
        clean_snippet = strip_comments_and_strings(snippet)
        if not is_in_target_line_range(line, line_bounds):
            continue
        if not STATE_SOURCE_RE.search(clean_snippet):
            continue
        if is_local_declaration_assignment(snippet):
            continue
        target = state_mutation_target(clean_snippet)
        if target and target in state_names:
            field = state_fields.get(target, {})
            for case in state_initial_cases_from_type(str(field.get("type") or "")):
                items.append(
                    {
                        **item,
                        "state_target": target,
                        "state_scope": "static_field",
                        "state_case": case,
                        "state_type": field.get("type"),
                    }
                )
            continue
        qualified = re.search(
            r"\b([A-Z][A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*(?:=(?!=)|\+\+|--|\+=|-=|\.add\s*\(|\.put\s*\(|\.remove\s*\(|\.clear\s*\(|\.set\s*\()",
            clean_snippet,
        )
        if qualified:
            target_name = f"{qualified.group(1)}.{qualified.group(2)}"
            for case in ["positive_normal"]:
                items.append(
                    {
                        **item,
                        "state_target": target_name,
                        "state_scope": "static_class_property",
                        "state_case": case,
                    }
                )
    return items


EXTERNAL_DEPENDENCY_RE = re.compile(
    r"\bnew\s+(?:Socket|ServerSocket)\s*\(|"
    r"\.(?:connect|getInputStream|getOutputStream|getResponseCode)\s*\(|"
    r"\b\w+\.(?:send|execute)\s*\(|"
    r"\bFiles\.(?:read|write|create|delete|copy|move|newInputStream|newOutputStream)\w*\s*\(|"
    r"\bnew\s+(?:FileInputStream|FileOutputStream|InputStreamReader|OutputStreamWriter|FileReader|FileWriter|BufferedReader|BufferedWriter)\s*\(",
    re.MULTILINE,
)


def external_dependency_obligation_items(source: str, signature: str | None = None) -> list[dict[str, Any]]:
    source_lines = source.splitlines()
    line_bounds = target_method_line_bounds(source, signature)
    method_source = target_method_source(source, signature) or source
    items = []
    for item in line_items_for_pattern(source_lines, "interaction_dependency", "external_side_effect", EXTERNAL_DEPENDENCY_RE):
        line = int(item.get("line") or 0)
        snippet = str(item.get("snippet") or "")
        if is_process_backed_stream(snippet):
            continue
        if re.search(r"\.\s*(?:send|execute)\s*\(", snippet) and not is_http_or_socket_send_execute(snippet):
            continue
        if is_in_target_line_range(line, line_bounds):
            kind = dependency_kind(snippet)
            cases = dependency_source_cases(method_source, [item])
            for case in sorted(cases or {"positive_normal"}):
                items.append(
                    {
                        **item,
                        "dependency_kind": kind,
                        "dependency_case": case,
                    }
                )
    return items


def source_obligation_items(source: str, signature: str | None = None) -> dict[str, list[dict[str, Any]]]:
    source_lines = source.splitlines()
    items: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORIES}
    items["exception_path"] = []
    for signal, pattern in BOUNDARY_SOURCE_PATTERNS.items():
        category = BOUNDARY_SOURCE_TO_CATEGORY.get(signal, signal)
        items["boundary_value"].extend(
            line_items_for_pattern(source_lines, "boundary_value", category, re.compile(pattern))
        )
    items["exception_path"].extend(line_items_for_pattern(source_lines, "exception_path", "throw_or_declared_exception", THROW_RE))
    items["branch_condition"].extend(control_flow_obligation_items(source, None))
    # State transition is excluded from the current paper/reviewer scope; avoid
    # the legacy static extraction path during production analysis.
    items["interaction_dependency"].extend(external_dependency_obligation_items(source, signature))
    items["weak_oracle"].append(
        {
            "category": "weak_oracle",
            "signal": "observable_test_oracle",
            "line": None,
            "snippet": "Target method requires assertions for observable return, exception, state, or side-effect behavior.",
        }
    )
    return items


def source_context_for_line(source_lines: list[str], line_number: int | None, radius: int = 2) -> str:
    if not line_number or line_number < 1 or line_number > len(source_lines):
        return ""
    start = max(0, line_number - 1 - radius)
    end = min(len(source_lines), line_number + radius)
    return "\n".join(source_lines[start:end])


def categorize_mutator(mutator: str, description: str, status: str, source_context: str = "") -> set[str]:
    mutator = mutator.lower()
    desc = description.lower()
    categories = set()
    if "negateconditionals" in mutator or "conditionalsboundary" in mutator or "conditional" in desc:
        categories.add("branch_condition")
    if "return" in mutator or "return" in desc:
        categories.add("weak_oracle")
        if "null" in mutator or "null" in desc or "empty" in mutator or "empty" in desc:
            categories.add("boundary_value")
    if "mathmutator" in mutator or "increments" in mutator or "replaced integer" in desc or "changed increment" in desc:
        categories.add("boundary_value")
    if "constructorcall" in mutator or "nonvoidmethodcall" in mutator:
        categories.add("interaction_dependency")
    if source_context:
        if re.search(r"\bif\s*\(|\belse\b|\bswitch\s*\(|\bcase\s+|\bdefault\s*:|\bfor\s*\(|\bwhile\s*\(|\btry\s*\{|\bcatch\s*\(|\bthrow\b|\bthrows\b|\?", source_context):
            categories.add("branch_condition")
        if EXCEPTION_LINE_RE.search(source_context):
            categories.add("exception_path")
        if INTERACTION_LINE_RE.search(source_context):
            categories.add("interaction_dependency")
        if BOUNDARY_LINE_RE.search(source_context):
            categories.add("boundary_value")
    return categories


def pit_counters_from_details(details: list[dict[str, Any]], source: str = "") -> dict[str, Counter]:
    counters: dict[str, Counter] = defaultdict(Counter)
    source_lines = source.splitlines()
    for mutation in details:
        status = str(mutation.get("status") or "UNKNOWN")
        mutator = str(mutation.get("mutator") or "")
        desc = str(mutation.get("description") or "")
        source_context = source_context_for_line(source_lines, mutation.get("line_number"))
        for category in categorize_mutator(mutator, desc, status, source_context):
            counters[category][status] += 1
            counters[category]["total"] += 1
    return counters


def pit_items_from_details(details: list[dict[str, Any]], source: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    source_lines = source.splitlines()
    for mutation in details:
        status = str(mutation.get("status") or "UNKNOWN")
        mutator = str(mutation.get("mutator") or "")
        desc = str(mutation.get("description") or "")
        line_number = mutation.get("line_number")
        source_context = source_context_for_line(source_lines, line_number)
        for category in categorize_mutator(mutator, desc, status, source_context):
            items.append(
                {
                    "category": category,
                    "line": line_number,
                    "status": status,
                    "mutator": mutator,
                    "description": desc,
                    "number_of_tests_run": mutation.get("number_of_tests_run"),
                    "source_context": source_context[:500],
                }
            )
    return items


def parse_pit_mutants(workdir: str | None, row_details: list[dict[str, Any]] | None = None, source: str = "") -> dict[str, Counter]:
    if row_details:
        return pit_counters_from_details(row_details, source)
    counters: dict[str, Counter] = defaultdict(Counter)
    if not workdir:
        return counters
    path = Path(workdir) / "target/pit-reports/mutations.xml"
    if not path.exists():
        return counters
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return counters
    source_lines = source.splitlines()
    for mutation in root.findall("mutation"):
        status = mutation.attrib.get("status", "UNKNOWN")
        mutator = mutation.findtext("mutator") or ""
        desc = mutation.findtext("description") or ""
        line_number = int(mutation.findtext("lineNumber") or 0) or None
        source_context = source_context_for_line(source_lines, line_number)
        for category in categorize_mutator(mutator, desc, status, source_context):
            counters[category][status] += 1
            counters[category]["total"] += 1
    return counters


def parse_pit_items(workdir: str | None, row_details: list[dict[str, Any]] | None = None, source: str = "") -> list[dict[str, Any]]:
    if row_details:
        return pit_items_from_details(row_details, source)
    if not workdir:
        return []
    path = Path(workdir) / "target/pit-reports/mutations.xml"
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    details: list[dict[str, Any]] = []
    for mutation in root.findall("mutation"):
        details.append(
            {
                "status": mutation.attrib.get("status", "UNKNOWN"),
                "mutator": mutation.findtext("mutator") or "",
                "description": mutation.findtext("description") or "",
                "line_number": int(mutation.findtext("lineNumber") or 0) or None,
                "number_of_tests_run": mutation.findtext("numberOfTestsRun"),
            }
        )
    return pit_items_from_details(details, source)


def tested_boundary_signals(test_code: str) -> set[str]:
    return {
        name
        for name, pattern in BOUNDARY_TEST_PATTERNS.items()
        if re.search(pattern, test_code, flags=re.MULTILINE | re.DOTALL)
    }


PIT_MISS_STATUSES = {"SURVIVED", "NO_COVERAGE"}
PIT_COVER_STATUSES = {"KILLED"}
CONDITION_MUTATOR_RE = re.compile(
    r"NegateConditionalsMutator|ConditionalsBoundaryMutator|conditional",
    re.IGNORECASE,
)


def pit_items_by_category_line(pit_items: list[dict[str, Any]]) -> dict[str, dict[int, list[dict[str, Any]]]]:
    by_category_line: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for item in pit_items:
        line = item.get("line")
        if isinstance(line, int):
            by_category_line[str(item.get("category"))][line].append(item)
    return by_category_line


def candidate_pit_lines(item: dict[str, Any]) -> list[int]:
    lines = []
    for line in item.get("candidate_pit_lines") or [item.get("line")]:
        if isinstance(line, int):
            lines.append(line)
    return sorted(set(lines))


def source_control_flow_mutation_profile(
    obligation_items: list[dict[str, Any]],
    pit_items: list[dict[str, Any]],
    *,
    custom_extreme_available: bool = False,
) -> dict[str, Any]:
    """Map PIT statuses onto our source-level control-flow obligations.

    The denominator is always the source-obligation list, not PIT. PIT only
    supplies evidence about whether each obligation is represented and reached.
    """
    by_line = pit_items_by_category_line(pit_items).get("branch_condition", {})
    reached_items: list[dict[str, Any]] = []
    unreached_items: list[dict[str, Any]] = []
    other_status_items: list[dict[str, Any]] = []
    unmutated_items: list[dict[str, Any]] = []
    classified_items: list[dict[str, Any]] = []

    for item in obligation_items:
        if item.get("construct") == "ternary" and not custom_extreme_available:
            item_out = {
                **item,
                "pit_statuses": [],
                "pit_mutators": [],
                "pit_descriptions": [],
                "pit_mutation_count": 0,
                "coverage_imprecise": True,
                "coverage_source": "custom_extreme_mutation_missing",
                "mutation_representability_status": "custom_extreme_missing",
            }
            unmutated_items.append(item_out)
            classified_items.append(item_out)
            continue
        line_pit_items = [
            pit_item
            for line in candidate_pit_lines(item)
            for pit_item in by_line.get(line, [])
        ]
        statuses = {str(pit_item.get("status") or "UNKNOWN") for pit_item in line_pit_items}
        item_out = {
            **item,
            "pit_statuses": sorted(statuses),
            "pit_mutators": sorted({str(pit_item.get("mutator") or "") for pit_item in line_pit_items if pit_item.get("mutator")}),
            "pit_descriptions": sorted({str(pit_item.get("description") or "") for pit_item in line_pit_items if pit_item.get("description")}),
            "pit_mutation_count": len(line_pit_items),
            "coverage_imprecise": item.get("construct") == "ternary",
            "coverage_source": "custom_extreme_mutation" if item.get("construct") == "ternary" and custom_extreme_available else "mutation_line_match",
        }
        if statuses & PIT_COVER_STATUSES:
            item_out["mutation_representability_status"] = "killed"
            reached_items.append(item_out)
        elif statuses & PIT_MISS_STATUSES:
            item_out["mutation_representability_status"] = "survived_or_no_coverage"
            unreached_items.append(item_out)
        elif line_pit_items:
            item_out["mutation_representability_status"] = "other_status"
            other_status_items.append(item_out)
        else:
            item_out["mutation_representability_status"] = "unmutated"
            unmutated_items.append(item_out)
        classified_items.append(item_out)

    total = len(obligation_items)
    mutation_covered = len(reached_items) + len(unreached_items)
    pit_covered_by_construct: dict[str, int] = {}
    pit_total_by_construct: dict[str, int] = {}
    for item in classified_items:
        c = str(item.get("construct") or "unknown")
        pit_total_by_construct[c] = pit_total_by_construct.get(c, 0) + 1
        if item.get("mutation_representability_status") not in ("unmutated", "custom_extreme_missing"):
            pit_covered_by_construct[c] = pit_covered_by_construct.get(c, 0) + 1
    return {
        "total": total,
        "reached": len(reached_items),
        "unreached": len(unreached_items),
        "other_status": len(other_status_items),
        "unmutated": len(unmutated_items),
        "mutation_covered": mutation_covered,
        "mutation_coverage_rate": mutation_covered / total if total else None,
        "pit_covered_by_construct": pit_covered_by_construct,
        "pit_total_by_construct": pit_total_by_construct,
        "reached_items": reached_items[:50],
        "unreached_items": unreached_items[:50],
        "other_status_items": other_status_items[:50],
        "unmutated_items": unmutated_items[:50],
        "classified_items": classified_items,
    }


def condition_line_has_killed_mutant(item: dict[str, Any]) -> bool:
    if str(item.get("construct") or "") not in {"if", "else_if"}:
        return False
    statuses = set(item.get("pit_statuses") or [])
    if "KILLED" not in statuses:
        return False
    mutators = " ".join(str(value) for value in item.get("pit_mutators") or [])
    descriptions = " ".join(str(value) for value in item.get("pit_descriptions") or [])
    if mutators or descriptions:
        return bool(CONDITION_MUTATOR_RE.search(mutators) or CONDITION_MUTATOR_RE.search(descriptions))
    return True


def condition_line_has_represented_mutant(item: dict[str, Any]) -> bool:
    if str(item.get("construct") or "") not in {"if", "else_if"}:
        return False
    statuses = set(item.get("pit_statuses") or [])
    if not statuses & (PIT_COVER_STATUSES | PIT_MISS_STATUSES):
        return False
    mutators = " ".join(str(value) for value in item.get("pit_mutators") or [])
    descriptions = " ".join(str(value) for value in item.get("pit_descriptions") or [])
    if mutators or descriptions:
        return bool(CONDITION_MUTATOR_RE.search(mutators) or CONDITION_MUTATOR_RE.search(descriptions))
    return True


def if_else_if_checked_condition_profile(
    branch_items: list[dict[str, Any]],
    outcome_evidence: dict[Any, dict[str, Any]],
    execution_passed: bool,
) -> dict[str, Any]:
    condition_items = [
        item
        for item in branch_items
        if item.get("construct") in {"if", "else_if"}
        and not item.get("excluded_from_main_control_flow")
    ]
    scored_items: list[dict[str, Any]] = []
    score_sum = 0.0
    both_reached = 0
    one_reached = 0
    killed_conditions = 0
    represented_conditions = 0
    checked_conditions = 0
    by_construct: dict[str, dict[str, Any]] = {
        "if": {"total": 0, "score_sum": 0.0, "checked": 0, "represented": 0, "killed": 0},
        "else_if": {"total": 0, "score_sum": 0.0, "checked": 0, "represented": 0, "killed": 0},
    }
    for item in condition_items:
        construct = str(item.get("construct") or "")
        by_construct.setdefault(construct, {"total": 0, "score_sum": 0.0, "checked": 0, "represented": 0, "killed": 0})
        by_construct[construct]["total"] += 1
        line = item.get("line")
        evidence = outcome_evidence.get(line) or outcome_evidence.get(str(line)) or {}
        has_true = bool(evidence.get("true"))
        has_false = bool(evidence.get("false"))
        reached_count = int(has_true) + int(has_false)
        killed = condition_line_has_killed_mutant(item)
        represented = condition_line_has_represented_mutant(item)
        if reached_count == 2:
            both_reached += 1
        elif reached_count == 1:
            one_reached += 1
        if represented:
            represented_conditions += 1
            by_construct[construct]["represented"] += 1
        if killed:
            killed_conditions += 1
            by_construct[construct]["killed"] += 1
        score = 0.0
        reason = "not_reached_or_not_killed"
        if execution_passed and killed and reached_count == 2:
            score = 1.0
            reason = "both_outcomes_reached_and_condition_mutant_killed"
        elif execution_passed and killed and reached_count == 1:
            score = 0.5
            reason = "one_outcome_reached_and_condition_mutant_killed"
        score_sum += score
        by_construct[construct]["score_sum"] += score
        if score > 0:
            checked_conditions += 1
            by_construct[construct]["checked"] += 1
        scored_items.append(
            {
                "line": line,
                "construct": item.get("construct"),
                "condition": item.get("condition"),
                "snippet": item.get("snippet"),
                "true_reached": has_true,
                "false_reached": has_false,
                "condition_mutant_killed": killed,
                "condition_mutant_represented": represented,
                "score": score,
                "reason": reason,
                "pit_statuses": item.get("pit_statuses") or [],
                "pit_mutators": item.get("pit_mutators") or [],
                "pit_descriptions": item.get("pit_descriptions") or [],
            }
        )
    total = len(condition_items)
    return {
        "if_else_if_condition_total": total,
        "if_else_if_condition_scorable": total,
        "if_else_if_condition_score_sum": score_sum,
        "if_else_if_condition_score": checked_conditions / total if total else None,
        "if_else_if_condition_avg_item_score": score_sum / total if total else None,
        "if_else_if_condition_pessimistic_score": checked_conditions / total if total else None,
        "if_else_if_condition_checked_items": checked_conditions,
        "if_else_if_condition_represented": represented_conditions,
        "if_else_if_condition_representability_rate": represented_conditions / total if total else None,
        "if_else_if_condition_both_reached": both_reached,
        "if_else_if_condition_one_reached": one_reached,
        "if_else_if_condition_killed": killed_conditions,
        "if_else_if_condition_by_construct": by_construct,
        "if_else_if_condition_scored_items": scored_items[:50],
    }


def forced_condition_outcome_status(item: dict[str, Any], outcome: str) -> str | None:
    if outcome == "true":
        return item.get("condition_false_mutant_status")
    if outcome == "false":
        return item.get("condition_true_mutant_status")
    return None


def status_is_checked(status: Any) -> bool:
    return str(status or "").lower() == "killed"


def extreme_condition_checked_profile(extreme: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(extreme, dict) or extreme.get("status") != "passed":
        return None
    items = list(extreme.get("items") or [])
    total = int(extreme.get("total_conditions") or len(items))
    scorable = int(
        extreme.get("scorable_conditions")
        or sum(1 for item in items if item.get("score") is not None)
    )
    score_sum = float(
        extreme.get("score_sum")
        or sum(float(item.get("score") or 0) for item in items if item.get("score") is not None)
    )
    checked_items = int(
        extreme.get("checked_items")
        or sum(1 for item in items if item.get("score") is not None and float(item.get("score") or 0) > 0)
    )
    fully_checked = int(
        extreme.get("fully_checked_items")
        or sum(1 for item in items if item.get("score") is not None and float(item.get("score") or 0) == 1)
    )
    partially_checked = checked_items - fully_checked
    represented = int(extreme.get("represented_items") or sum(1 for item in items if item.get("represented")))
    by_construct: dict[str, dict[str, Any]] = {
        "if": {"total": 0, "scorable": 0, "score_sum": 0.0, "checked": 0, "represented": 0, "killed": 0},
        "else_if": {"total": 0, "scorable": 0, "score_sum": 0.0, "checked": 0, "represented": 0, "killed": 0},
    }
    for construct, stats in (extreme.get("by_construct") or {}).items():
        total_variants = int(stats.get("total_variants") or 0)
        represented_variants = int(stats.get("represented_variants") or 0)
        represented_items = int(stats.get("represented") or 0)
        if total_variants == 0:
            total_variants = int(stats.get("total") or 0)
            represented_variants = represented_items
        elif represented_variants == 0 and represented_items > 0:
            represented_variants = represented_items
        by_construct[str(construct)] = {
            "total": int(stats.get("total") or 0),
            "scorable": int(stats.get("scorable") or stats.get("represented") or 0),
            "score_sum": float(stats.get("score_sum") or 0),
            "checked": int(stats.get("checked") or 0),
            "represented": represented_items,
            "total_variants": total_variants,
            "represented_variants": represented_variants,
            "variant_representability_rate": represented_variants / total_variants if total_variants else None,
            "killed": int(stats.get("checked") or 0),
            "true_killed": int(stats.get("true_killed") or 0),
            "false_killed": int(stats.get("false_killed") or 0),
        }
    return {
        "if_else_if_condition_total": total,
        "if_else_if_condition_scorable": scorable,
        "if_else_if_condition_score_sum": score_sum,
        "if_else_if_condition_score": checked_items / scorable if scorable else None,
        "if_else_if_condition_avg_item_score": score_sum / scorable if scorable else None,
        "if_else_if_condition_pessimistic_score": checked_items / total if total else None,
        "if_else_if_condition_checked_items": checked_items,
        "if_else_if_condition_represented": represented,
        "if_else_if_condition_representability_rate": represented / total if total else None,
        "if_else_if_condition_both_reached": fully_checked,
        "if_else_if_condition_one_reached": partially_checked,
        "if_else_if_condition_killed": checked_items,
        "if_else_if_condition_by_construct": by_construct,
        "if_else_if_condition_scored_items": items[:50],
    }


def item_match_stats(
    category: str,
    obligation_items: list[dict[str, Any]],
    pit_items: list[dict[str, Any]],
    fallback_covered: bool = False,
) -> dict[str, Any]:
    by_line = pit_items_by_category_line(pit_items).get(category, {})
    matched_items: list[dict[str, Any]] = []
    missed_items: list[dict[str, Any]] = []
    covered = 0
    for item in obligation_items:
        line = item.get("line")
        line_pit_items = by_line.get(line, []) if isinstance(line, int) else []
        statuses = {str(pit_item.get("status")) for pit_item in line_pit_items}
        item_out = {**item, "pit_statuses": sorted(statuses)}
        if statuses & PIT_COVER_STATUSES:
            covered += 1
            matched_items.append(item_out)
        elif line_pit_items and statuses <= PIT_MISS_STATUSES:
            missed_items.append(item_out)
        elif fallback_covered:
            covered += 1
            matched_items.append(item_out)
        else:
            missed_items.append(item_out)
    total = len(obligation_items)
    return {
        "total": total,
        "covered": covered,
        "missed": max(0, total - covered),
        "matched_items": matched_items[:50],
        "missed_items": missed_items[:50],
    }


def category_item_profile(
    category: str,
    obligation_items: list[dict[str, Any]],
    test_features: dict[str, Any],
    pit_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if category == "boundary_value":
        applicable_items = list(test_features.get("boundary_applicable_items") or [])
        if applicable_items:
            matched_items = list(test_features.get("boundary_matched_items") or [])
            missed_items = list(test_features.get("boundary_missed_items") or [])
            return {
                "total": len(applicable_items),
                "covered": len(matched_items),
                "missed": len(missed_items),
                "matched_items": matched_items,
                "missed_items": missed_items,
            }
        applicable = list(test_features.get("boundary_applicable_categories") or [])
        covered_categories = set(test_features.get("boundary_covered_categories") or [])
        if not applicable:
            return None
        matched_items = [
            {"category": category, "signal": signal, "line": None, "snippet": f"Boundary category: {signal}"}
            for signal in applicable
            if signal in covered_categories
        ]
        missed_items = [
            {"category": category, "signal": signal, "line": None, "snippet": f"Boundary category: {signal}"}
            for signal in applicable
            if signal not in covered_categories
        ]
        return {
            "total": len(applicable),
            "covered": len(matched_items),
            "missed": len(missed_items),
            "matched_items": matched_items,
            "missed_items": missed_items,
        }

    if category == "weak_oracle":
        applicable = list(test_features.get("oracle_obligation_items") or [])
        if not applicable:
            return None
        matched_items = list(test_features.get("oracle_matched_items") or [])
        missed_items = list(test_features.get("oracle_missed_items") or [])
        return {
            "total": len(applicable),
            "covered": len(matched_items),
            "missed": len(missed_items),
            "matched_items": matched_items,
            "missed_items": missed_items,
        }

    if category == "exception_path":
        if not obligation_items:
            return None
        fallback_covered = bool(test_features.get("has_target_exception_test"))
        return item_match_stats(category, obligation_items, pit_items, fallback_covered=fallback_covered)

    if category == "branch_condition":
        private_nested_items = [
            item for item in obligation_items if item.get("excluded_from_main_control_flow")
        ]
        obligation_items = [
            item for item in obligation_items if not item.get("excluded_from_main_control_flow")
        ]
        if not obligation_items:
            return {
                "total": 0,
                "covered": 0,
                "reached": 0,
                "unreached": 0,
                "other_status": 0,
                "unmutated": 0,
                "mutation_covered": 0,
                "mutation_coverage_rate": None,
                "missed": 0,
                "reached_items": [],
                "matched_items": [],
                "missed_items": [],
                "excluded_private_nested_items": private_nested_items[:50],
                "excluded_private_nested_count": len(private_nested_items),
                "if_else_if_condition_total": 0,
                "if_else_if_condition_scorable": 0,
                "if_else_if_condition_score_sum": 0,
                "if_else_if_condition_score": None,
                "if_else_if_condition_avg_item_score": None,
                "if_else_if_condition_pessimistic_score": None,
                "if_else_if_condition_checked_items": 0,
                "if_else_if_condition_represented": 0,
                "if_else_if_condition_representability_rate": None,
                "if_else_if_condition_both_reached": 0,
                "if_else_if_condition_one_reached": 0,
                "if_else_if_condition_killed": 0,
                "if_else_if_condition_by_construct": {},
                "if_else_if_condition_scored_items": [],
            }
        condition_checked = extreme_condition_checked_profile(test_features.get("extreme_condition_mutation") or {})
        custom_extreme_available = condition_checked is not None
        mutation_profile = source_control_flow_mutation_profile(
            obligation_items,
            pit_items,
            custom_extreme_available=custom_extreme_available,
        )
        if condition_checked is None:
            condition_checked = if_else_if_checked_condition_profile(
                mutation_profile["classified_items"],
                test_features.get("branch_condition_outcome_evidence") or {},
                True,
            )
        reached_items = mutation_profile["reached_items"]
        missed_items = mutation_profile["unreached_items"] + mutation_profile["other_status_items"] + mutation_profile["unmutated_items"]
        return {
            "total": len(obligation_items),
            "covered": mutation_profile["reached"],
            "reached": mutation_profile["reached"],
            "unreached": mutation_profile["unreached"],
            "other_status": mutation_profile["other_status"],
            "unmutated": mutation_profile["unmutated"],
            "mutation_covered": mutation_profile["mutation_covered"],
            "mutation_coverage_rate": mutation_profile["mutation_coverage_rate"],
            "pit_covered_by_construct": mutation_profile.get("pit_covered_by_construct", {}),
            "pit_total_by_construct": mutation_profile.get("pit_total_by_construct", {}),
            "missed": len(obligation_items) - mutation_profile["reached"],
            "reached_items": reached_items[:50],
            "matched_items": reached_items[:50],
            "missed_items": missed_items[:50],
            "excluded_private_nested_items": private_nested_items[:50],
            "excluded_private_nested_count": len(private_nested_items),
            **condition_checked,
        }

    fallback = False
    asserted = test_features.get("asserted_observable_behaviors") or {}
    if category == "state_transition":
        fallback = bool(asserted.get("state_change"))
    elif category == "interaction_dependency":
        fallback = bool(asserted.get("side_effect_or_dependency"))
    if not obligation_items:
        return None
    return item_match_stats(category, obligation_items, pit_items, fallback_covered=fallback)


def category_profile(
    category: str,
    obligation: dict[str, Any],
    obligation_items: list[dict[str, Any]],
    test_features: dict[str, Any],
    test_code: str,
    pit: dict[str, Counter],
    pit_items: list[dict[str, Any]],
    execution_passed: bool,
) -> dict[str, Any]:
    total = int(obligation.get("total") or 0)
    evidence: list[str] = []
    missed = total
    item_profile = category_item_profile(category, obligation_items, test_features, pit_items)

    if item_profile:
        total = int(item_profile["total"])
        missed = int(item_profile["missed"])
        evidence = sorted({str(item.get("signal")) for item in item_profile.get("matched_items", [])})

    pit_counts = pit.get(category, Counter())
    pit_total = int(pit_counts.get("total", 0))
    pit_missed = int(pit_counts.get("SURVIVED", 0)) + int(pit_counts.get("NO_COVERAGE", 0))

    if total != 0 and not execution_passed:
        missed = total

    return {
        "total": total,
        "missed": missed,
        "evidence": evidence,
        "all_obligation_items": obligation_items,
        "obligation_items": obligation_items[:50],
        "obligation_item_count": len(obligation_items),
        "reached_obligation_items": (item_profile or {}).get("reached_items", []),
        "reached_obligation_item_count": int((item_profile or {}).get("reached", 0)),
        "matched_obligation_items": (item_profile or {}).get("matched_items", []),
        "missed_obligation_items": (item_profile or {}).get("missed_items", []),
        "unreached_obligation_item_count": int((item_profile or {}).get("unreached", 0)),
        "other_status_obligation_item_count": int((item_profile or {}).get("other_status", 0)),
        "unmutated_obligation_item_count": int((item_profile or {}).get("unmutated", 0)),
        "mutation_covered_obligation_item_count": int((item_profile or {}).get("mutation_covered", 0)),
        "mutation_coverage_rate": (item_profile or {}).get("mutation_coverage_rate"),
        "excluded_private_nested_count": int((item_profile or {}).get("excluded_private_nested_count", 0)),
        "excluded_private_nested_items": (item_profile or {}).get("excluded_private_nested_items", []),
        "if_else_if_condition_total": int((item_profile or {}).get("if_else_if_condition_total", 0)),
        "if_else_if_condition_scorable": int((item_profile or {}).get("if_else_if_condition_scorable", 0)),
        "if_else_if_condition_score_sum": float((item_profile or {}).get("if_else_if_condition_score_sum", 0) or 0),
        "if_else_if_condition_score": (item_profile or {}).get("if_else_if_condition_score"),
        "if_else_if_condition_avg_item_score": (item_profile or {}).get("if_else_if_condition_avg_item_score"),
        "if_else_if_condition_pessimistic_score": (item_profile or {}).get("if_else_if_condition_pessimistic_score"),
        "if_else_if_condition_checked_items": int((item_profile or {}).get("if_else_if_condition_checked_items", 0)),
        "if_else_if_condition_represented": int((item_profile or {}).get("if_else_if_condition_represented", 0)),
        "if_else_if_condition_representability_rate": (item_profile or {}).get("if_else_if_condition_representability_rate"),
        "if_else_if_condition_both_reached": int((item_profile or {}).get("if_else_if_condition_both_reached", 0)),
        "if_else_if_condition_one_reached": int((item_profile or {}).get("if_else_if_condition_one_reached", 0)),
        "if_else_if_condition_killed": int((item_profile or {}).get("if_else_if_condition_killed", 0)),
        "if_else_if_condition_by_construct": (item_profile or {}).get("if_else_if_condition_by_construct", {}),
        "if_else_if_condition_scored_items": (item_profile or {}).get("if_else_if_condition_scored_items", []),
        "if_else_if_condition_pit_covered_by_construct": (item_profile or {}).get("pit_covered_by_construct", {}),
        "if_else_if_condition_pit_total_by_construct": (item_profile or {}).get("pit_total_by_construct", {}),
        "pit_total": pit_total,
        "pit_missed": pit_missed,
    }


_PANTA_PLACEHOLDER_BYTES = 300
_PANTA_PLACEHOLDER_MARKER = "testPlaceHolder"


def is_panta_placeholder(row: dict[str, Any]) -> bool:
    """Return True when panta emitted its stub fallback test (assertTrue(true) only)."""
    test_file = row.get("test_file")
    if not isinstance(test_file, dict):
        return False
    if test_file.get("bytes", 9999) > _PANTA_PLACEHOLDER_BYTES:
        return False
    preview = test_file.get("preview") or ""
    return _PANTA_PLACEHOLDER_MARKER in preview


def generated_test_code(row: dict[str, Any]) -> str:
    code = row.get("extracted_test_code")
    if isinstance(code, str) and code:
        return code
    test_file = row.get("test_file")
    if isinstance(test_file, dict):
        preview = test_file.get("preview")
        if isinstance(preview, str):
            return preview
        path_value = test_file.get("path")
        if path_value:
            path = Path(str(path_value))
            try:
                return read_text_cached(str(path))
            except OSError:
                pass
    return ""


@functools.lru_cache(maxsize=4096)
def read_text_cached(path_text: str) -> str:
    return Path(path_text).read_text(encoding="utf-8", errors="replace")


def analyze_row(row: dict[str, Any]) -> dict[str, Any]:
    # Panta returns a stub test (assertTrue(true)) when generation fails entirely.
    # Treat those as generation failures, not as executable tests with no coverage.
    if row.get("execution_passed") and is_panta_placeholder(row):
        row = {**row, "execution_passed": False, "compile_passed": False}
    source_path = find_source(row)
    source = read_text_cached(str(source_path)) if source_path else ""
    test_code = generated_test_code(row)
    failure = failure_classification_for_row(row)
    task = row.get("task") or {}
    signature = task.get("signature") or row.get("signature")
    obligations = source_obligations(source, signature)
    obligation_items = source_obligation_items(source, signature)
    features = assertion_features(test_code)
    features["test_code"] = test_code
    features["extreme_condition_mutation"] = row.get("extreme_condition_mutation") or {}
    obligation_items["branch_condition"] = control_flow_obligation_items(source, signature)
    obligations["branch_condition"]["total"] = len(obligation_items["branch_condition"])
    boundary = classify_boundary_strength(test_code, source, signature, features)
    features.update(boundary)
    features["branch_condition_outcome_evidence"] = branch_condition_outcomes(
        test_code,
        signature,
        obligation_items["branch_condition"],
    )
    features["switch_case_outcome_evidence"] = switch_case_outcomes(
        test_code,
        signature,
        obligation_items["branch_condition"],
    )
    method_name = signature_method_name(signature)
    features["target_call_argument_variants"] = target_call_argument_variants(test_code, method_name)
    if row.get("assertion_mutation"):
        features["meaningful_return_assertion_count"] = 0
        features["weak_return_assertion_count"] = 0
    else:
        features["meaningful_return_assertion_count"] = count_meaningful_target_return_assertions(test_code, method_name)
        features["weak_return_assertion_count"] = count_weak_target_return_assertions(test_code, method_name)
    pit = parse_pit_mutants(row.get("workdir"), row.get("mutation_details"), source)
    pit_items = parse_pit_items(row.get("workdir"), row.get("mutation_details"), source)
    target_is_private = is_private_method_signature(signature)
    if row.get("assertion_mutation"):
        oracle = {
            "legacy_oracle_strength": "superseded_by_assertion_mutation",
            "legacy_oracle_strength_score": None,
            "oracle_strength": "superseded_by_assertion_mutation",
            "oracle_strength_score": None,
            "observable_obligations": {},
            "asserted_observable_behaviors": {},
            "applicable_observable_count": 0,
            "asserted_observable_count": 0,
            "oracle_obligation_items": [],
            "oracle_matched_items": [],
            "oracle_missed_items": [],
            "oracle_obligation_scores": [],
            "oracle_match_budgets": {},
            "oracle_cap_reasons": ["superseded_by_assertion_mutation"],
        }
    else:
        oracle = classify_oracle_strength(
            test_code=test_code,
            features=features,
            obligations=obligations,
            obligation_items=obligation_items,
            source=source,
            signature=signature,
            mutation_score=row.get("mutation_score"),
        )
    features.update(oracle)
    execution_passed = bool(row.get("execution_passed"))
    features.update(assertion_strength_profile(row.get("assertion_mutation") or {}, execution_passed, target_is_private))
    features["has_meaningful_target_assertion"] = has_meaningful_target_assertion(features)
    profile = {}
    for category in CATEGORIES:
        if category in {"boundary_value", "branch_condition"}:
            profile[category] = category_profile(
                category,
                obligations[category],
                obligation_items[category],
                features,
                test_code,
                pit,
                pit_items,
                execution_passed,
            )
        else:
            profile[category] = category_profile(category, {"total": 0}, [], features, test_code, {}, [], execution_passed)
    branch_strength = classify_branch_condition_strength(row, features, obligations, profile, execution_passed)
    state_strength = {
        "state_transition_strength": "not_applicable",
        "state_transition_strength_score": None,
        "state_transition_obligations": 0,
        "state_transition_covered_items": 0,
        "state_transition_asserted_items": 0,
        "state_transition_setup_items": 0,
        "state_transition_setup_score": None,
        "state_transition_sequence_order_score": None,
        "state_transition_setup_evidence_items": [],
        "state_transition_assertion_evidence_items": [],
        "state_transition_sequence_assertion": False,
        "state_transition_near_all_items": False,
    }
    interaction_strength = {
        "interaction_dependency_strength": "not_applicable",
        "interaction_dependency_strength_score": None,
        "interaction_dependency_obligations": 0,
        "interaction_dependency_covered_items": 0,
        "interaction_dependency_asserted_items": 0,
        "interaction_dependency_outcome_signals": [],
        "interaction_dependency_outcome_evidence": {},
        "interaction_dependency_applicable_cases": [],
        "interaction_dependency_achieved_cases": [],
        "interaction_dependency_applicable_kind_cases": [],
        "interaction_dependency_achieved_kind_cases": [],
        "interaction_dependency_case_scores": {},
        "interaction_dependency_checked_items": [],
        "interaction_dependency_unchecked_items": [],
        "interaction_dependency_setup_evidence_by_kind": {},
    }
    features.update(branch_strength)
    features.update(state_strength)
    features.update(interaction_strength)
    features.update(classify_side_effect_strength(features))
    return {
        "run_id": row.get("run_id"),
        "task_id": row.get("task_id"),
        "language": row.get("language"),
        "model_id": row.get("model_id"),
        "prompt_template": row.get("prompt_template"),
        "temperature": row.get("temperature"),
        "complexity_bucket": (
            row.get("complexity_bucket")
            or task.get("v2_primary_split")
            or task.get("dataset")
            or task.get("complexity_bucket")
            or "unknown"
        ),
        "cyclomatic_complexity": task.get("cyclomatic_complexity"),
        "cognitive_complexity": task.get("cognitive_complexity"),
        "dependency_category": task.get("dependency_category"),
        "target_is_private": target_is_private,
        "compile_passed": row.get("compile_passed"),
        "execution_passed": row.get("execution_passed"),
        "error_stage": row.get("error_stage"),
        "failure_family": failure["failure_family"],
        "failure_category": failure["failure_category"],
        "failure_matched_categories": failure["failure_matched_categories"],
        "failure_log_path": failure["failure_log_path"],
        "failure_evidence": failure["failure_evidence"],
        "coverage_line_rate": (row.get("coverage") or {}).get("line_rate"),
        "coverage_branch_rate": (row.get("coverage") or {}).get("branch_rate"),
        "mutation_score": row.get("mutation_score"),
        "extreme_condition_mutation_status": (row.get("extreme_condition_mutation") or {}).get("status"),
        "assertion_mutation_status": features["assertion_mutation_status"],
        "assertion_mutation_raw_status": features["assertion_mutation_raw_status"],
        "assertion_strength_score": features["assertion_strength_score"],
        "assertion_strength_pessimistic_score": features["assertion_strength_pessimistic_score"],
        "assertion_strength_total_items": features["assertion_strength_total_items"],
        "assertion_strength_reached_items": features["assertion_strength_reached_items"],
        "assertion_strength_scorable_items": features["assertion_strength_scorable_items"],
        "assertion_strength_non_representable_items": features["assertion_strength_non_representable_items"],
        "assertion_strength_checked_items": features["assertion_strength_checked_items"],
        "assertion_strength_oracle_killed_items": features["assertion_strength_oracle_killed_items"],
        "assertion_strength_incidentally_killed_items": features["assertion_strength_incidentally_killed_items"],
        "assertion_strength_survived_items": features["assertion_strength_survived_items"],
        "assertion_strength_unmeasurable_by_type_substitution_items": features["assertion_strength_unmeasurable_by_type_substitution_items"],
        "assertion_strength_represented_items": features["assertion_strength_represented_items"],
        "assertion_strength_representability_rate": features["assertion_strength_representability_rate"],
        "assertion_strength_scorable_rate": features["assertion_strength_scorable_rate"],
        "assertion_strength_status_counts": features["assertion_strength_status_counts"],
        "assertion_strength_by_construct": features["assertion_strength_by_construct"],
        "assertion_strength_return_subcategories": features["assertion_strength_return_subcategories"],
        "assertion_strength_return_object_rules": features["assertion_strength_return_object_rules"],
        "exception_behavior_excluding_broad_original_scope": features["exception_behavior_excluding_broad_original_scope"],
        "side_effect_research_scope": features["side_effect_research_scope"],
        "side_effect_research_by_construct": features["side_effect_research_by_construct"],
        "assertion_strength_items": features["assertion_strength_items"],
        "assertion_count": features["assertion_count"],
        "weak_assertion_count": features["weak_assertion_count"],
        "semantic_assertion_count": features["semantic_assertion_count"],
        "correct_exception_assertion_count": features["correct_exception_assertion_count"],
        "swallowed_manual_failure_count": features["swallowed_manual_failure_count"],
        "oracle_strength_score": features["oracle_strength_score"],
        "applicable_observable_count": features["applicable_observable_count"],
        "asserted_observable_count": features["asserted_observable_count"],
        "observable_obligations": features["observable_obligations"],
        "asserted_observable_behaviors": features["asserted_observable_behaviors"],
        "oracle_obligation_items": features["oracle_obligation_items"],
        "oracle_matched_items": features["oracle_matched_items"],
        "oracle_missed_items": features["oracle_missed_items"],
        "oracle_obligation_scores": features["oracle_obligation_scores"],
        "oracle_match_budgets": features["oracle_match_budgets"],
        "oracle_cap_reasons": features["oracle_cap_reasons"],
        "boundary_strength_score": features["boundary_strength_score"],
        "boundary_applicable_categories": features["boundary_applicable_categories"],
        "boundary_covered_categories": features["boundary_covered_categories"],
        "boundary_applicable_items": features["boundary_applicable_items"],
        "boundary_matched_items": features["boundary_matched_items"],
        "boundary_missed_items": features["boundary_missed_items"],
        "boundary_parameter_scores": features["boundary_parameter_scores"],
        "boundary_parameter_evidence": features["boundary_parameter_evidence"],
        "boundary_global_mentioned_categories": features["boundary_global_mentioned_categories"],
        "boundary_source_constraints": features["boundary_source_constraints"],
        "boundary_applicable_count": features["boundary_applicable_count"],
        "boundary_covered_count": features["boundary_covered_count"],
        "boundary_asserted_count": features["boundary_asserted_count"],
        "boundary_target_method": features["boundary_target_method"],
        "boundary_target_call_count": features["boundary_target_call_count"],
        "boundary_total_argument_count": features["boundary_total_argument_count"],
        "boundary_unresolvable_argument_count": features["boundary_unresolvable_argument_count"],
        "boundary_unresolvable_argument_rate": features["boundary_unresolvable_argument_rate"],
        "boundary_unresolvable_argument_examples": features["boundary_unresolvable_argument_examples"],
        "target_call_argument_variants": features["target_call_argument_variants"],
        "meaningful_return_assertion_count": features["meaningful_return_assertion_count"],
        "weak_return_assertion_count": features["weak_return_assertion_count"],
        "branch_condition_strength_score": features["branch_condition_strength_score"],
        "branch_condition_obligations": features["branch_condition_obligations"],
        "branch_condition_covered_items": features["branch_condition_covered_items"],
        "branch_condition_reached_items": features["branch_condition_reached_items"],
        "branch_condition_unreached_items": features["branch_condition_unreached_items"],
        "branch_condition_other_status_items": features["branch_condition_other_status_items"],
        "branch_condition_unmutated_items": features["branch_condition_unmutated_items"],
        "branch_condition_mutation_covered_items": features["branch_condition_mutation_covered_items"],
        "branch_condition_mutation_coverage_rate": features["branch_condition_mutation_coverage_rate"],
        "if_else_if_condition_total": features["if_else_if_condition_total"],
        "if_else_if_condition_scorable": features["if_else_if_condition_scorable"],
        "if_else_if_condition_checked_sum": features["if_else_if_condition_checked_sum"],
        "if_else_if_condition_checked_score": features["if_else_if_condition_checked_score"],
        "if_else_if_condition_avg_item_score": features["if_else_if_condition_avg_item_score"],
        "if_else_if_condition_pessimistic_score": features["if_else_if_condition_pessimistic_score"],
        "if_else_if_condition_checked_items": features["if_else_if_condition_checked_items"],
        "if_else_if_condition_represented": features["if_else_if_condition_represented"],
        "if_else_if_condition_representability_rate": features["if_else_if_condition_representability_rate"],
        "if_else_if_condition_both_reached": features["if_else_if_condition_both_reached"],
        "if_else_if_condition_one_reached": features["if_else_if_condition_one_reached"],
        "if_else_if_condition_killed": features["if_else_if_condition_killed"],
        "if_else_if_condition_by_construct": features["if_else_if_condition_by_construct"],
        "if_else_if_condition_pit_covered_by_construct": features["if_else_if_condition_pit_covered_by_construct"],
        "if_else_if_condition_pit_total_by_construct": features["if_else_if_condition_pit_total_by_construct"],
        "if_else_if_condition_scored_items": features["if_else_if_condition_scored_items"],
        "branch_condition_asserted_items": features["branch_condition_asserted_items"],
        "branch_condition_reach_score": features["branch_condition_reach_score"],
        "branch_condition_true_outcomes": features["branch_condition_true_outcomes"],
        "branch_condition_false_outcomes": features["branch_condition_false_outcomes"],
        "branch_condition_both_outcomes": features["branch_condition_both_outcomes"],
        "branch_condition_outcome_evidence_count": features["branch_condition_outcome_evidence_count"],
        "branch_condition_outcome_evidence": features["branch_condition_outcome_evidence"],
        "switch_case_outcome_evidence": features["switch_case_outcome_evidence"],
        "has_meaningful_target_assertion": features["has_meaningful_target_assertion"],
        "state_transition_strength_score": features["state_transition_strength_score"],
        "state_transition_obligations": features["state_transition_obligations"],
        "state_transition_covered_items": features["state_transition_covered_items"],
        "state_transition_asserted_items": features["state_transition_asserted_items"],
        "state_transition_setup_items": features["state_transition_setup_items"],
        "state_transition_setup_score": features["state_transition_setup_score"],
        "state_transition_sequence_order_score": features["state_transition_sequence_order_score"],
        "state_transition_setup_evidence_items": features["state_transition_setup_evidence_items"],
        "state_transition_assertion_evidence_items": features["state_transition_assertion_evidence_items"],
        "state_transition_sequence_assertion": features["state_transition_sequence_assertion"],
        "state_transition_near_all_items": features["state_transition_near_all_items"],
        "interaction_dependency_strength_score": features["interaction_dependency_strength_score"],
        "interaction_dependency_obligations": features["interaction_dependency_obligations"],
        "interaction_dependency_covered_items": features["interaction_dependency_covered_items"],
        "interaction_dependency_asserted_items": features["interaction_dependency_asserted_items"],
        "interaction_dependency_outcome_signals": features["interaction_dependency_outcome_signals"],
        "interaction_dependency_outcome_evidence": features["interaction_dependency_outcome_evidence"],
        "interaction_dependency_applicable_cases": features["interaction_dependency_applicable_cases"],
        "interaction_dependency_achieved_cases": features["interaction_dependency_achieved_cases"],
        "interaction_dependency_applicable_kind_cases": features["interaction_dependency_applicable_kind_cases"],
        "interaction_dependency_achieved_kind_cases": features["interaction_dependency_achieved_kind_cases"],
        "interaction_dependency_case_scores": features["interaction_dependency_case_scores"],
        "interaction_dependency_checked_items": features["interaction_dependency_checked_items"],
        "interaction_dependency_unchecked_items": features["interaction_dependency_unchecked_items"],
        "interaction_dependency_setup_evidence_by_kind": features["interaction_dependency_setup_evidence_by_kind"],
        "side_effect_strength_score": features["side_effect_strength_score"],
        "side_effect_obligations": features["side_effect_obligations"],
        "side_effect_checked_items": features["side_effect_checked_items"],
        "side_effect_state_obligations": features["side_effect_state_obligations"],
        "side_effect_state_checked_items": features["side_effect_state_checked_items"],
        "side_effect_external_obligations": features["side_effect_external_obligations"],
        "side_effect_external_checked_items": features["side_effect_external_checked_items"],
        "target_calls": features["target_calls"],
        "syntax_backend": features["syntax_backend"],
        "method_declaration_count": features["method_declaration_count"],
        "method_invocation_count": features["method_invocation_count"],
        "object_creation_count": features["object_creation_count"],
        "catch_clause_count": features["catch_clause_count"],
        "throw_statement_count": features["throw_statement_count"],
        "if_statement_count": features["if_statement_count"],
        "loop_statement_count": features["loop_statement_count"],
        "literal_count": features["literal_count"],
        "uses_reflection": features["uses_reflection"],
        "uses_subclass_or_override": features["uses_subclass_or_override"],
        "uses_manual_assertion_error": features["uses_manual_assertion_error"],
        "source_path": str(source_path) if source_path else "",
        "workdir": row.get("workdir"),
        "source_obligation_items": obligation_items,
        "pit_obligation_evidence": pit_items,
        "profile": profile,
    }


def paper_profile(value: Any) -> Any:
    """Build a report-only view while retaining the full analysis profile."""
    excluded = {"side_effect_or_dependency", "interaction_dependency", "external_side_effect"}
    if isinstance(value, dict):
        if value.get("construct") in excluded or value.get("category") in excluded:
            return None
        return {
            key: paper_profile(item)
            for key, item in value.items()
            if key not in excluded and not (isinstance(key, str) and key.startswith(("side_effect_", "interaction_dependency_")))
        }
    if isinstance(value, list):
        items = [paper_profile(item) for item in value if not isinstance(item, str) or item not in excluded]
        return [item for item in items if item is not None]
    return value


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    flat = {k: v for k, v in row.items() if k != "profile"}
    if "profile" not in row:
        return flat
    for category in CATEGORIES:
        stats = row["profile"][category]
        flat[f"{category}_total"] = stats["total"]
        flat[f"{category}_missed"] = stats["missed"]
        flat[f"{category}_pit_total"] = stats["pit_total"]
        flat[f"{category}_pit_missed"] = stats["pit_missed"]
        flat[f"{category}_evidence"] = ",".join(stats["evidence"])
    return flat


def avg(values: list[Any]) -> float | None:
    clean = [float(value) for value in values if value not in (None, "")]
    return sum(clean) / len(clean) if clean else None


def fmt(value: Any) -> str:
    if value in (None, ""):
        return "N/A"
    return f"{float(value):.3f}"


def fmt_min_n(value: Any, n: int, minimum: int = MIN_SUBCATEGORY_N) -> str:
    return "suppressed" if n < minimum else fmt(value)


def pct(numer: int, denom: int) -> str:
    if denom == 0:
        return ""
    return f"{numer / denom:.3f}"


def boundary_category_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for category in BOUNDARY_CATEGORIES:
        applicable_items = [
            item
            for row in rows
            for item in row.get("boundary_applicable_items") or []
            if item.get("category") == category
        ]
        covered_items = [
            item
            for row in rows
            for item in row.get("boundary_matched_items") or []
            if item.get("category") == category
        ]
        applicable_rows = [row for row in rows if any((item.get("category") == category) for item in row.get("boundary_applicable_items") or [])]
        covered_rows = [row for row in rows if any((item.get("category") == category) for item in row.get("boundary_matched_items") or [])]
        by_prompt: dict[str, tuple[int, int]] = {}
        for prompt, prompt_rows in group_rows_by(rows, "prompt_template").items():
            prompt_applicable = [
                item
                for row in prompt_rows
                for item in row.get("boundary_applicable_items") or []
                if item.get("category") == category
            ]
            prompt_covered = [
                item
                for row in prompt_rows
                for item in row.get("boundary_matched_items") or []
                if item.get("category") == category
            ]
            by_prompt[prompt] = (len(prompt_covered), len(prompt_applicable))
        summary.append(
            {
                "category": category,
                "applicable_rows": len(applicable_rows),
                "covered_rows": len(covered_rows),
                "applicable_items": len(applicable_items),
                "covered_items": len(covered_items),
                "coverage_rate": len(covered_items) / len(applicable_items) if applicable_items else None,
                "by_prompt": by_prompt,
            }
        )
    return summary


def group_rows_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key))].append(row)
    return groups


def executable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if bool(row.get("execution_passed"))]


def execution_summary_lines(rows: list[dict[str, Any]]) -> list[str]:
    executable = executable_rows(rows)
    compiled = sum(1 for row in rows if row.get("compile_passed"))
    return [
        "",
        "## Execution Funnel",
        "",
        "| Stage | Rows |",
        "| --- | ---: |",
        f"| Total | {len(rows)} |",
        f"| Compilation passed | {compiled} |",
        f"| Execution passed | {len(executable)} |",
        f"| Non-executable | {len(rows) - len(executable)} |",
    ]


def failure_classification_lines(rows: list[dict[str, Any]]) -> list[str]:
    failures = [row for row in rows if not row.get("execution_passed")]
    lines = [
        "",
        "## Failure Classification",
        "",
        f"- classified failure rows: {len(failures)}",
    ]
    if not failures:
        return lines

    family_counts = Counter(str(row.get("failure_family") or "unknown") for row in failures)
    category_counts = Counter(
        (
            str(row.get("failure_family") or "unknown"),
            str(row.get("failure_category") or "unknown"),
        )
        for row in failures
    )
    stage_counts = Counter(str(row.get("error_stage") or "unknown") for row in failures)
    lines.extend([
        "",
        "### Failure Families",
        "",
        "| Failure Family | Rows |",
        "| --- | ---: |",
    ])
    for family, count in sorted(family_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {family} | {count} |")

    lines.extend([
        "",
        "### Failure Categories",
        "",
        "| Failure Family | Failure Category | Rows |",
        "| --- | --- | ---: |",
    ])
    for (family, category), count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {family} | {category} | {count} |")

    lines.extend([
        "",
        "### Failure Stages",
        "",
        "| Error Stage | Rows |",
        "| --- | ---: |",
    ])
    for stage, count in sorted(stage_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {stage} | {count} |")

    categories = [
        f"{family}/{category}"
        for (family, category), _ in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    lines.extend([
        "",
        "### Failure Categories By Complexity Bucket",
        "",
        "| Complexity Bucket | Total | " + " | ".join(categories) + " |",
        "| --- | ---: | " + " | ".join("---:" for _ in categories) + " |",
    ])
    for bucket, bucket_rows in sorted(group_rows_by(failures, "complexity_bucket").items()):
        counts = Counter(
            f"{row.get('failure_family') or 'unknown'}/{row.get('failure_category') or 'unknown'}"
            for row in bucket_rows
        )
        lines.append(f"| {bucket} | {len(bucket_rows)} | {' | '.join(str(counts[category]) for category in categories)} |")

    return lines


STRENGTH_COLUMNS = ["very_weak", "weak", "medium", "strong", "very_strong"]
STRENGTH_ORDER = {"not_applicable": -1, "very_weak": 0, "weak": 1, "medium": 2, "strong": 3, "very_strong": 4}


STRENGTH_METRICS = [
    ("exception_strength", "Exception-Path Strength"),
    ("boundary_strength", "Boundary-Value Strength"),
    ("branch_condition_strength", "Branch-Condition Strength"),
    ("interaction_dependency_strength", "External Resource Setup Strength"),
]

RATIO_METRICS = [
    (
        "boundary_strength_score",
        "boundary_covered_count",
        "boundary_applicable_count",
        "Boundary-Value Adequacy",
    ),
    (
        "if_else_if_condition_avg_item_score",
        "if_else_if_condition_checked_sum",
        "if_else_if_condition_scorable",
        "Control-Flow Adequacy",
    ),
    (
        "assertion_strength_score",
        "assertion_strength_checked_items",
        "assertion_strength_scorable_items",
        "Assertion Strength",
    ),
]


def branch_kind(snippet: str) -> str:
    if re.search(r"\bthrow\s+new\b|\bthrows\s+\w+", snippet):
        return "exception_path"
    if re.search(r"\btry\s*\{", snippet):
        return "try"
    if re.search(r"\bcatch\s*\(", snippet):
        return "catch"
    if re.search(r"\bcase\s+", snippet):
        return "case"
    if re.search(r"\bdefault\s*:", snippet):
        return "default"
    if re.search(r"\belse\s+if\s*\(", snippet):
        return "else_if"
    if re.search(r"\belse\b", snippet):
        return "else"
    if re.search(r"\bif\s*\(", snippet):
        return "if"
    if re.search(r"\bfor\s*\(|\bwhile\s*\(", snippet):
        if re.search(r"\bfor\s*\([^;:]+:\s*[^)]+\)", snippet):
            return "enhanced_for_iteration"
        return "loop_condition"
    if "?" in snippet:
        return "ternary"
    return "other"


def state_kind(snippet: str) -> str:
    if re.search(r"\bset[A-Z]\w*\s*\(", snippet) or re.search(r"\.set\s*\(", snippet):
        return "setter_or_set_call"
    if re.search(r"\.(?:add|put|remove|clear)\s*\(", snippet):
        return "collection_mutation"
    if re.search(r"\+\+|--|\+=|-=", snippet):
        return "increment_or_compound_update"
    if re.search(r"\b(?:this\.|[A-Z][A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.)?\w+\s*=", snippet):
        return "field_assignment"
    return "other_state_write"


def dependency_kind(snippet: str) -> str:
    if re.search(r"\b(?:Files|FileInputStream|FileOutputStream|InputStreamReader|OutputStreamWriter|FileReader|FileWriter|BufferedReader|BufferedWriter|InputStream|OutputStream|Reader|Writer)\b", snippet):
        return "filesystem_or_stream"
    if re.search(r"\b(?:URL|URI|Socket|HttpClient|RestTemplate|WebClient|URLConnection|HttpURLConnection)\b|\.(?:connect|getInputStream|getOutputStream|getResponseCode)\s*\(|\b\w+\.(?:send|execute)\s*\(", snippet):
        return "network_or_http"
    return "other_dependency"


def oracle_obligation_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for row in executable_rows(rows):
        for item in row.get("oracle_obligation_scores") or []:
            entries.append({"row": row, "item": item})
    return entries


def oracle_prompt_strength_tables(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    groups = group_rows_by(score_rows, "prompt_template")
    lines = [
        "",
        "### Oracle Obligation Scores By Prompt",
        "",
        "| Prompt | Applicable FUT Rows | Applicable Obligations | O0 No Visible Oracle | O1 Weak Visible Oracle | O2 Direct Obligation Oracle | Avg FUT Score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for prompt, prompt_rows in sorted(groups.items()):
        entries = oracle_obligation_entries(prompt_rows)
        counts = Counter(int(entry["item"].get("score") or 0) for entry in entries)
        applicable_rows = [row for row in prompt_rows if row.get("oracle_strength") != "not_applicable"]
        lines.append(
            f"| {prompt} | {len(applicable_rows)} | {len(entries)} | "
            f"{counts[0]} | {counts[1]} | {counts[2]} | "
            f"{fmt(avg([row['oracle_strength_score'] for row in applicable_rows]))} |"
        )

    lines.extend([
        "",
        "### Oracle Obligation Scores By Prompt (%)",
        "",
        "| Prompt | O0 No Visible Oracle | O1 Weak Visible Oracle | O2 Direct Obligation Oracle |",
        "| --- | ---: | ---: | ---: |",
    ])
    for prompt, prompt_rows in sorted(groups.items()):
        entries = oracle_obligation_entries(prompt_rows)
        counts = Counter(int(entry["item"].get("score") or 0) for entry in entries)
        denom = len(entries)
        cells = [f"{(counts[column] / denom * 100):.1f}%" if denom else "N/A" for column in range(3)]
        lines.append(f"| {prompt} | {' | '.join(cells)} |")
    return lines


def oracle_signal_strength_tables(rows: list[dict[str, Any]]) -> list[str]:
    entries = oracle_obligation_entries(rows)
    signal_order = [
        "return_behavior",
        "exception_behavior",
        "side_effect_or_dependency",
    ]
    signal_names = {
        "return_behavior": "Return Behavior",
        "exception_behavior": "Exception Behavior",
        "side_effect_or_dependency": "External Interaction Oracle",
    }
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        signal = str(entry["item"].get("signal") or "unknown")
        if signal == "state_change":
            continue
        by_signal[signal].append(entry)

    lines = [
        "",
        "### Oracle Obligation Scores By Signal",
        "",
        "| Signal | Obligation Items | O0 No Visible Oracle | O1 Weak Visible Oracle | O2 Direct Obligation Oracle | Avg Obligation Score | Avg Row Mutation |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    ordered_signals = [signal for signal in signal_order if signal in by_signal]
    ordered_signals.extend(sorted(signal for signal in by_signal if signal not in signal_order))
    for signal in ordered_signals:
        group_entries = by_signal[signal]
        counts = Counter(int(entry["item"].get("score") or 0) for entry in group_entries)
        lines.append(
            f"| {signal_names.get(signal, signal)} | {len(group_entries)} | "
            f"{counts[0]} | {counts[1]} | {counts[2]} | "
            f"{fmt(avg([entry['item'].get('score') for entry in group_entries]))} | "
            f"{fmt(avg([entry['row'].get('mutation_score') for entry in group_entries]))} |"
        )

    lines.extend([
        "",
        "### Oracle Obligation Scores By Signal And Prompt",
        "",
        "| Prompt | Signal | Obligation Items | Avg Obligation Score | O0 No Visible Oracle | O1 Weak Visible Oracle | O2 Direct Obligation Oracle |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    prompt_groups = group_rows_by(executable_rows(rows), "prompt_template")
    for prompt, prompt_rows in sorted(prompt_groups.items()):
        prompt_entries = oracle_obligation_entries(prompt_rows)
        prompt_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in prompt_entries:
            signal = str(entry["item"].get("signal") or "unknown")
            if signal == "state_change":
                continue
            prompt_by_signal[signal].append(entry)
        prompt_signals = [signal for signal in signal_order if signal in prompt_by_signal]
        prompt_signals.extend(sorted(signal for signal in prompt_by_signal if signal not in signal_order))
        for signal in prompt_signals:
            group_entries = prompt_by_signal[signal]
            counts = Counter(int(entry["item"].get("score") or 0) for entry in group_entries)
            lines.append(
                f"| {prompt} | {signal_names.get(signal, signal)} | {len(group_entries)} | "
                f"{fmt(avg([entry['item'].get('score') for entry in group_entries]))} | "
                f"{counts[0]} | {counts[1]} | {counts[2]} |"
            )
    return lines


def oracle_fut_score_distribution(rows: list[dict[str, Any]], group_key: str, title: str) -> list[str]:
    score_rows = [row for row in executable_rows(rows) if row.get("oracle_strength_score") is not None]
    groups = group_rows_by(score_rows, group_key)
    bins = [
        ("s = 0", lambda value: value == 0),
        ("0 < s < 1", lambda value: 0 < value < 1),
        ("1 <= s < 2", lambda value: 1 <= value < 2),
        ("s = 2", lambda value: value == 2),
    ]
    lines = [
        "",
        f"### Oracle FUT Score Distribution {title}",
        "",
        "| Group | Applicable FUT Rows | Avg FUT Score | " + " | ".join(label for label, _ in bins) + " |",
        "| --- | ---: | ---: | " + " | ".join("---:" for _ in bins) + " |",
    ]
    for group, group_rows in sorted(groups.items()):
        scores = [float(row["oracle_strength_score"]) for row in group_rows]
        bin_counts = [sum(1 for score in scores if predicate(score)) for _, predicate in bins]
        lines.append(
            f"| {group} | {len(group_rows)} | {fmt(avg(scores))} | "
            f"{' | '.join(str(count) for count in bin_counts)} |"
        )
    return lines


def oracle_strength_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    entries = oracle_obligation_entries(rows)
    no_contract_rows = [row for row in score_rows if row.get("oracle_strength") == "not_applicable"]
    lines = [
        "",
        "## Oracle Strength",
        "",
        "Oracle strength is counted per observable obligation. Each FUT also receives `oracle_strength_score`, the average score across its obligations.",
        "Oracle obligation scores use a three-level scale: O0 no visible oracle, O1 weak visible oracle, and O2 direct obligation oracle.",
        "",
        f"- executable FUT rows with no observable oracle contract: {len(no_contract_rows)}",
        "",
        "| Obligation Score | Obligation Items | Avg Obligation Score | Avg Row Mutation |",
        "| --- | ---: | ---: | ---: |",
    ]
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        groups[int(entry["item"].get("score") or 0)].append(entry)
    for score, group_entries in sorted(groups.items()):
        lines.append(
            f"| {score} | {len(group_entries)} | "
            f"{fmt(avg([entry['item'].get('score') for entry in group_entries]))} | "
            f"{fmt(avg([entry['row'].get('mutation_score') for entry in group_entries]))} |"
        )
    lines.extend(oracle_prompt_strength_tables(rows))
    lines.extend(oracle_signal_strength_tables(rows))
    lines.extend(oracle_fut_score_distribution(rows, "prompt_template", "By Prompt"))
    lines.extend(oracle_fut_score_distribution(rows, "complexity_bucket", "By Complexity Bucket"))
    return lines


def prompt_strength_tables(rows: list[dict[str, Any]], field: str, title: str) -> list[str]:
    score_rows = executable_rows(rows)
    lines = [
        "",
        f"### {title} By Prompt",
        "",
        "| Prompt | Applicable Rows | Very Weak | Weak | Medium | Strong | Very Strong |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    groups = group_rows_by(score_rows, "prompt_template")
    for prompt, prompt_rows in sorted(groups.items()):
        applicable = [row for row in prompt_rows if row.get(field) != "not_applicable"]
        counts = Counter(str(row.get(field)) for row in applicable)
        lines.append(
            f"| {prompt} | {len(applicable)} | "
            f"{counts['very_weak']} | {counts['weak']} | {counts['medium']} | "
            f"{counts['strong']} | {counts['very_strong']} |"
        )

    lines.extend([
        "",
        f"### {title} By Prompt (%)",
        "",
        "| Prompt | Very Weak | Weak | Medium | Strong | Very Strong |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for prompt, prompt_rows in sorted(groups.items()):
        applicable = [row for row in prompt_rows if row.get(field) != "not_applicable"]
        counts = Counter(str(row.get(field)) for row in applicable)
        denom = len(applicable)
        cells = [f"{(counts[column] / denom * 100):.1f}%" if denom else "" for column in STRENGTH_COLUMNS]
        lines.append(f"| {prompt} | {' | '.join(cells)} |")
    return lines


def overall_strength_table(rows: list[dict[str, Any]], field: str, score_field: str, title: str) -> list[str]:
    score_rows = executable_rows(rows)
    lines = [
        "",
        f"## {title}",
        "",
        "| Strength | Executable Rows | Avg Score | Avg Mutation |",
        "| --- | ---: | ---: | ---: |",
    ]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        groups[str(row.get(field))].append(row)
    for strength, group_rows in sorted(groups.items(), key=lambda item: STRENGTH_ORDER.get(item[0], 99)):
        lines.append(
            f"| {strength} | {len(group_rows)} | "
            f"{fmt(avg([r[score_field] for r in group_rows]))} | "
            f"{fmt(avg([r['mutation_score'] for r in group_rows]))} |"
        )
    lines.extend(prompt_strength_tables(rows, field, title))
    return lines


def ratio_metric_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    lines = [
        "",
        "## Ratio Adequacy Scores",
        "",
        "| Metric | Applicable Rows | Numerator | Denominator | Micro Score | Avg Row Score | Avg Row Mutation |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for score_field, numer_field, denom_field, title in RATIO_METRICS:
        applicable = [row for row in score_rows if row.get(score_field) is not None]
        numer = sum(float(row.get(numer_field) or 0) for row in applicable)
        denom = sum(int(row.get(denom_field) or 0) for row in applicable)
        micro = numer / denom if denom else None
        lines.append(
            f"| {title} | {len(applicable)} | {fmt(numer)} | {denom} | "
            f"{fmt(micro)} | {fmt(avg([row.get(score_field) for row in applicable]))} | "
            f"{fmt(avg([row.get('mutation_score') for row in applicable]))} |"
        )
    return lines


def boundary_unresolvable_argument_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    prompts = sorted(group_rows_by(score_rows, "prompt_template"))
    lines = [
        "",
        "### Boundary Unresolvable Argument Rate",
        "",
        "| Configuration | Target Arguments | Unresolvable Arguments | Rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for prompt in ["all"] + prompts:
        group = score_rows if prompt == "all" else [row for row in score_rows if row.get("prompt_template") == prompt]
        total = sum(int(row.get("boundary_total_argument_count") or 0) for row in group)
        unresolved = sum(int(row.get("boundary_unresolvable_argument_count") or 0) for row in group)
        lines.append(f"| {prompt} | {total} | {unresolved} | {fmt(unresolved / total if total else None)} |")
    return lines


def joint_reached_checked_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    prompts = sorted(group_rows_by(score_rows, "prompt_template"))
    lines = [
        "",
        "## Joint Reached-And-Checked Adequacy",
        "",
        "| Configuration | Control-Flow Checked/Applicable | Control-Flow Joint | Assertion Checked/Applicable | Assertion Joint | Boundary Covered/Applicable | Boundary Joint |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for prompt in ["all"] + prompts:
        group = score_rows if prompt == "all" else [row for row in score_rows if row.get("prompt_template") == prompt]
        cf_checked = sum(float(row.get("if_else_if_condition_checked_sum") or 0) for row in group)
        cf_total = sum(int(row.get("if_else_if_condition_total") or 0) for row in group)
        assertion_checked = sum(int(row.get("assertion_strength_checked_items") or 0) for row in group)
        assertion_total = sum(int(row.get("assertion_strength_total_items") or 0) for row in group)
        boundary_checked = sum(int(row.get("boundary_covered_count") or 0) for row in group)
        boundary_total = sum(int(row.get("boundary_applicable_count") or 0) for row in group)
        lines.append(
            f"| {prompt} | {fmt(cf_checked)}/{cf_total} | {fmt(cf_checked / cf_total if cf_total else None)} | "
            f"{assertion_checked}/{assertion_total} | {fmt(assertion_checked / assertion_total if assertion_total else None)} | "
            f"{boundary_checked}/{boundary_total} | {fmt(boundary_checked / boundary_total if boundary_total else None)} |"
        )
    return lines


def assertion_strength_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    collected = [row for row in score_rows if row.get("assertion_mutation_status") == "passed"]
    lines = [
        "",
        "## Oracle Strength Details",
        "",
    ]


    # Compute broad_totals first; used to override exception_behavior in the construct table.
    # The exception_behavior_excluding_broad_original_scope now excludes unmeasurable items
    # from the denominator (same as the primary scope after the assertion_scope_summary change).
    broad_totals: Counter = Counter()
    broad_score_sum = 0.0
    for row in collected:
        stats = row.get("exception_behavior_excluding_broad_original_scope") or {}
        if not isinstance(stats, dict):
            continue
        broad_totals["total"] += int(stats.get("total_items") or 0)
        broad_totals["reached"] += int(stats.get("reached_items") or 0)
        broad_totals["scorable"] += int(stats.get("scorable_items") or 0)
        broad_totals["checked"] += int(stats.get("checked_items") or 0)
        broad_totals["represented"] += int(stats.get("represented_items") or 0)
        broad_totals["oracle_killed"] += int(stats.get("oracle_killed_items") or 0)
        broad_totals["incidental"] += int(stats.get("incidentally_killed_items") or 0)
        broad_totals["survived"] += int(stats.get("survived_items") or 0)
        broad_totals["path_changed"] += int(stats.get("survived_path_changed_items") or 0)
        broad_totals["coverage_unverified"] += int(stats.get("coverage_unverified_items") or 0)
        broad_score_sum += float(stats.get("score_sum") or 0)
    unmeasurable_total = sum(
        int(row.get("assertion_strength_unmeasurable_by_type_substitution_items") or 0) for row in collected
    )

    construct_totals: dict[str, Counter[str]] = defaultdict(Counter)
    construct_score_sum: Counter[str] = Counter()
    for row in collected:
        for construct, stats in (row.get("assertion_strength_by_construct") or {}).items():
            if not isinstance(stats, dict):
                continue
            for key in [
                "total",
                "not_reached_items",
                "scorable",
                "checked",
                "killed_by_oracle_items",
                "killed_incidentally_items",
                "survived_items",
                "survived_path_changed_items",
                "coverage_unverified_items",
                "subsumed_by_broad_original_items",
                "represented",
            ]:
                construct_totals[str(construct)][key] += int(stats.get(key) or 0)
            construct_score_sum[str(construct)] += float(stats.get("score_sum") or 0)

    # Override exception_behavior stats with unmeasurable-excluded numbers only when
    # there are broad-original items to correct for. When unmeasurable_total==0 the
    # aggregate by_construct stats are already correct (no mixed data sources).
    if "exception_behavior" in construct_totals and unmeasurable_total > 0:
        construct_totals["exception_behavior"]["scorable"] = int(broad_totals["scorable"])
        construct_totals["exception_behavior"]["checked"] = int(broad_totals["checked"])
        construct_totals["exception_behavior"]["killed_by_oracle_items"] = int(broad_totals["oracle_killed"])
        construct_totals["exception_behavior"]["killed_incidentally_items"] = int(broad_totals["incidental"])
        construct_totals["exception_behavior"]["survived_items"] = int(broad_totals["survived"])
        construct_totals["exception_behavior"]["survived_path_changed_items"] = int(broad_totals["path_changed"])
        construct_totals["exception_behavior"]["coverage_unverified_items"] = int(broad_totals["coverage_unverified"])
        construct_totals["exception_behavior"]["represented"] = int(broad_totals["represented"])
        construct_score_sum["exception_behavior"] = broad_score_sum

    lines.extend([
        "| Construct | Total | Reached | Scorable | Scorable / Reached | Checked | Oracle-Killed | Incidental | Unchanged-Path Survived | Path Changed | Coverage Failed | Score | Representability |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for construct, stats in sorted(construct_totals.items()):
        total = int(stats.get("total") or 0)
        reached = total - int(stats.get("not_reached_items") or 0)
        scorable = int(stats.get("scorable") or 0)
        represented = int(stats.get("represented") or 0)
        score = construct_score_sum[construct] / scorable if scorable else None
        representability = represented / reached if reached else None
        lines.append(
            f"| {construct} | {total} | {reached} | {scorable} | {fmt(scorable / reached if reached else None)} | {int(stats.get('checked') or 0)} | "
            f"{int(stats.get('killed_by_oracle_items') or 0)} | "
            f"{int(stats.get('killed_incidentally_items') or 0)} | "
            f"{int(stats.get('survived_items') or 0)} | "
            f"{int(stats.get('survived_path_changed_items') or 0)} | "
            f"{int(stats.get('coverage_unverified_items') or 0)} | {fmt(score)} | {fmt(representability)} |"
        )
    if unmeasurable_total > 0:
        lines.append(
            f"| exception_behavior: unmeasurable excluded | {unmeasurable_total} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |"
        )

    exception_total_stats = construct_totals.get("exception_behavior", Counter())
    if exception_total_stats and unmeasurable_total > 0:
        lines.extend([
            "",
            "### Exception Representability Limitation",
            "",
            f"- Unmeasurable exception items: {unmeasurable_total}",
        ])

    subcategory_totals: dict[str, Counter[str]] = defaultdict(Counter)
    subcategory_score_sum: Counter[str] = Counter()
    for row in collected:
        subcategories = row.get("assertion_strength_return_subcategories") or {}
        for subcategory, stats in subcategories.items():
            if not isinstance(stats, dict):
                continue
            key = str(subcategory)
            subcategory_totals[key]["applicable"] += int(stats.get("applicable") or stats.get("total") or 0)
            subcategory_totals[key]["reached"] += int(stats.get("reached") or 0)
            subcategory_totals[key]["represented"] += int(stats.get("represented") or 0)
            subcategory_totals[key]["scorable"] += int(stats.get("scorable") or 0)
            subcategory_totals[key]["checked"] += int(stats.get("checked") or 0)
            subcategory_totals[key]["degenerate_null"] += int(stats.get("degenerate_null") or stats.get("degenerate_null_items") or 0)
            subcategory_totals[key]["unobservable_return_state"] += int(
                stats.get("unobservable_return_state") or stats.get("unobservable_return_state_items") or 0
            )
            subcategory_score_sum[key] += float(stats.get("score_sum") or 0)


    lines.extend([
        "",
        "### Return Behavior By Subcategory",
        "",
        f"- Minimum scorable N for reported subcategory rates: {MIN_SUBCATEGORY_N}",
        "",
        "| Subcategory | Applicable | Reached | Represented | Scorable | Checked | Score | Representability | Degenerate Null |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for subcategory in RETURN_SUBCATEGORY_ORDER:
        if subcategory == "object":
            continue
        stats = subcategory_totals.get(subcategory, Counter())
        applicable = int(stats.get("applicable") or 0)
        reached = int(stats.get("reached") or 0)
        represented = int(stats.get("represented") or 0)
        scorable = int(stats.get("scorable") or 0)
        score = subcategory_score_sum[subcategory] / scorable if scorable else None
        representability = represented / reached if reached else None
        lines.append(
            f"| {subcategory} | {applicable} | {reached} | {represented} | {scorable} | {int(stats.get('checked') or 0)} | "
            f"{fmt_min_n(score, scorable)} | {fmt_min_n(representability, scorable)} | {int(stats.get('degenerate_null') or 0)} |"
        )

    object_unobservable = int(subcategory_totals.get("object", Counter()).get("unobservable_return_state") or 0)
    if object_unobservable:
        lines.extend([
            "",
            f"- Unobservable object-return obligations: {object_unobservable}",
        ])


    return lines


def assertion_strength_funnel_table(rows: list[dict[str, Any]]) -> list[str]:
    total_rows = len(rows)
    execution_passed = sum(1 for row in rows if row.get("execution_passed"))
    public_target = sum(1 for row in rows if not row.get("target_is_private"))
    executable_public_target = sum(1 for row in rows if row.get("execution_passed") and not row.get("target_is_private"))
    raw_evidence_rows = [
        row for row in rows if row.get("assertion_mutation_raw_status") not in (None, "", "missing")
    ]
    rows_with_evidence = [
        row
        for row in rows
        if row.get("execution_passed")
        and not row.get("target_is_private")
        and row.get("assertion_mutation_raw_status") not in (None, "", "missing")
    ]
    passed_evidence_rows = [row for row in rows_with_evidence if row.get("assertion_mutation_status") == "passed"]
    obligations_found = sum(int(row.get("assertion_strength_total_items") or 0) for row in rows_with_evidence)
    obligations_reached = sum(int(row.get("assertion_strength_reached_items") or 0) for row in rows_with_evidence)
    mutants_generated = sum(int(row.get("assertion_strength_represented_items") or 0) for row in rows_with_evidence)
    mutants_executed = sum(int(row.get("assertion_strength_scorable_items") or 0) for row in rows_with_evidence)
    scorable = mutants_executed
    checked = sum(int(row.get("assertion_strength_checked_items") or 0) for row in rows_with_evidence)
    path_changed = sum(int(row.get("assertion_strength_path_changed_items") or 0) for row in rows_with_evidence)
    coverage_unverified = sum(int(row.get("assertion_strength_coverage_unverified_items") or 0) for row in rows_with_evidence)
    unmeasurable = sum(int(row.get("assertion_strength_unmeasurable_by_type_substitution_items") or 0) for row in rows_with_evidence)
    return [
        "",
        "## Assertion Mutation Funnel",
        "",
        f"- Rows with raw assertion-mutation evidence: {len(raw_evidence_rows)}",
        "",
        "| Stage | N |",
        "| --- | ---: |",
        f"| rows in set | {total_rows} |",
        f"| -> execution_passed | {execution_passed} |",
        f"| -> execution_passed and public target method | {executable_public_target} |",
        f"| -> executable public rows with assertion_mutation artifact attached | {len(rows_with_evidence)} |",
        f"| -> executable public rows with passed assertion_mutation evidence | {len(passed_evidence_rows)} |",
        f"| -> obligations found | {obligations_found} |",
        f"| -> obligations reached | {obligations_reached} |",
        f"| -> mutants generated / represented | {mutants_generated} |",
        f"| -> score-usable obligations | {scorable} |",
        f"| -> checked obligations | {checked} |",
        f"| excluded represented survivors: target-method coverage changed | {path_changed} |",
        f"| non-representable tooling outcome: coverage comparison failed | {coverage_unverified} |",
        f"| unmeasurable_by_type_substitution (broad-original exceptions, excluded from denominator) | {unmeasurable} |",
        "",
        f"- Non-executable rows: {len(rows) - execution_passed}",
        f"- Executable private-target rows: {execution_passed - executable_public_target}",
    ]


def assertion_mutation_outcome_breakdown_table(rows: list[dict[str, Any]]) -> list[str]:
    collected = [row for row in executable_rows(rows) if row.get("assertion_mutation_status") == "passed"]
    construct_totals: dict[str, Counter[str]] = defaultdict(Counter)
    for row in collected:
        for construct, stats in (row.get("assertion_strength_by_construct") or {}).items():
            if not isinstance(stats, dict):
                continue
            counter = construct_totals[str(construct)]
            for key in [
                "total",
                "represented",
                "scorable",
                "checked",
                "not_reached_items",
                "not_generated_items",
                "out_of_scope_items",
                "compile_error_items",
                "timeout_items",
                "killed_by_oracle_items",
                "killed_incidentally_items",
                "survived_items",
                "survived_path_changed_items",
                "coverage_unverified_items",
                "non_representable_items",
            ]:
                counter[key] += int(stats.get(key) or 0)
    lines = [
        "",
        "### Assertion Mutation Outcome Breakdown",
        "",
        "| Construct | Total | Reached | Represented | Scorable | Scorable / Reached | Checked | Not Reached | Not Generated | Out Of Scope | Compile Error | Timeout | Coverage Failed | Oracle Killed | Incidental Killed | Unchanged-Path Survived | Path Changed | Non-Representable |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for construct, stats in sorted(construct_totals.items()):
        total = int(stats.get("total") or 0)
        not_reached = int(stats.get("not_reached_items") or 0)
        reached = total - not_reached
        lines.append(
            f"| {construct} | {total} | {reached} | {int(stats.get('represented') or 0)} | "
            f"{int(stats.get('scorable') or 0)} | {fmt(int(stats.get('scorable') or 0) / reached if reached else None)} | {int(stats.get('checked') or 0)} | "
            f"{not_reached} | {int(stats.get('not_generated_items') or 0)} | "
            f"{int(stats.get('out_of_scope_items') or 0)} | {int(stats.get('compile_error_items') or 0)} | "
            f"{int(stats.get('timeout_items') or 0)} | {int(stats.get('coverage_unverified_items') or 0)} | "
            f"{int(stats.get('killed_by_oracle_items') or 0)} | "
            f"{int(stats.get('killed_incidentally_items') or 0)} | {int(stats.get('survived_items') or 0)} | "
            f"{int(stats.get('survived_path_changed_items') or 0)} | "
            f"{int(stats.get('non_representable_items') or 0)} |"
        )
    return lines


def ratio_metric_by_group(rows: list[dict[str, Any]], group_key: str, title: str) -> list[str]:
    score_rows = executable_rows(rows)
    lines = [
        "",
        f"## Ratio Adequacy {title}",
        "",
        "| Group | Rows | Executable | Avg Row Mutation | Line Cov | Branch Cov | Boundary | Control Flow | Assertion Strength |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group, group_rows in sorted(group_rows_by(rows, group_key).items()):
        group_score_rows = executable_rows(group_rows)
        lines.append(
            f"| {group} | {len(group_rows)} | "
            f"{len(group_score_rows)} | "
            f"{fmt(avg([r['mutation_score'] for r in group_score_rows]))} | "
            f"{fmt(avg([r['coverage_line_rate'] for r in group_score_rows]))} | "
            f"{fmt(avg([r['coverage_branch_rate'] for r in group_score_rows]))} | "
            f"{fmt(avg([r['boundary_strength_score'] for r in group_score_rows]))} | "
            f"{fmt(avg([r['if_else_if_condition_avg_item_score'] for r in group_score_rows]))} | "
            f"{fmt(avg([r['assertion_strength_score'] for r in group_score_rows]))} |"
        )
    return lines


def extreme_mutation_funnel_table(rows: list[dict[str, Any]]) -> list[str]:
    def evidence_status(row: dict[str, Any]) -> Any:
        return row.get("extreme_condition_mutation_status") or (row.get("extreme_condition_mutation") or {}).get("status")

    executable = executable_rows(rows)
    eligible = [row for row in executable if not row.get("target_is_private")]
    attached = [row for row in eligible if evidence_status(row) not in (None, "", "missing")]
    collected = [row for row in attached if evidence_status(row) == "passed"]
    return [
        "",
        "## Extreme Mutation Funnel",
        "",
        "| Stage | N |",
        "| --- | ---: |",
        f"| Rows in set | {len(rows)} |",
        f"| Execution passed | {len(executable)} |",
        f"| Executable public-target rows | {len(eligible)} |",
        f"| Rows with extreme mutation evidence | {len(attached)} |",
        f"| Rows with passed extreme mutation evidence | {len(collected)} |",
        f"| Applicable obligations | {sum(int(row.get('if_else_if_condition_total') or 0) for row in collected)} |",
        f"| Represented obligations | {sum(int(row.get('if_else_if_condition_represented') or 0) for row in collected)} |",
        f"| Scorable obligations | {sum(int(row.get('if_else_if_condition_scorable') or 0) for row in collected)} |",
        f"| Checked obligations | {sum(int(row.get('if_else_if_condition_checked_items') or 0) for row in collected)} |",
    ]


def branch_condition_detail_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    condition_totals: dict[str, Counter[str]] = defaultdict(Counter)
    condition_score_sum: Counter[str] = Counter()
    pit_covered_totals: Counter[str] = Counter()
    pit_item_totals: Counter[str] = Counter()
    custom_constructs = {"if", "else_if", "ternary", "case", "default", "catch", "exception_path"}
    extreme_evidence_rows = sum(
        1
        for row in score_rows
        if (
            ((row.get("extreme_condition_mutation") or {}).get("status") == "passed")
            or row.get("extreme_condition_mutation_status") == "passed"
        )
    )
    for row in score_rows:
        by_construct = row.get("if_else_if_condition_by_construct") or {}
        pit_covered_by = row.get("if_else_if_condition_pit_covered_by_construct") or {}
        pit_total_by = row.get("if_else_if_condition_pit_total_by_construct") or {}
        if isinstance(by_construct, dict):
            for kind in custom_constructs:
                stats = by_construct.get(kind) or {}
                condition_totals["total"][kind] += int(stats.get("total") or 0)
                condition_totals["scorable"][kind] += int(stats.get("scorable") or stats.get("represented") or 0)
                condition_totals["checked"][kind] += int(stats.get("checked") or 0)
                total_variants = int(stats.get("total_variants") or 0)
                represented_variants = int(stats.get("represented_variants") or 0)
                represented_items = int(stats.get("represented") or 0)
                if total_variants == 0:
                    total_variants = int(stats.get("total") or 0)
                    represented_variants = represented_items
                elif represented_variants == 0 and represented_items > 0:
                    represented_variants = represented_items
                condition_totals["total_variants"][kind] += total_variants
                condition_totals["represented_variants"][kind] += represented_variants
                condition_score_sum[kind] += float(stats.get("score_sum") or 0)
        if isinstance(pit_covered_by, dict) and isinstance(pit_total_by, dict):
            for kind in custom_constructs:
                pit_covered_totals[kind] += int(pit_covered_by.get(kind) or 0)
                pit_item_totals[kind] += int(pit_total_by.get(kind) or 0)
    kinds = ["if", "else_if", "case", "default", "catch", "exception_path", "ternary", "other"]
    lines = [
        "",
        "## Control-Flow Adequacy Details",
        "",
        f"- Executable rows with custom control-flow evidence: {extreme_evidence_rows}/{len(score_rows)}",
        "",
        "| Construct | Applicable Items | Scorable Items | Checked Items | Avg Item Score | Pessimistic Bound | PIT Item Repr. | Expected Variants | Represented Variants | Variant Representability |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for kind in kinds:
        condition_total = condition_totals["total"][kind]
        if not condition_total:
            continue
        checked = condition_totals["checked"][kind]
        scorable = condition_totals["scorable"][kind]
        avg_item_score = condition_score_sum[kind] / scorable if scorable else None
        total_variants = condition_totals["total_variants"][kind]
        represented_variants = condition_totals["represented_variants"][kind]
        if total_variants:
            condition_repr_rate = represented_variants / total_variants
        else:
            condition_repr_rate = None
        pit_total = pit_item_totals[kind]
        pit_repr = pit_covered_totals[kind] / pit_total if pit_total else None
        lines.append(
            f"| {kind} | {condition_total} | {scorable} | "
            f"{checked} | {fmt(avg_item_score)} | "
            f"{fmt(checked / condition_total if condition_total else None)} | "
            f"{fmt(pit_repr)} | "
            f"{total_variants} | {represented_variants} | {fmt(condition_repr_rate)} |"
        )
    return lines


def exception_detail_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    lines = [
        "",
        "## Control-Flow Exception-Path Details",
        "",
        "| Prompt | Applicable Rows | Exception Obligations | Covered Paths | Asserted Paths | Weak Handlers | Type Assertions | Type+Context Assertions |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for prompt, prompt_rows in sorted(group_rows_by(score_rows, "prompt_template").items()):
        applicable = [row for row in prompt_rows if int(row.get("exception_obligations") or 0) > 0]
        lines.append(
            f"| {prompt} | {len(applicable)} | "
            f"{sum(int(row.get('exception_obligations') or 0) for row in applicable)} | "
            f"{sum(int(row.get('exception_covered') or 0) for row in applicable)} | "
            f"{sum(int(row.get('exception_asserted_count') or 0) for row in applicable)} | "
            f"{sum(int(row.get('weak_exception_handlers') or 0) for row in applicable)} | "
            f"{sum(int(row.get('exception_assertions') or 0) for row in applicable)} | "
            f"{sum(int(row.get('strong_exception_assertions') or 0) for row in applicable)} |"
        )
    return lines


def state_transition_detail_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    prompts = sorted(group_rows_by(score_rows, "prompt_template"))
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    case_totals: dict[str, Counter[str]] = defaultdict(Counter)
    for row in score_rows:
        prompt = str(row.get("prompt_template"))
        state_profile = row["profile"]["state_transition"]
        asserted_items = list(row.get("state_transition_assertion_evidence_items") or [])
        setup_evidence_items = list(row.get("state_transition_setup_evidence_items") or [])
        for item in state_profile.get("obligation_items") or []:
            totals["all_applicable"][state_kind(str(item.get("snippet") or ""))] += 1
            totals[f"{prompt}_applicable"][state_kind(str(item.get("snippet") or ""))] += 1
            case = str(item.get("state_case") or "unknown")
            case_totals["all_applicable"][case] += 1
            case_totals[f"{prompt}_applicable"][case] += 1
        for item in asserted_items:
            totals["all_asserted"][state_kind(str(item.get("snippet") or ""))] += 1
            totals[f"{prompt}_asserted"][state_kind(str(item.get("snippet") or ""))] += 1
            case = str(item.get("state_case") or "unknown")
            case_totals["all_asserted"][case] += 1
            case_totals[f"{prompt}_asserted"][case] += 1
        for item in setup_evidence_items:
            totals["all_setup"][state_kind(str(item.get("snippet") or ""))] += 1
            totals[f"{prompt}_setup"][state_kind(str(item.get("snippet") or ""))] += 1
    kinds = ["field_assignment", "increment_or_compound_update", "collection_mutation", "setter_or_set_call", "other_state_write"]
    lines = [
        "",
        "## State Side-Effect Details",
        "",
        "State side effects are restricted to static/global state or static-class properties changed by the FUT and not supplied as explicit input parameters.",
        "",
        "| State Signal | Applicable Items | Setup Items | Asserted Items | " + " | ".join(f"{prompt} Asserted/Applicable" for prompt in prompts) + " |",
        "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in prompts) + " |",
    ]
    for kind in kinds:
        if not totals["all_applicable"][kind]:
            continue
        prompt_cells = [f"{totals[f'{prompt}_asserted'][kind]}/{totals[f'{prompt}_applicable'][kind]}" for prompt in prompts]
        lines.append(
            f"| {kind} | {totals['all_applicable'][kind]} | {totals['all_setup'][kind]} | "
            f"{totals['all_asserted'][kind]} | {' | '.join(prompt_cells)} |"
        )
    lines.extend([
        "",
        "### State Initial-State Case Details",
        "",
        "| Initial-State Case | Applicable Items | Checked Items | Score | " + " | ".join(f"{prompt} Checked/Applicable" for prompt in prompts) + " |",
        "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in prompts) + " |",
    ])
    for case in ["null", "empty", "false_value", "true_value", "zero", "negative", "positive_normal", "duplicate", "overflow_or_long", "unknown"]:
        applicable = case_totals["all_applicable"][case]
        if not applicable:
            continue
        checked = case_totals["all_asserted"][case]
        prompt_cells = [
            f"{case_totals[f'{prompt}_asserted'][case]}/{case_totals[f'{prompt}_applicable'][case]}"
            for prompt in prompts
        ]
        lines.append(f"| {case} | {applicable} | {checked} | {fmt(checked / applicable if applicable else None)} | {' | '.join(prompt_cells)} |")
    return lines


def dependency_detail_table(rows: list[dict[str, Any]]) -> list[str]:
    score_rows = executable_rows(rows)
    prompts = sorted(group_rows_by(score_rows, "prompt_template"))
    case_totals: dict[str, Counter[str]] = defaultdict(Counter)
    kind_case_totals: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for row in score_rows:
        prompt = str(row.get("prompt_template"))
        for case in row.get("interaction_dependency_applicable_cases") or []:
            case_totals["all_applicable"][str(case)] += 1
            case_totals[f"{prompt}_applicable"][str(case)] += 1
        for case in row.get("interaction_dependency_achieved_cases") or []:
            case_totals["all_achieved"][str(case)] += 1
            case_totals[f"{prompt}_achieved"][str(case)] += 1
        for kind_case in row.get("interaction_dependency_applicable_kind_cases") or []:
            kind, _, case = str(kind_case).partition("::")
            if not case:
                continue
            key = (kind, case)
            kind_case_totals["all_applicable"][key] += 1
            kind_case_totals[f"{prompt}_applicable"][key] += 1
        for kind_case in row.get("interaction_dependency_achieved_kind_cases") or []:
            kind, _, case = str(kind_case).partition("::")
            if not case:
                continue
            key = (kind, case)
            kind_case_totals["all_achieved"][key] += 1
            kind_case_totals[f"{prompt}_achieved"][key] += 1
    lines = [
        "",
        "## External Resource Setup Details",
        "",
        "External resource setup is currently restricted to network/HTTP request or stream-interaction calls and file/stream I/O. URL construction and generic service/collaborator method names are not counted. The score measures whether generated tests set up or verify relevant external-resource outcomes, not whether every external side effect is semantically asserted.",
        "",
        "| Case | Applicable Items | Setup/Verified Items | Score | " + " | ".join(f"{prompt} Setup/Verified/Applicable" for prompt in prompts) + " |",
        "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in prompts) + " |",
    ]
    cases = [
        "null_value",
        "empty",
        "zero",
        "negative",
        "positive_normal",
        "http_success_status",
        "http_error_status",
        "exception",
        "large_or_boundary",
        "void_or_side_effect",
    ]
    for case in cases:
        applicable = case_totals["all_applicable"][case]
        achieved = case_totals["all_achieved"][case]
        if not applicable:
            continue
        prompt_cells = [
            f"{case_totals[f'{prompt}_achieved'][case]}/{case_totals[f'{prompt}_applicable'][case]}"
            for prompt in prompts
        ]
        lines.append(f"| {case} | {applicable} | {achieved} | {fmt(achieved / applicable if applicable else None)} | {' | '.join(prompt_cells)} |")
    lines.extend([
        "",
        "### External Resource Setup By Dependency Kind And Contract Case",
        "",
        "| Dependency Kind | Contract Case | Applicable Items | Setup/Verified Items | Score | " + " | ".join(f"{prompt} Setup/Verified/Applicable" for prompt in prompts) + " |",
        "| --- | --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in prompts) + " |",
    ])
    kind_order = {"filesystem_or_stream": 0, "network_or_http": 1, "other_dependency": 2}
    ordered_keys = sorted(
        kind_case_totals["all_applicable"],
        key=lambda item: (kind_order.get(item[0], 99), cases.index(item[1]) if item[1] in cases else len(cases), item[0], item[1]),
    )
    for key in ordered_keys:
        kind, case = key
        applicable = kind_case_totals["all_applicable"][key]
        achieved = kind_case_totals["all_achieved"][key]
        prompt_cells = [
            f"{kind_case_totals[f'{prompt}_achieved'][key]}/{kind_case_totals[f'{prompt}_applicable'][key]}"
            for prompt in prompts
        ]
        lines.append(
            f"| {kind} | {case} | {applicable} | {achieved} | "
            f"{fmt(achieved / applicable if applicable else None)} | {' | '.join(prompt_cells)} |"
        )
    return lines


def write_summary(rows: list[dict[str, Any]], out: Path) -> None:
    rows = [paper_profile(row) for row in rows]
    score_rows = executable_rows(rows)
    lines = [
        "# Java Bias Profile Summary",
    ]
    lines.extend(execution_summary_lines(rows))
    lines.extend(failure_classification_lines(rows))

    prompts = sorted(group_rows_by(score_rows, "prompt_template"))
    lines.extend([
        "",
        "## Boundary-Value Adequacy Details",
        "",
        "| Boundary Category | Applicable Items | Covered Items | Coverage Rate | " + " | ".join(f"{prompt} Covered/Applicable Items" for prompt in prompts) + " |",
        "| --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in prompts) + " |",
    ])
    for stats in boundary_category_summary(score_rows):
        prompt_cells = []
        for prompt in prompts:
            covered, applicable = stats["by_prompt"].get(prompt, (0, 0))
            prompt_cells.append(f"{covered}/{applicable}")
        lines.append(
            f"| {stats['category']} | {stats['applicable_items']} | {stats['covered_items']} | {fmt(stats['coverage_rate'])} | "
            f"{' | '.join(prompt_cells)} |"
        )
    lines.extend(extreme_mutation_funnel_table(rows))
    lines.extend(branch_condition_detail_table(rows))
    lines.extend(assertion_strength_funnel_table(rows))
    lines.extend(assertion_strength_table(rows))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_excluded_task_ids(paths: list[Path], explicit: list[str]) -> set[str]:
    excluded = set(explicit)
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            excluded.add(line.split()[0])
    return excluded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path, nargs="+", help="Java result JSONL files, or saved JSONL/.jsonl.gz profiles with --saved-profiles")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/java_blindspots"))
    parser.add_argument("--saved-profiles", action="store_true", help="Regenerate reports from saved analysis profiles without source workdirs or new scoring.")
    parser.add_argument("--exclude-task-id", action="append", default=[])
    parser.add_argument("--exclude-file", type=Path, action="append", default=[])
    parser.add_argument("--include-prompt", action="append", default=[], help="Only include rows with this prompt_template; can be repeated.")
    parser.add_argument("--exclude-prompt", action="append", default=[], help="Exclude rows with this prompt_template; can be repeated.")
    parser.add_argument("--include-complexity-bucket", action="append", default=[], help="Only include rows with this resolved complexity_bucket; can be repeated.")
    parser.add_argument(
        "--custom-control-flow-result",
        type=Path,
        action="append",
        default=[],
        help="Custom-control-flow JSONL file or directory used to attach extreme_condition_mutation evidence by task/prompt/model.",
    )
    parser.add_argument(
        "--assertion-mutation-result",
        type=Path,
        action="append",
        default=[],
        help="Assertion-mutation JSONL file or directory used to attach assertion_mutation evidence by task/prompt/model.",
    )
    args = parser.parse_args()
    if args.saved_profiles and (args.custom_control_flow_result or args.assertion_mutation_result):
        parser.error("saved profiles already contain analyzed evidence; external evidence options apply to raw results")

    excluded = load_excluded_task_ids(args.exclude_file, args.exclude_task_id)
    include_prompts = set(args.include_prompt)
    exclude_prompts = set(args.exclude_prompt)
    include_buckets = set(args.include_complexity_bucket)
    custom_control_flow_index = load_custom_control_flow_index(args.custom_control_flow_result)
    assertion_mutation_index = load_assertion_mutation_index(args.assertion_mutation_result)
    rows: list[dict[str, Any]] = []
    for path in args.results:
        rows.extend(
            row if args.saved_profiles else analyze_row(attach_external_evidence(row, custom_control_flow_index, assertion_mutation_index))
            for row in read_jsonl(path)
            if str(row.get("task_id")) not in excluded
            and (not include_prompts or str(row.get("prompt_template")) in include_prompts)
            and str(row.get("prompt_template")) not in exclude_prompts
            and (
                not include_buckets
                or str(
                    row.get("complexity_bucket")
                    or (row.get("task") or {}).get("v2_primary_split")
                    or (row.get("task") or {}).get("dataset")
                    or (row.get("task") or {}).get("complexity_bucket")
                    or "unknown"
                )
                in include_buckets
            )
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.out_dir / "blindspot_profiles.jsonl"
    csv_path = args.out_dir / "blindspot_profiles.csv"
    summary_path = args.out_dir / "summary.md"

    jsonl_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    flat_rows = [flatten(row) for row in rows]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        if flat_rows:
            writer = csv.DictWriter(handle, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
    write_summary(rows, summary_path)
    print(f"wrote {jsonl_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
