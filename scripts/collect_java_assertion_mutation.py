#!/usr/bin/env python3
"""Run deterministic custom oracle/interaction mutants for Java generated tests.

This collector is separate from the existing custom control-flow mutation
collector. It targets observable behavior obligations in public target methods:

  * return_behavior: replace a return value with a type-appropriate alternative
  * exception_behavior: change the thrown exception type
  * side_effect_or_dependency: future-work dependency-call mutants
  * interaction_dependency: future-work dependency-interaction mutants

Each reached, represented item is scored over oracle outcomes: assertion-backed
kills mean checked, survived mutants mean unchecked, and incidental runtime
kills are reported separately. Unreached and non-viable mutants are excluded
from the oracle denominator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from analyze_java_blindspots import (
    find_matching_brace,
    find_matching_paren,
    is_assertable_external_side_effect_item,
    is_private_method_signature,
    return_obligation_items,
    signature_parameter_names,
    source_obligation_items,
    split_java_arguments,
    target_method_source,
)
from collect_java_extreme_condition_mutation import (
    DEFAULT_MVN,
    EXEC_PLUGIN,
    alternate_return_expression,
    existing_rows,
    find_statement_end,
    line_bounds,
    method_return_type,
    panta_test_class,
    read_jsonl,
    row_key,
    run_command,
)


ROOT = Path(__file__).resolve().parents[1]
ASSERTION_MUTATION_VERSION = "custom_assertion_mutation_v9"
LOG_DIR_NAME = "assertion-mutants"
REPRESENTED_STATUSES = {
    "killed_by_oracle",
    "killed_incidentally",
    "survived",
    "survived_path_changed",
}
NON_REPRESENTED_STATUSES = {
    "compile_error",
    "timeout",
    "command_missing",
    "not_generated",
    "out_of_scope",
    "coverage_unverified",
}
JAVA_BIN = Path(os.environ.get("JAVA_BIN") or (str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.environ.get("JAVA_HOME") else shutil.which("java") or "java"))
JACOCO_AGENT = Path(
    os.environ.get(
        "JACOCO_AGENT",
        str(
            Path.home()
            / ".m2/repository/org/jacoco/org.jacoco.agent/0.8.14/org.jacoco.agent-0.8.14-runtime.jar"
        ),
    )
)


def copy_project_for_mutant(workdir: Path, prompt_name: str, label: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"java-assertion-mutant-{prompt_name}-{label[:40]}-"))

    def ignore(_: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {"target", LOG_DIR_NAME, ".git", ".gradle", "build"}
        }

    clone = root / "project"
    shutil.copytree(workdir, clone, ignore=ignore)
    return clone


def original_log_path(workdir: Path, prompt_name: str, label: str, suffix: str) -> Path:
    return workdir / LOG_DIR_NAME / prompt_name / f"{label}-{suffix}.log"


def copy_log_back(source: str | None, destination: Path) -> str | None:
    if not source:
        return None
    src = Path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copyfile(src, destination)
    return str(destination)


def git_status_short(cwd: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def write_cleanliness_report(path: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
    report = {
        "collector_version": ASSERTION_MUTATION_VERSION,
        "check": "git status --short",
        "unchanged": before.get("stdout") == after.get("stdout") and before.get("returncode") == after.get("returncode"),
        "before": before,
        "after": after,
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", source):
        offsets.append(match.end())
    return offsets


def offset_to_line(offsets: list[int], offset: int) -> int:
    line = 1
    for index, start in enumerate(offsets, start=1):
        if start > offset:
            break
        line = index
    return line


def parse_subject_covered_lines(path: Path) -> set[int]:
    if not path.exists():
        return set()
    root = ET.parse(path).getroot()
    sourcefile = None
    for candidate in root.findall(".//sourcefile"):
        if candidate.attrib.get("name") == "Subject.java":
            sourcefile = candidate
            break
    if sourcefile is None:
        return set()
    covered: set[int] = set()
    for line in sourcefile.findall("line"):
        number = int(line.attrib.get("nr", "0") or 0)
        instruction_hits = int(line.attrib.get("ci", "0") or 0)
        branch_hits = int(line.attrib.get("cb", "0") or 0)
        if number and (instruction_hits > 0 or branch_hits > 0):
            covered.add(number)
    return covered


def collect_subject_line_coverage(
    workdir: Path,
    prompt_name: str,
    timeout: int,
    mvn: str,
    test_class: str | None,
) -> dict[str, Any]:
    log_dir = workdir / LOG_DIR_NAME / prompt_name
    jacoco_xml = workdir / "target/site/jacoco/jacoco.xml"
    if not JAVA_BIN.exists():
        return {"status": "failed", "stage": "java_bin_missing", "covered_lines": []}
    if not JACOCO_AGENT.exists():
        return {"status": "failed", "stage": "jacoco_agent_missing", "covered_lines": []}
    compile_result = run_command(
        [mvn, "-q", "test-compile"],
        workdir,
        log_dir / "original-coverage-compile.log",
        timeout,
    )
    if compile_result["status"] == "timeout":
        return {"status": "failed", "stage": "compile_timeout", "covered_lines": []}
    if compile_result["status"] == "command_missing":
        return {"status": "failed", "stage": "command_missing", "covered_lines": []}
    if compile_result["returncode"] != 0:
        return {"status": "failed", "stage": "compile", "covered_lines": []}

    if test_class:
        execute_result = run_command(
            [mvn, "-q", f"-Dtest={test_class}", f"-DargLine=-javaagent:{JACOCO_AGENT}=destfile=target/jacoco.exec", "test"],
            workdir,
            log_dir / "original-coverage-execute.log",
            timeout,
        )
    else:
        execute_result = run_command(
            [
                str(JAVA_BIN),
                f"-javaagent:{JACOCO_AGENT}=destfile=target/jacoco.exec",
                "-cp",
                "target/classes:target/test-classes",
                "benchmark.GeneratedSmokeTest",
            ],
            workdir,
            log_dir / "original-coverage-execute.log",
            timeout,
        )
    if execute_result["status"] == "timeout":
        return {"status": "failed", "stage": "execute_timeout", "covered_lines": parse_subject_covered_lines(jacoco_xml)}
    if execute_result["status"] == "command_missing":
        return {"status": "failed", "stage": "command_missing", "covered_lines": parse_subject_covered_lines(jacoco_xml)}
    if execute_result["returncode"] != 0:
        return {"status": "failed", "stage": "execute", "covered_lines": parse_subject_covered_lines(jacoco_xml)}

    report_result = run_command(
        [mvn, "-q", "org.jacoco:jacoco-maven-plugin:0.8.14:report"],
        workdir,
        log_dir / "original-coverage-report.log",
        timeout,
    )
    covered_lines = parse_subject_covered_lines(jacoco_xml)
    if report_result["status"] == "timeout":
        return {"status": "failed", "stage": "report_timeout", "covered_lines": sorted(covered_lines)}
    if report_result["returncode"] != 0:
        return {"status": "failed", "stage": "report", "covered_lines": sorted(covered_lines)}
    return {"status": "passed", "stage": None, "covered_lines": sorted(covered_lines)}


ORACLE_FAILURE_RE = re.compile(
    r"AssertionError|AssertionFailedError|org\.opentest4j|junit\.framework\.AssertionFailedError|"
    r"org\.mockito\.exceptions\.verification\.(?:WantedButNotInvoked|NeverWantedButInvoked|TooManyActualInvocations)|"
    r"\bexpected\b|but was|wrong exception|Expected .*exception",
    re.IGNORECASE,
)
STRICTNESS_FAILURE_RE = re.compile(
    r"org\.mockito\.exceptions\.misusing\.(?:UnnecessaryStubbingException|PotentialStubbingProblem)|"
    r"\b(?:UnnecessaryStubbingException|PotentialStubbingProblem)\b",
    re.IGNORECASE,
)


def exception_name_matches(failure_type: str | None, expected_type: str | None) -> bool:
    if not failure_type or not expected_type:
        return False
    failure_simple = exception_simple_name(failure_type)
    expected_simple = exception_simple_name(expected_type)
    if failure_type == expected_type or failure_simple == expected_simple:
        return True
    known_parents = {
        "IllegalStateException": {"RuntimeException", "Exception", "Throwable"},
        "IllegalArgumentException": {"RuntimeException", "Exception", "Throwable"},
        "UnsupportedOperationException": {"RuntimeException", "Exception", "Throwable"},
        "NullPointerException": {"RuntimeException", "Exception", "Throwable"},
        "IndexOutOfBoundsException": {"RuntimeException", "Exception", "Throwable"},
        "ArithmeticException": {"RuntimeException", "Exception", "Throwable"},
        "NumberFormatException": {"IllegalArgumentException", "RuntimeException", "Exception", "Throwable"},
        "RuntimeException": {"Exception", "Throwable"},
        "java.io.IOException": {"Exception", "Throwable"},
        "IOException": {"Exception", "Throwable"},
        "java.text.ParseException": {"Exception", "Throwable"},
        "ParseException": {"Exception", "Throwable"},
        "java.sql.SQLException": {"Exception", "Throwable"},
        "SQLException": {"Exception", "Throwable"},
    }
    return expected_simple in known_parents.get(failure_simple, set())


def exception_caught_by(catch_type: str, exception_type: str) -> bool:
    catch_simple = exception_simple_name(catch_type)
    exception_simple = exception_simple_name(exception_type)
    return exception_name_matches(exception_type, catch_type) or catch_simple in {"Throwable", "Exception"}


def candidate_changes_enclosing_handler(source: str | None, offset: int | None, original_type: str, candidate_type: str) -> bool:
    if source is None or offset is None:
        return True
    catches = enclosing_try_catch_types(source, offset)
    if not catches:
        return True
    original_handlers = [catch for catch in catches if exception_caught_by(catch, original_type)]
    candidate_handlers = [catch for catch in catches if exception_caught_by(catch, candidate_type)]
    if not original_handlers and not candidate_handlers:
        return True
    return original_handlers[:1] != candidate_handlers[:1]


def killed_status_from_execute_log(path_value: str | None, replacement_exception_type: str | None = None) -> tuple[str, str | None]:
    if not path_value:
        return "killed_incidentally", None
    path = Path(path_value)
    log_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if STRICTNESS_FAILURE_RE.search(log_text):
        return "killed_incidentally", "mockito_strictness_failure"
    if ORACLE_FAILURE_RE.search(log_text):
        return "killed_by_oracle", "assertion_failure_or_assertion_library_signal"
    java_class_message = re.search(r"Java class\.\s*(.*?)\s*(?:->\s*\[Help|\n|$)", log_text, flags=re.DOTALL)
    if java_class_message:
        message = java_class_message.group(1).strip()
        throwable = re.search(r"\b([A-Za-z_$][\w$.]*(?:Exception|Error|Throwable))\b", message)
        if throwable:
            failure_type = throwable.group(1)
            if exception_name_matches(failure_type, replacement_exception_type):
                return "killed_by_oracle", f"mutated_exception_type_escaped:{failure_type}"
            return "killed_incidentally", failure_type
        if message:
            return "killed_incidentally", message[:200]
    throwable = re.search(r"\b([A-Za-z_$][\w$.]*(?:Exception|Error|Throwable))\b", log_text)
    failure_type = throwable.group(1) if throwable else None
    if exception_name_matches(failure_type, replacement_exception_type):
        return "killed_by_oracle", f"mutated_exception_type_escaped:{failure_type}"
    return "killed_incidentally", failure_type if failure_type else "non_assertion_test_failure"


def return_type_category(return_type: str | None) -> str:
    typ = re.sub(r"<.*>", "", return_type or "").strip()
    if not typ:
        return "unknown"
    if typ in {"boolean", "Boolean"}:
        return "boolean"
    if typ in {"byte", "short", "int", "long", "float", "double", "Byte", "Short", "Integer", "Long", "Float", "Double"}:
        return "numeric"
    if typ in {"char", "Character"}:
        return "char"
    if typ in {"String", "java.lang.String"}:
        return "string"
    if typ in {"Class", "java.lang.Class"}:
        return "class"
    if typ.endswith("[]"):
        return "array"
    if typ in {"List", "Set", "Map", "Collection", "Iterable", "java.util.List", "java.util.Set", "java.util.Map", "java.util.Collection", "java.lang.Iterable"}:
        return "collection"
    return "domain_object"


def run_generated_test(
    workdir: Path,
    prompt_name: str,
    label: str,
    timeout: int,
    mvn: str,
    test_class: str | None = None,
    collect_coverage: bool = False,
) -> dict[str, Any]:
    log_dir = workdir / LOG_DIR_NAME / prompt_name
    compile_result = run_command(
        [mvn, "-q", "test-compile"],
        workdir,
        log_dir / f"{label}-compile.log",
        timeout,
    )
    if compile_result["status"] == "timeout":
        return {"status": "timeout", "stage": "compile", "compile_log": compile_result["log"]}
    if compile_result["status"] == "command_missing":
        return {"status": "command_missing", "stage": "compile", "compile_log": compile_result["log"]}
    if compile_result["returncode"] != 0:
        return {
            "status": "compile_error",
            "stage": "compile",
            "compile_returncode": compile_result["returncode"],
            "compile_log": compile_result["log"],
        }
    if test_class:
        exec_args = [mvn, "-q", f"-Dtest={test_class}"]
        if collect_coverage:
            exec_args.append(f"-DargLine=-javaagent:{JACOCO_AGENT}=destfile=target/jacoco.exec")
        exec_args.append("test")
    elif collect_coverage and JAVA_BIN.exists() and JACOCO_AGENT.exists():
        exec_args = [
            str(JAVA_BIN),
            f"-javaagent:{JACOCO_AGENT}=destfile=target/jacoco.exec",
            "-cp",
            "target/classes:target/test-classes",
            "benchmark.GeneratedSmokeTest",
        ]
    else:
        exec_args = [
            mvn,
            "-q",
            "-Dexec.mainClass=benchmark.GeneratedSmokeTest",
            "-Dexec.classpathScope=test",
            EXEC_PLUGIN,
        ]
    exec_result = run_command(exec_args, workdir, log_dir / f"{label}-execute.log", timeout)
    if exec_result["status"] == "timeout":
        return {
            "status": "timeout",
            "stage": "execute",
            "compile_log": compile_result["log"],
            "execute_log": exec_result["log"],
        }
    if exec_result["status"] == "command_missing":
        return {
            "status": "command_missing",
            "stage": "execute",
            "compile_log": compile_result["log"],
            "execute_log": exec_result["log"],
        }
    if exec_result["returncode"] == 0:
        coverage_result: dict[str, Any] = {"coverage_status": "not_requested", "covered_lines": []}
        if collect_coverage:
            report_result = run_command(
                [mvn, "-q", "org.jacoco:jacoco-maven-plugin:0.8.14:report"],
                workdir,
                log_dir / f"{label}-coverage-report.log",
                timeout,
            )
            jacoco_xml = workdir / "target/site/jacoco/jacoco.xml"
            coverage_result = {
                "coverage_status": "passed" if report_result.get("returncode") == 0 else "failed",
                "coverage_stage": None if report_result.get("returncode") == 0 else "report",
                "coverage_log": report_result.get("log"),
                "covered_lines": sorted(parse_subject_covered_lines(jacoco_xml)),
            }
        return {
            "status": "survived",
            "stage": "execute",
            "compile_log": compile_result["log"],
            "execute_log": exec_result["log"],
            "execute_returncode": exec_result["returncode"],
            **coverage_result,
        }
    return {
        "status": "killed",
        "stage": "execute",
        "compile_log": compile_result["log"],
        "execute_log": exec_result["log"],
        "execute_returncode": exec_result["returncode"],
    }


def run_isolated_mutant_test(
    workdir: Path,
    prompt_name: str,
    label: str,
    mutated_source: str,
    timeout: int,
    mvn: str,
    test_class: str | None = None,
    collect_coverage: bool = False,
) -> dict[str, Any]:
    clone = copy_project_for_mutant(workdir, prompt_name, label)
    try:
        clone_source_path = clone / "src/main/java/benchmark/Subject.java"
        if not clone_source_path.exists():
            return {"status": "not_generated", "stage": "isolated_subject_missing"}
        clone_source_path.write_text(mutated_source, encoding="utf-8")
        result = run_generated_test(
            clone,
            prompt_name,
            label,
            timeout,
            mvn,
            test_class,
            collect_coverage=collect_coverage,
        )
        compile_log = copy_log_back(
            result.get("compile_log"),
            original_log_path(workdir, prompt_name, label, "compile"),
        )
        execute_log = copy_log_back(
            result.get("execute_log"),
            original_log_path(workdir, prompt_name, label, "execute"),
        )
        coverage_log = copy_log_back(
            result.get("coverage_log"),
            original_log_path(workdir, prompt_name, label, "coverage-report"),
        )
        return {
            **result,
            "compile_log": compile_log,
            "execute_log": execute_log,
            "coverage_log": coverage_log,
            "isolated_workdir": str(clone),
        }
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)


def return_statement_at_line(source: str, line_number: int) -> tuple[int, int, str | None] | None:
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, _ = bounds
    match = re.search(r"\breturn\b", source[line_start:])
    if not match:
        return None
    return_start = line_start + match.start()
    statement_end = find_statement_end(source, return_start)
    if statement_end is None:
        return None
    expr = source[return_start + len("return") : statement_end - 1].strip()
    return return_start, statement_end, expr or None


def erased_return_type(return_type: str | None) -> str:
    return re.sub(r"<.*>", "", return_type or "").strip()


def simple_type_name(return_type: str | None) -> str:
    erased = erased_return_type(return_type)
    if erased.endswith("[]"):
        return erased[:-2].split(".")[-1] + "[]"
    return erased.split(".")[-1]


def class_bound(return_type: str | None) -> str | None:
    text = return_type or ""
    match = re.search(r"Class\s*<\s*\?\s+extends\s+([A-Za-z_$][\w$.]*)\s*>", text)
    if match:
        return match.group(1)
    return None


def bounded_class_alternatives(bound: str) -> list[str]:
    simple = exception_simple_name(bound)
    if simple in {"Number"}:
        return ["Integer.class", "Long.class", "Double.class"]
    if simple in {"CharSequence"}:
        return ["String.class", "StringBuilder.class"]
    if simple in {"Object"}:
        return ["String.class", "Object.class"]
    if simple in {"Throwable"}:
        return ["RuntimeException.class", "Exception.class"]
    if simple in {"Exception"}:
        return ["RuntimeException.class", "java.io.IOException.class"]
    if simple in {"RuntimeException"}:
        return ["IllegalArgumentException.class", "IllegalStateException.class"]
    return []


def class_return_alternative(return_type: str | None, original_expr: str | None) -> tuple[str | None, str]:
    expr = (original_expr or "").strip()
    class_match = re.fullmatch(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.class", expr)
    if not class_match:
        return None, "class_original_not_class_token"
    original_class = class_match.group(1).split(".")[-1]
    bound = class_bound(return_type)
    if bound:
        for candidate in bounded_class_alternatives(bound):
            if candidate.split(".")[0] != original_class:
                return candidate, "class_type_changed_with_declared_bound"
        return None, "class_bound_no_safe_replacement"
    return ("Object.class" if original_class != "Object" else "String.class"), "class_type_changed"


def is_numeric_literal(text: str) -> bool:
    return bool(re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?[fFdDlL]?", text.strip()))


def is_zero_numeric_literal(text: str) -> bool:
    stripped = text.strip().rstrip("fFdDlL")
    try:
        return float(stripped) == 0.0
    except ValueError:
        return False


def parenthesized_expression(expr: str) -> str:
    stripped = expr.strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*|\d+[lL]?|'(?:\\.|[^'\\])'", stripped):
        return stripped
    return f"({stripped})"


def enum_constants(source: str, return_type: str | None) -> list[str]:
    if not return_type:
        return []
    simple_type = simple_type_name(return_type)
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", simple_type):
        return []
    enum_match = re.search(rf"\benum\s+{re.escape(simple_type)}\s*\{{(?P<body>.*?)\n\s*\}}", source, flags=re.DOTALL)
    if not enum_match:
        return []
    constants_text = enum_match.group("body").split(";")[0]
    return [
        token
        for token in re.findall(r"\b([A-Z][A-Z0-9_]*)\b", constants_text)
        if token not in {"ENUM"}
    ]


def enum_return_alternative(source: str, return_type: str | None, original_expr: str | None) -> str | None:
    if not return_type or not original_expr:
        return None
    simple_type = simple_type_name(return_type)
    constants = enum_constants(source, return_type)
    current = original_expr.split(".")[-1].strip()
    if current in constants and len(constants) > 1:
        next_constant = constants[(constants.index(current) + 1) % len(constants)]
        return f"{simple_type}.{next_constant}" if "." in original_expr else next_constant
    for constant in constants:
        if constant != current:
            return f"{simple_type}.{constant}" if "." in original_expr else constant
    return None


def provably_empty_expression(original_expr: str | None) -> bool:
    original = (original_expr or "").strip()
    return bool(
        original in {'""', "null"}
        or re.fullmatch(r"new\s+[A-Za-z_$][\w$.\[\]]+\s*\[\s*0\s*\]", original)
        or re.fullmatch(r"(?:java\.util\.)?Collections\.empty(?:List|Set|Map)\s*\(\s*\)", original)
        or re.fullmatch(r"(?:java\.util\.)?(?:List|Set|Map)\.of\s*\(\s*\)", original)
    )


def generic_arguments(type_text: str | None) -> list[str]:
    text = type_text or ""
    start = text.find("<")
    end = text.rfind(">")
    if start < 0 or end <= start:
        return []
    return [arg.strip() for arg in split_java_arguments(text[start + 1 : end])]


def sample_literal_for_type(type_text: str | None) -> str:
    simple = simple_type_name(type_text)
    if simple in {"String"}:
        return '"MUTATED1"'
    if simple in {"boolean", "Boolean"}:
        return "true"
    if simple in {"byte", "short", "int", "long", "Byte", "Short", "Integer", "Long"}:
        return "1"
    if simple in {"float", "double", "Float", "Double"}:
        return "1.0"
    if simple in {"char", "Character"}:
        return "'x'"
    if simple in {"Object", "?"} or not simple:
        return "new Object()"
    if re.fullmatch(r"[A-Za-z_$][\w$]*", simple):
        return f"new {simple}()"
    return "new Object()"


def collection_return_alternative(return_type: str | None, original_expr: str | None) -> str | None:
    erased_type = erased_return_type(return_type)
    original_empty = provably_empty_expression(original_expr)
    if erased_type.endswith("[]"):
        element_type = erased_type[:-2].strip()
        if original_empty:
            if element_type in {"boolean"}:
                return "new boolean[] { true }"
            if element_type in {"byte", "short", "int", "long", "float", "double"}:
                return f"new {element_type}[] {{ 1 }}"
            if element_type == "char":
                return "new char[] { 'x' }"
            return f"new {element_type}[] {{ {sample_literal_for_type(element_type)} }}"
        return f"new {element_type}[0]"
    simple = simple_type_name(return_type)
    generic_args = generic_arguments(return_type)
    element = sample_literal_for_type(generic_args[0] if generic_args else "Object")
    if simple in {"List", "Collection", "Iterable"}:
        return f"new java.util.ArrayList<>(java.util.Arrays.asList({element}))" if original_empty else "new java.util.ArrayList<>()"
    if simple == "Set":
        return f"new java.util.LinkedHashSet<>(java.util.Arrays.asList({element}))" if original_empty else "new java.util.LinkedHashSet<>()"
    if simple == "Map":
        value = sample_literal_for_type(generic_args[1] if len(generic_args) > 1 else "Object")
        return f"new java.util.HashMap<>(java.util.Collections.singletonMap(\"MUTATED1\", {value}))" if original_empty else "new java.util.HashMap<>()"
    return None


def direct_new_expression(original_expr: str | None) -> tuple[str, str] | None:
    original = (original_expr or "").strip()
    match = re.match(r"new\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(", original)
    if not match:
        return None
    open_paren = original.find("(", match.end() - 1)
    close_paren = find_matching_paren(original, open_paren)
    if close_paren is None or original[close_paren + 1 :].strip():
        return None
    return match.group(1), original[open_paren + 1 : close_paren]


def safe_constructor_default_for_type(type_text: str | None) -> str | None:
    simple = simple_type_name(type_text)
    if simple in {"String"}:
        return '"MUTATED1"'
    if simple in {"boolean", "Boolean"}:
        return "true"
    if simple in {"byte", "short", "int", "long", "Byte", "Short", "Integer", "Long"}:
        return "1"
    if simple in {"float", "double", "Float", "Double"}:
        return "1.0"
    if simple in {"char", "Character"}:
        return "'x'"
    return None


def package_name(source: str | None) -> str:
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", source or "", flags=re.MULTILINE)
    return match.group(1) if match else ""


def test_source_for_class(workdir: Path, test_class: str | None) -> Path | None:
    if test_class:
        rel = Path(*test_class.split(".")).with_suffix(".java")
        path = workdir / "src/test/java" / rel
        if path.exists():
            return path
    default = workdir / "src/test/java/benchmark/GeneratedSmokeTest.java"
    return default if default.exists() else None


def generated_test_same_package(workdir: Path, subject_source: str, test_class: str | None) -> bool:
    test_path = test_source_for_class(workdir, test_class)
    if not test_path:
        return False
    test_source = test_path.read_text(encoding="utf-8", errors="replace")
    return package_name(subject_source) == package_name(test_source)


def concrete_constructor_return_alternative(source: str, original_expr: str | None) -> str | None:
    parsed = direct_new_expression(original_expr)
    if not parsed:
        return None
    concrete_type, args_text = parsed
    original_args = split_java_arguments(args_text)
    if "." in concrete_type and not concrete_type.startswith("Subject."):
        return None
    class_name = concrete_type.split(".")[-1]
    span = class_body_span(source, class_name)
    if span is None:
        return None
    body = source[span[0] : span[1]]
    constructor_matches = list(
        re.finditer(
            rf"\b(?:public|protected|private)?\s*{re.escape(class_name)}\s*\(([^)]*)\)",
            body,
            flags=re.MULTILINE,
        )
    )
    if not constructor_matches:
        return f"new {class_name}()"
    for constructor in constructor_matches:
        params = split_java_arguments(constructor.group(1))
        if len(params) != len(original_args):
            continue
        defaults = []
        for param in params:
            pieces = param.strip().split()
            if len(pieces) < 2:
                return None
            default = safe_constructor_default_for_type(" ".join(pieces[:-1]))
            if default is None:
                return None
            defaults.append(default)
        return f"new {class_name}({', '.join(defaults)})"
    return None


def record_return_alternative(source: str, original_expr: str | None) -> str | None:
    parsed = direct_new_expression(original_expr)
    if not parsed:
        return None
    concrete_type, args_text = parsed
    if "." in concrete_type and not concrete_type.startswith("Subject."):
        return None
    class_name = concrete_type.split(".")[-1]
    record_match = re.search(rf"\brecord\s+{re.escape(class_name)}\s*\(([^)]*)\)", source, flags=re.MULTILINE)
    if not record_match:
        return None
    components = split_java_arguments(record_match.group(1))
    original_args = split_java_arguments(args_text)
    if len(components) != len(original_args):
        return None
    defaults = []
    for component in components:
        pieces = component.strip().split()
        if len(pieces) < 2:
            return None
        default = safe_constructor_default_for_type(" ".join(pieces[:-1]))
        if default is None:
            return None
        defaults.append(default)
    return f"new {class_name}({', '.join(defaults)})"


def class_body_span(source: str, class_name: str) -> tuple[int, int] | None:
    match = re.search(rf"\b(?:class|record)\s+{re.escape(class_name)}\b[^{{]*\{{", source)
    if not match:
        return None
    open_brace = source.find("{", match.start())
    close_brace = find_matching_brace(source, open_brace)
    if close_brace is None:
        return None
    return open_brace + 1, close_brace


def class_header(source: str, class_name: str) -> str | None:
    match = re.search(rf"\b(?:class|record)\s+{re.escape(class_name)}\b([^\{{]*)\{{", source)
    return match.group(1) if match else None


def inherited_type_name(source: str, class_name: str) -> str | None:
    header = class_header(source, class_name) or ""
    match = re.search(r"\bextends\s+([A-Za-z_$][\w$.]*)", header)
    return match.group(1).split(".")[-1] if match else None


def has_accessible_no_arg_constructor(source: str, class_name: str, same_package_test: bool = False) -> bool:
    span = class_body_span(source, class_name)
    if span is None:
        return False
    body = source[span[0] : span[1]]
    constructors = list(re.finditer(rf"\b(?:(public|protected|private)\s+)?{re.escape(class_name)}\s*\(([^)]*)\)", body))
    if not constructors:
        return True
    for match in constructors:
        access = match.group(1) or "package"
        if match.group(2).strip():
            continue
        if access in {"public", "protected"} or (access == "package" and same_package_test):
            return True
    return False


def class_direct_body(source: str, class_name: str) -> str | None:
    span = class_body_span(source, class_name)
    if span is None:
        return None
    return source[span[0] : span[1]]


def top_level_class_member_text(body: str) -> str:
    lines: list[str] = []
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            lines.append(line)
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return "\n".join(lines)


def observable_state_signal_for_class(
    source: str,
    class_name: str,
    same_package_test: bool,
    inherited: bool = True,
    seen: set[str] | None = None,
) -> str | None:
    seen = seen or set()
    if class_name in seen:
        return None
    seen.add(class_name)
    body = class_direct_body(source, class_name)
    if body is None:
        return None
    top_level_body = top_level_class_member_text(body)

    def access_visible(access: str | None) -> bool:
        normalized = access or "package"
        return normalized == "public" or normalized == "protected" or (normalized == "package" and same_package_test)

    field_re = re.compile(
        r"(?m)^\s*(?:(public|protected|private)\s+)?"
        r"(?!(?:class|interface|enum|record|return|if|for|while|switch|catch|try|else)\b)"
        r"(?:static\s+)?(?:final\s+)?[A-Za-z_$][\w$.\[\]<>?,\s]*\s+([A-Za-z_$][\w$]*)\s*(?:=|;)"
    )
    for match in field_re.finditer(top_level_body):
        access = match.group(1) or "package"
        if access_visible(access):
            return "public_field" if access == "public" else f"{access}_field_same_package"

    getter_re = re.compile(
        r"(?m)^\s*(?:(public|protected|private)\s+)?(?:final\s+)?"
        r"[A-Za-z_$][\w$.\[\]<>?,\s]*\s+((?:get|is)[A-Z][A-Za-z0-9_$]*)\s*\(\s*\)\s*(?:throws\s+[^{]+)?\{"
    )
    for match in getter_re.finditer(body):
        access = match.group(1) or "package"
        if access_visible(access):
            return "public_getter" if access == "public" else f"{access}_getter_same_package"

    bare_re = re.compile(
        r"(?m)^\s*(?:(public|protected|private)\s+)?(?:final\s+)?"
        r"([A-Za-z_$][\w$.\[\]<>?,]*)\s+([a-z][A-Za-z0-9_$]*)\s*\(\s*\)\s*(?:throws\s+[^{]+)?\{"
    )
    for match in bare_re.finditer(body):
        access = match.group(1) or "package"
        return_type = match.group(2)
        method_name = match.group(3)
        if access_visible(access) and return_type != "void" and method_name not in {"hashCode", "toString", "clone", "iterator", "stream"}:
            return "public_bare_accessor" if access == "public" else f"{access}_bare_accessor_same_package"

    fluent_re = re.compile(
        r"(?m)^\s*(?:(public|protected|private)\s+)?(?:final\s+)?"
        r"([A-Za-z_$][\w$.\[\]<>?,]*)\s+([A-Za-z_$][\w$]*)\s*\(\s*\)\s*(?:throws\s+[^{]+)?\{([^{}]{0,500})\}"
    )
    for match in fluent_re.finditer(body):
        access = match.group(1) or "package"
        return_type = match.group(2)
        method_name = match.group(3)
        method_body = match.group(4)
        if (
            access_visible(access)
            and return_type != "void"
            and method_name not in {"hashCode", "toString", "clone"}
            and re.search(r"\breturn\s+(?:this\.)?[A-Za-z_$][\w$]*\s*;", method_body)
        ):
            return "public_fluent_accessor" if access == "public" else f"{access}_fluent_accessor_same_package"

    if re.search(r"(?m)^\s*(?:@Override\s*)?(?:(public|protected)\s+)?boolean\s+equals\s*\(\s*Object\s+\w+\s*\)", body):
        return "overridden_equals"

    if inherited:
        parent = inherited_type_name(source, class_name)
        if parent:
            signal = observable_state_signal_for_class(source, parent, same_package_test, inherited=True, seen=seen)
            if signal:
                return f"inherited_{signal}"
    return None


def accessible_field_defaults_constructor(
    source: str,
    return_type: str | None,
    same_package_test: bool = False,
) -> dict[str, Any] | None:
    class_name = simple_type_name(return_type)
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", class_name):
        return None
    if not has_accessible_no_arg_constructor(source, class_name, same_package_test):
        return None
    signal = observable_state_signal_for_class(source, class_name, same_package_test)
    if signal:
        return {
            "expression": f"new {class_name}()",
            "observability_signal": signal,
            "same_package_test": same_package_test,
        }
    return None


def immutable_single_value_return_alternative(source: str, return_type: str | None, original_expr: str | None) -> str | None:
    class_name = simple_type_name(return_type)
    original = (original_expr or "").strip()
    if not re.fullmatch(rf"new\s+{re.escape(class_name)}\s*\((.*)\)", original, flags=re.DOTALL):
        return None
    match = re.fullmatch(rf"new\s+{re.escape(class_name)}\s*\((.*)\)", original, flags=re.DOTALL)
    if not match:
        return None
    args = split_java_arguments(match.group(1))
    if len(args) != 1:
        return None
    arg = args[0].strip()
    if re.fullmatch(r"-?\d+[lL]?", arg):
        suffix = "L" if arg.endswith(("l", "L")) else ""
        value = int(arg.rstrip("lL")) + 1
        return f"new {class_name}({value}{suffix})"
    if re.fullmatch(r"\".*\"", arg, flags=re.DOTALL):
        return f"new {class_name}({arg[:-1]}MUTATED1\")"
    if arg in {"true", "false"}:
        return f"new {class_name}({'false' if arg == 'true' else 'true'})"
    return None


def alternate_return_for_source(
    source: str,
    return_type: str | None,
    original_expr: str | None,
    same_package_test: bool = False,
) -> dict[str, Any]:
    erased_type = erased_return_type(return_type)
    original = (original_expr or "").strip()
    category = return_type_category(return_type)
    if erased_type == "boolean":
        if original == "true":
            return {"expression": "false", "rule": "boolean_negate"}
        if original == "false":
            return {"expression": "true", "rule": "boolean_negate"}
        return {"expression": f"!({original})" if original else "false", "rule": "boolean_negate"}
    if erased_type == "Boolean":
        if original == "null":
            return {"expression": "false", "rule": "return_null_type_compatible_replacement"}
        if original == "true":
            return {"expression": "false", "rule": "boolean_negate"}
        if original == "false":
            return {"expression": "true", "rule": "boolean_negate"}
        return {"expression": None, "rule": "boxed_boolean_expression_not_generated_unboxing_guard"}
    if erased_type in {"byte", "short", "int", "long", "char"}:
        if original == "null":
            return {"expression": "0", "rule": "return_null_type_compatible_replacement"}
        if not is_numeric_literal(original):
            replacement = f"{parenthesized_expression(original)} + 1"
            if erased_type == "byte":
                replacement = f"(byte) ({replacement})"
            elif erased_type == "short":
                replacement = f"(short) ({replacement})"
            elif erased_type == "char":
                replacement = f"(char) ({replacement})"
            return {"expression": replacement, "rule": "integral_expression_plus_one_noop_safe"}
        return {"expression": "1" if is_zero_numeric_literal(original) else "0", "rule": "numeric_zero_or_one_literal_only"}
    if erased_type in {"Byte", "Short", "Integer", "Long", "Character"}:
        if original == "null":
            return {"expression": "0", "rule": "return_null_type_compatible_replacement"}
        if not is_numeric_literal(original):
            return {"expression": None, "rule": "boxed_integral_expression_not_generated_unboxing_guard"}
        return {"expression": "1" if is_zero_numeric_literal(original) else "0", "rule": "numeric_zero_or_one_literal_only"}
    if erased_type in {"float", "double", "Float", "Double"}:
        if original == "null":
            return {"expression": "0.0", "rule": "return_null_type_compatible_replacement"}
        if not is_numeric_literal(original):
            return {"expression": None, "rule": "floating_expression_not_generated_noop_guard"}
        return {"expression": "1.0" if is_zero_numeric_literal(original) else "0.0", "rule": "floating_zero_or_one_literal_only"}
    if erased_type in {"String", "java.lang.String"}:
        if original == "null":
            return {"expression": '""', "rule": "return_null_type_compatible_replacement"}
        if not re.fullmatch(r'".*"', original, flags=re.DOTALL):
            return {"expression": f"{parenthesized_expression(original)} + \"MUTATED1\"", "rule": "string_expression_concatenated_noop_safe"}
        return {"expression": '"MUTATED1"' if original == '""' else '""', "rule": "string_empty_or_mutated_literal_only"}
    collection_alt = collection_return_alternative(return_type, original_expr)
    if collection_alt:
        return {
            "expression": collection_alt,
            "rule": "collection_map_array_empty_or_singleton",
        }
    enum_alt = enum_return_alternative(source, return_type, original_expr)
    if enum_alt:
        return {"expression": enum_alt, "rule": "enum_next_declared_constant"}
    constants = enum_constants(source, return_type)
    if len(constants) == 1:
        return {"expression": None, "rule": "enum_single_constant_not_generated"}
    if erased_type in {"Class", "java.lang.Class"}:
        if original == "null":
            class_alt, rule = class_return_alternative(return_type, "String.class")
            return {"expression": class_alt or "String.class", "rule": f"return_null_type_compatible_replacement_{rule}"}
        class_alt, rule = class_return_alternative(return_type, original_expr)
        return {"expression": class_alt, "rule": rule}
    record_alt = record_return_alternative(source, original_expr)
    if record_alt:
        return {"expression": record_alt, "rule": "domain_object_record_canonical_defaults"}
    concrete_constructor_alt = concrete_constructor_return_alternative(source, original_expr)
    if concrete_constructor_alt:
        return {"expression": concrete_constructor_alt, "rule": "domain_object_concrete_constructor_defaults"}
    default_object = accessible_field_defaults_constructor(source, return_type, same_package_test)
    if default_object:
        return {
            "expression": default_object["expression"],
            "rule": "domain_object_default_constructor",
            "observability_signal": default_object.get("observability_signal"),
            "same_package_test": default_object.get("same_package_test"),
        }
    immutable_alt = immutable_single_value_return_alternative(source, return_type, original_expr)
    if immutable_alt:
        return {"expression": immutable_alt, "rule": "domain_object_single_value_perturbed"}
    if category == "domain_object" and original != "null":
        if has_accessible_no_arg_constructor(source, simple_type_name(return_type), same_package_test):
            return {
                "expression": None,
                "rule": "unobservable_return_state",
                "unobservable_return_state": True,
                "same_package_test": same_package_test,
            }
        return {"expression": "null", "rule": "opaque_object_degenerate_null", "degenerate_null": True}
    if original == "null":
        typed_alt = collection_return_alternative(return_type, original_expr)
        if typed_alt:
            return {"expression": typed_alt, "rule": "return_null_type_compatible_replacement"}
        if erased_type in {"String", "java.lang.String"}:
            return {"expression": '""', "rule": "return_null_type_compatible_replacement"}
        if erased_type in {"Object", "java.lang.Object"}:
            return {"expression": "new Object()", "rule": "return_null_type_compatible_replacement"}
    fallback = alternate_return_expression(return_type, original_expr)
    if fallback and fallback != original:
        return {"expression": fallback, "rule": "legacy_type_fallback"}
    return {"expression": None, "rule": "not_generated"}


def mutated_return_source(
    source: str,
    item: dict[str, Any],
    return_type: str | None,
    same_package_test: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_return_line"}
    found = return_statement_at_line(source, line_number)
    if found is None:
        return None, {"operator_rule": "return_statement_not_found"}
    return_start, statement_end, expr = found
    replacement_info = alternate_return_for_source(source, return_type, expr, same_package_test)
    if "rule" in replacement_info and "operator_rule" not in replacement_info:
        replacement_info = {**replacement_info, "operator_rule": replacement_info["rule"]}
    replacement = replacement_info.get("expression")
    if replacement is None:
        return None, replacement_info
    return source[:return_start] + f"return {replacement};" + source[statement_end:], replacement_info


UNCHECKED_EXCEPTION_ALTERNATIVES = [
    "IllegalStateException",
    "IllegalArgumentException",
    "UnsupportedOperationException",
    "NullPointerException",
    "IndexOutOfBoundsException",
]
BROAD_ORIGINAL_EXCEPTION_TYPES = {"RuntimeException", "Exception", "Throwable"}


CHECKED_EXCEPTION_ALTERNATIVES = [
    "java.io.IOException",
    "java.text.ParseException",
    "java.sql.SQLException",
]


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


def exception_simple_name(exception_type: str) -> str:
    return exception_type.split(".")[-1]


def declared_exception_types(signature: str | None) -> list[str]:
    if not signature:
        return []
    match = re.search(r"\bthrows\s+(.+)$", " ".join(signature.split()))
    if not match:
        return []
    return [part.strip().split()[0] for part in split_java_arguments(match.group(1))]


def declared_exception_types_at_offset(source: str | None, offset: int | None) -> list[str]:
    if source is None or offset is None:
        return []
    method_pattern = re.compile(
        r"(?:^|[;{}\n])\s*(?:@[^\n]+\s*)*"
        r"(?:public|protected|private|static|final|synchronized|abstract|native|strictfp|\s)+"
        r"[A-Za-z_$][\w$.\[\]<>?,\s]*\s+"
        r"[A-Za-z_$][\w$]*\s*\([^;{}]*\)\s*"
        r"(?:throws\s+([^{}]+?))?\s*\{",
        flags=re.MULTILINE,
    )
    best: tuple[int, list[str]] | None = None
    for match in method_pattern.finditer(source):
        open_brace = source.find("{", match.end() - 1)
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None or not (open_brace < offset < close_brace):
            continue
        throws_text = match.group(1) or ""
        declared = [part.strip().split()[0] for part in split_java_arguments(throws_text)] if throws_text else []
        if best is None or open_brace > best[0]:
            best = (open_brace, declared)
    return best[1] if best else []


def offset_inside_lambda_body(source: str | None, offset: int | None) -> bool:
    if source is None or offset is None:
        return False
    for match in re.finditer(r"->\s*\{", source):
        open_brace = source.find("{", match.end() - 1)
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is not None and open_brace < offset < close_brace:
            return True
    return False


def offset_inside_anonymous_class_body(source: str | None, offset: int | None) -> bool:
    if source is None or offset is None:
        return False
    for match in re.finditer(r"\bnew\s+[A-Za-z_$][\w$.<>?]*\s*\([^)]*\)\s*\{", source):
        open_brace = source.find("{", match.end() - 1)
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is not None and open_brace < offset < close_brace:
            return True
    return False


def is_checked_exception_type(exception_type: str) -> bool:
    simple = exception_simple_name(exception_type)
    unchecked = {
        "RuntimeException",
        "IllegalStateException",
        "IllegalArgumentException",
        "UnsupportedOperationException",
        "NullPointerException",
        "IndexOutOfBoundsException",
        "ArithmeticException",
        "NumberFormatException",
    }
    return simple.endswith("Exception") and simple not in unchecked


def exception_parent_types(source: str | None, type_name: str) -> set[str]:
    simple = exception_simple_name(type_name)
    parents = {
        "IllegalStateException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "IllegalArgumentException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "UnsupportedOperationException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "NullPointerException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "IndexOutOfBoundsException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "ArithmeticException": {"RuntimeException", "Exception", "Throwable", "Object"},
        "NumberFormatException": {"IllegalArgumentException", "RuntimeException", "Exception", "Throwable", "Object"},
        "RuntimeException": {"Exception", "Throwable", "Object"},
        "IOException": {"Exception", "Throwable", "Object"},
        "ParseException": {"Exception", "Throwable", "Object"},
        "SQLException": {"Exception", "Throwable", "Object"},
        "Exception": {"Throwable", "Object"},
        "Throwable": {"Object"},
    }.get(simple, set())
    if source:
        match = re.search(rf"\bclass\s+{re.escape(simple)}\b[^\{{;]*\bextends\s+([A-Za-z_$][\w$.]*)", source)
        if match:
            parent = exception_simple_name(match.group(1))
            parents = set(parents)
            parents.add(parent)
            parents.update(exception_parent_types(source, parent))
    return parents


def exception_type_assignable_to(source: str | None, candidate_type: str, declared_type: str | None) -> bool:
    declared = simple_type_name(declared_type)
    candidate = exception_simple_name(candidate_type)
    if not declared or declared == "var":
        return True
    if declared == candidate:
        return True
    if declared in {"Throwable", "Exception", "Object"}:
        return declared in exception_parent_types(source, candidate)
    return declared in exception_parent_types(source, candidate)


def expression_type_hint(expr: str) -> str | None:
    text = expr.strip()
    if not text:
        return None
    if text == "null":
        return "null"
    if re.fullmatch(r'".*"', text, flags=re.DOTALL) or '"' in text and "+" in text:
        return "String"
    if re.fullmatch(r"'.'", text, flags=re.DOTALL):
        return "char"
    if text in {"true", "false"}:
        return "boolean"
    if re.fullmatch(r"-?\d+[lL]", text):
        return "long"
    if re.fullmatch(r"-?\d+", text):
        return "int"
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)(?:[fFdD])?", text):
        return "double"
    match = re.fullmatch(r"new\s+([A-Za-z_$][\w$.<>?]*)\s*\(.*\)", text, flags=re.DOTALL)
    if match:
        return simple_type_name(match.group(1))
    if re.fullmatch(r"[A-Za-z_$][\w$]*", text):
        return "unknown"
    return "unknown"


def constructor_param_type(param: str) -> str | None:
    text = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", param.strip())
    text = re.sub(r"\b(?:public|protected|private|final)\b\s*", "", text).strip()
    if not text:
        return None
    pieces = text.rsplit(None, 1)
    if len(pieces) == 1:
        return pieces[0].replace("...", "[]")
    return pieces[0].replace("...", "[]")


def argument_compatible_with_type(source: str | None, arg_type: str | None, param_type: str | None) -> bool:
    if not arg_type or not param_type:
        return False
    param_simple = simple_type_name(param_type)
    arg_simple = simple_type_name(arg_type)
    if arg_simple == "unknown":
        return param_simple == "Object"
    if arg_simple == "null":
        return param_simple not in {"boolean", "byte", "short", "int", "long", "float", "double", "char"}
    if arg_simple == param_simple:
        return True
    if param_simple == "Object":
        return arg_simple not in {"boolean", "byte", "short", "int", "long", "float", "double", "char"}
    if arg_simple == "String" and param_simple in {"Object", "CharSequence"}:
        return True
    numeric = {"byte", "short", "int", "long", "float", "double", "Byte", "Short", "Integer", "Long", "Float", "Double"}
    if arg_simple in numeric and param_simple in numeric:
        return True
    if arg_simple == "char" and param_simple in {"char", "Character", "int", "long", "float", "double"}:
        return True
    if arg_simple == "boolean" and param_simple in {"boolean", "Boolean"}:
        return True
    return exception_type_assignable_to(source, arg_simple, param_simple)


def source_exception_constructor_param_lists(source: str, exception_type: str) -> list[list[str]]:
    class_name = exception_simple_name(exception_type)
    span = class_body_span(source, class_name)
    if span is None:
        return []
    body = source[span[0] : span[1]]
    constructors = []
    for constructor in re.finditer(rf"\b(?:public|protected|private)?\s*{re.escape(class_name)}\s*\(([^)]*)\)", body):
        params = split_java_arguments(constructor.group(1))
        constructors.append([constructor_param_type(param) or "" for param in params])
    return constructors


def source_defines_type(source: str | None, type_name: str) -> bool:
    if not source:
        return False
    return class_body_span(source, exception_simple_name(type_name)) is not None


def known_exception_constructor_param_lists(exception_type: str) -> list[list[str]]:
    simple = exception_simple_name(exception_type)
    if simple in set(UNCHECKED_EXCEPTION_ALTERNATIVES) | {"RuntimeException", "Exception", "IOException", "SQLException"}:
        return [[], ["String"], ["Throwable"], ["String", "Throwable"]]
    if simple == "ParseException":
        return [["String", "int"]]
    return []


def exception_constructor_accepts_args(source: str, exception_type: str, args_text: str) -> bool:
    args = split_java_arguments(args_text)
    arg_types = [expression_type_hint(arg) for arg in args]
    constructors = source_exception_constructor_param_lists(source, exception_type) or known_exception_constructor_param_lists(exception_type)
    if not constructors:
        return False
    for params in constructors:
        if len(params) != len(arg_types):
            continue
        if all(argument_compatible_with_type(source, arg_type, param_type) for arg_type, param_type in zip(arg_types, params)):
            return True
    return False


def replacement_exception_type(
    original_type: str,
    signature: str | None,
    source: str | None = None,
    offset: int | None = None,
    required_assignable_to: str | None = None,
) -> tuple[str | None, str]:
    original_simple = exception_simple_name(original_type)
    local_declared = declared_exception_types_at_offset(source, offset)
    nested_function_scope = offset_inside_lambda_body(source, offset) or offset_inside_anonymous_class_body(source, offset)
    declared = [] if nested_function_scope else (local_declared or declared_exception_types(signature))
    rejected_for_assignability = False

    def usable_candidate(candidate_type: str) -> bool:
        nonlocal rejected_for_assignability
        if not candidate_changes_enclosing_handler(source, offset, original_type, candidate_type):
            return False
        if required_assignable_to and not exception_type_assignable_to(source, candidate_type, required_assignable_to):
            rejected_for_assignability = True
            return False
        return True

    if is_checked_exception_type(original_type):
        for candidate in declared:
            if (
                exception_simple_name(candidate) != original_simple
                and is_checked_exception_type(candidate)
                and usable_candidate(candidate)
            ):
                return candidate, "checked_exception_declared_sibling"
        for candidate in CHECKED_EXCEPTION_ALTERNATIVES:
            if (
                exception_simple_name(candidate) != original_simple
                and candidate in declared
                and usable_candidate(candidate)
            ):
                return candidate, "checked_exception_declared_sibling"
        for candidate in UNCHECKED_EXCEPTION_ALTERNATIVES:
            if usable_candidate(candidate):
                return candidate, "checked_exception_fallback_unchecked"
        if rejected_for_assignability:
            return None, "replacement_not_assignable_to_declared_type"
        return None, "not_generated"
    preferred = [candidate for candidate in UNCHECKED_EXCEPTION_ALTERNATIVES if candidate != original_simple]
    for candidate in preferred:
        if candidate != original_simple and usable_candidate(candidate):
            return candidate, "unchecked_exception_sibling"
    if rejected_for_assignability:
        return None, "replacement_not_assignable_to_declared_type"
    return None, "exception_replacement_subsumed_by_enclosing_catch"


def first_constructor_arg_is_string_like(args: list[str]) -> bool:
    if not args:
        return False
    first = args[0].strip()
    return bool(
        re.fullmatch(r'".*"', first, flags=re.DOTALL)
        or '"' in first and "+" in first
        or re.search(r"\b(?:String\.valueOf|String\.format|Objects\.toString)\s*\(", first)
        or re.search(r"\.getMessage\s*\(\s*\)", first)
    )


def string_like_constructor_args(args_text: str) -> bool:
    args = split_java_arguments(args_text)
    if not args:
        return True
    if len(args) > 2:
        return False
    if not first_constructor_arg_is_string_like(args):
        return False
    if len(args) == 1:
        return True
    second = args[1].strip()
    return bool(re.fullmatch(r"[A-Za-z_$][\w$]*", second) or re.fullmatch(r"new\s+[A-Za-z_$][\w$.]*Exception\s*\(", second))


def replacement_constructor_preserves_string_prefix(source: str, replacement: str, original_args: str) -> bool:
    args = split_java_arguments(original_args)
    if not first_constructor_arg_is_string_like(args):
        return False
    class_name = exception_simple_name(replacement)
    span = class_body_span(source, class_name)
    if span is None:
        return False
    body = source[span[0] : span[1]]
    for constructor in re.finditer(rf"\b(?:public|protected|private)?\s*{re.escape(class_name)}\s*\(([^)]*)\)", body):
        params = split_java_arguments(constructor.group(1))
        if len(params) != len(args):
            continue
        first_param_type = " ".join(params[0].strip().split()[:-1])
        if simple_type_name(first_param_type) == "String":
            return True
    return False


def replacement_exception_constructor_args(source: str, replacement: str, rule: str, original_args: str) -> tuple[str | None, str]:
    if source_defines_type(source, replacement) and not exception_constructor_accepts_args(source, replacement, original_args):
        return None, "replacement_constructor_incompatible"
    if string_like_constructor_args(original_args):
        return None, "preserved_original_exception_constructor_args"
    if replacement_constructor_preserves_string_prefix(source, replacement, original_args):
        return None, "preserved_original_exception_constructor_args_custom_string_prefix"
    simple = exception_simple_name(replacement)
    if rule.endswith("_fallback_unchecked") or simple in UNCHECKED_EXCEPTION_ALTERNATIVES:
        return '"MUTATED1"', "replacement_exception_string_constructor_fallback"
    if simple in {"IOException", "Exception"}:
        return '"MUTATED1"', "replacement_exception_string_constructor_fallback"
    return None, "preserved_original_exception_constructor_args_unverified"


def mutate_exception_type_at(
    source: str,
    type_start: int,
    type_end: int,
    original_type: str,
    signature: str | None,
    required_assignable_to: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    original_simple = exception_simple_name(original_type)
    broad_original = original_simple in BROAD_ORIGINAL_EXCEPTION_TYPES
    replacement, rule = replacement_exception_type(
        original_type,
        signature,
        source,
        type_start,
        required_assignable_to=required_assignable_to,
    )
    if not replacement:
        meta = {"operator_rule": rule, "original_exception_type": original_type}
        if broad_original:
            meta["subsumed_by_broad_original"] = True
        if required_assignable_to:
            meta["declared_assignment_type"] = required_assignable_to
        return None, meta
    arg_start = source.find("(", type_end)
    arg_end = find_matching_paren(source, arg_start) if arg_start >= 0 else None
    original_args = source[arg_start + 1 : arg_end] if arg_start >= 0 and arg_end is not None else ""
    args_replacement, constructor_rule = replacement_exception_constructor_args(source, replacement, rule, original_args)
    if constructor_rule == "replacement_constructor_incompatible":
        return (
            None,
            {
                "operator_rule": constructor_rule,
                "original_exception_type": original_type,
                "replacement_exception_type": replacement,
                **({"subsumed_by_broad_original": True} if broad_original else {}),
                **({"declared_assignment_type": required_assignable_to} if required_assignable_to else {}),
            },
        )
    if args_replacement is not None and arg_end is not None:
        mutated = source[:type_start] + replacement + source[type_end:arg_start + 1] + args_replacement + source[arg_end:]
    else:
        mutated = source[:type_start] + replacement + source[type_end:]
    return (
        mutated,
        {
            "operator_rule": rule,
            "constructor_rule": constructor_rule,
            "original_exception_type": original_type,
            "replacement_exception_type": replacement,
            **({"subsumed_by_broad_original": True} if broad_original else {}),
            **({"declared_assignment_type": required_assignable_to} if required_assignable_to else {}),
        },
    )


def declared_local_type_before(source: str, variable: str, start: int, end: int) -> str | None:
    decl_pattern = re.compile(
        rf"\b(?:final\s+)?([A-Za-z_$][\w$.<>?\[\]]*)\s+{re.escape(variable)}\b",
        flags=re.MULTILINE,
    )
    declarations = list(decl_pattern.finditer(source, start, end))
    if not declarations:
        return None
    return declarations[-1].group(1)


def mutated_indirect_exception_source(source: str, line_number: int, variable: str, signature: str | None) -> tuple[str | None, dict[str, Any]]:
    method_source = target_method_source(source, signature)
    if method_source is None:
        return None, {"operator_rule": "indirect_throw_no_target_method_source"}
    method_start = source.find(method_source)
    if method_start < 0:
        return None, {"operator_rule": "indirect_throw_no_target_method_source"}
    throw_bounds = line_bounds(source, line_number)
    if throw_bounds is None:
        return None, {"operator_rule": "indirect_throw_line_not_found"}
    search_end = throw_bounds[0]
    assignment_pattern = re.compile(
        rf"\b{re.escape(variable)}\s*=\s*new\s+(?P<assigned_type>[A-Za-z_$][\w$.]*)\s*\(|"
        rf"\b(?:final\s+)?(?P<declared_type>[A-Za-z_$][\w$.<>?\[\]]*)\s+{re.escape(variable)}\s*=\s*new\s+(?P<declared_assigned_type>[A-Za-z_$][\w$.]*)\s*\(",
        flags=re.MULTILINE,
    )
    matches = list(assignment_pattern.finditer(source, method_start, search_end))
    if not matches:
        return None, {"operator_rule": "indirect_throw_constructor_assignment_not_found"}
    match = matches[-1]
    if match.group("assigned_type"):
        group_name = "assigned_type"
        declared_type = declared_local_type_before(source, variable, method_start, match.start())
    else:
        group_name = "declared_assigned_type"
        declared_type = match.group("declared_type")
    return mutate_exception_type_at(
        source,
        match.start(group_name),
        match.end(group_name),
        match.group(group_name),
        signature,
        required_assignable_to=declared_type,
    )


def mutated_exception_type_source(source: str, item: dict[str, Any], signature: str | None) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_exception_line"}
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None, {"operator_rule": "exception_line_not_found"}
    line_start, _ = bounds
    throw_match = re.search(r"\bthrow\s+new\s+([A-Za-z_$][\w$.]*)\s*\(", source[line_start:])
    if throw_match:
        type_start = line_start + throw_match.start(1)
        type_end = line_start + throw_match.end(1)
        original_type = throw_match.group(1)
        return mutate_exception_type_at(source, type_start, type_end, original_type, signature)
    indirect = re.search(r"\bthrow\s+([A-Za-z_$][\w$]*)\s*;", source[line_start:])
    if indirect:
        return mutated_indirect_exception_source(source, line_number, indirect.group(1), signature)
    return None, {"operator_rule": "exception_factory_or_complex_throw_not_generated"}


def statement_start_for_offset(source: str, offset: int) -> int:
    previous_semicolon = source.rfind(";", 0, offset)
    previous_open_brace = source.rfind("{", 0, offset)
    previous_close_brace = source.rfind("}", 0, offset)
    previous_newline = source.rfind("\n", 0, offset)
    start = max(previous_semicolon, previous_open_brace, previous_close_brace, previous_newline) + 1
    while start < len(source) and source[start].isspace() and source[start] != "\n":
        start += 1
    return start


def source_offset_for_line(source: str, line_number: int) -> int | None:
    bounds = line_bounds(source, line_number)
    return bounds[0] if bounds else None


def enclosing_try_catch_types(source: str, offset: int) -> list[str]:
    types: list[str] = []
    for match in re.finditer(r"\btry\s*(?:\([^)]*\))?\s*\{", source):
        open_brace = source.find("{", match.end() - 1)
        if open_brace < 0:
            continue
        close_brace = find_matching_brace(source, open_brace)
        if close_brace is None or not (open_brace < offset < close_brace):
            continue
        tail = source[close_brace : close_brace + 500]
        for catch_match in re.finditer(r"\bcatch\s*\(\s*([A-Za-z_$][\w$.]*)\s+\w+\s*\)", tail):
            types.append(catch_match.group(1))
    return types


def catch_redirect_hazard(source: str, offset: int) -> bool:
    hazardous = {"IOException", "FileNotFoundException", "java.io.IOException", "java.io.FileNotFoundException"}
    return any(catch_type in hazardous for catch_type in enclosing_try_catch_types(source, offset))


def constructor_argument_text(line: str, constructor_name: str) -> str | None:
    match = re.search(rf"\bnew\s+(?:java\.io\.)?{re.escape(constructor_name)}\s*\(", line)
    if not match:
        return None
    open_paren = line.find("(", match.start())
    close_paren = find_matching_paren(line, open_paren)
    if close_paren is None:
        return None
    return line[open_paren + 1 : close_paren]


def path_argument_flows_from_parameter(line: str, signature: str | None) -> bool:
    names = signature_parameter_names(signature)
    if not names:
        return False
    for constructor in ["FileOutputStream", "OutputStreamWriter", "FileWriter", "BufferedWriter"]:
        args = constructor_argument_text(line, constructor)
        if args and any(re.search(rf"\b{re.escape(name)}\b", args) for name in names):
            return True
    return False


def variable_uses(text: str, variable: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"\b{re.escape(variable)}\b", text))


def statement_lhs_rhs(statement: str) -> tuple[str | None, str]:
    declaration = re.match(
        r"\s*(?:final\s+)?[A-Za-z_$][\w$.<>\[\]?]*\s+([A-Za-z_$][\w$]*)\s*=\s*(.*)$",
        statement,
        flags=re.DOTALL,
    )
    if declaration:
        return declaration.group(1), declaration.group(2)
    assignment = re.match(r"\s*([A-Za-z_$][\w$]*)\s*=\s*(.*)$", statement, flags=re.DOTALL)
    if assignment:
        return assignment.group(1), assignment.group(2)
    return None, statement


def statement_is_explicit_reassignment(statement: str) -> bool:
    lhs, rhs = statement_lhs_rhs(statement)
    return bool(lhs and re.search(rf"\b{re.escape(lhs)}\b", rhs))


def target_method_span(source: str, signature: str | None) -> tuple[int, int] | None:
    method_source = target_method_source(source, signature)
    if method_source is None:
        return None
    start = source.find(method_source)
    if start < 0:
        return None
    return start, start + len(method_source)


def mutated_try_resource_body_source(source: str, line_number: int) -> tuple[str | None, dict[str, Any]]:
    line_offset = source_offset_for_line(source, line_number)
    if line_offset is None:
        return None, {"operator_rule": "try_resource_line_not_found"}
    for match in re.finditer(r"\btry\s*\(", source):
        open_paren = source.find("(", match.start())
        close_paren = find_matching_paren(source, open_paren)
        if close_paren is None:
            continue
        open_brace = source.find("{", close_paren)
        close_brace = find_matching_brace(source, open_brace) if open_brace >= 0 else None
        if open_brace >= 0 and close_brace is not None and match.start() <= line_offset <= open_brace:
            indent = re.match(r"[ \t]*", source[source.rfind("\n", 0, open_brace) + 1 : open_brace])
            replacement = "\n" + (indent.group(0) if indent else "") + "    ;\n" + (indent.group(0) if indent else "")
            return source[: open_brace + 1] + replacement + source[close_brace:], {"operator_rule": "try_resource_body_deleted"}
    return None, {"operator_rule": "not_try_resource"}


def mutated_statement_removed_source(source: str, item: dict[str, Any], signature: str | None = None) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_statement_line"}
    line_offset = source_offset_for_line(source, line_number)
    if line_offset is not None and catch_redirect_hazard(source, line_offset):
        return None, {"operator_rule": "deletion_catch_reachability_hazard"}
    try_source, try_meta = mutated_try_resource_body_source(source, line_number)
    if try_source is not None:
        return try_source, try_meta
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None, {"operator_rule": "statement_line_not_found"}
    line_start, line_end = bounds
    snippet = source[line_start:line_end]
    call_match = re.search(r"\b(?:Files\.\w+|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+|new\s+[A-Za-z_$][\w$]*)\s*\(", snippet)
    if not call_match:
        return None, {"operator_rule": "call_not_found"}
    call_start = line_start + call_match.start()
    statement_start = statement_start_for_offset(source, call_start)
    statement_end = find_statement_end(source, statement_start)
    if statement_end is None:
        return None, {"operator_rule": "statement_end_not_found"}
    statement = source[statement_start:statement_end]
    method_span = target_method_span(source, signature)
    lhs, _ = statement_lhs_rhs(statement)
    if lhs:
        method_end = method_span[1] if method_span else len(source)
        after_statement = source[statement_end:method_end]
        if variable_uses(after_statement, lhs) and not statement_is_explicit_reassignment(statement):
            return None, {"operator_rule": "def_use_out_of_scope", "defined_variable": lhs}
        if statement_is_explicit_reassignment(statement):
            rule = "explicit_reassignment_deleted"
        else:
            rule = "statement_deleted"
    else:
        rule = "statement_deleted"
    indent = re.match(r"[ \t]*", source[source.rfind("\n", 0, statement_start) + 1 : statement_start])
    replacement = (indent.group(0) if indent else "") + ";\n"
    return source[:statement_start] + replacement + source[statement_end:], {
        "operator_rule": rule,
        "call_behavior": "suppressed",
        "mutation_dimension": "invocation",
        "confounded_dimensions": [],
    }


def mutated_output_constructor_source(source: str, item: dict[str, Any], signature: str | None = None) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_output_constructor_line"}
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None, {"operator_rule": "output_constructor_line_not_found"}
    line_start, line_end = bounds
    line = source[line_start:line_end]
    statement_start = statement_start_for_offset(source, line_start)
    statement_end = find_statement_end(source, statement_start) or line_end
    declared_type, _, _ = assignment_declaration_type(source[statement_start:statement_end])
    if catch_redirect_hazard(source, line_start):
        return None, {"operator_rule": "redirect_catch_hazard"}
    if path_argument_flows_from_parameter(line, signature):
        return None, {"operator_rule": "redirect_invalid_path_parameter_flow"}
    replacements = [
        (
            r"new\s+(?:java\.io\.)?FileOutputStream\s*\([^;]+?\)",
            "new java.io.ByteArrayOutputStream()",
            {"OutputStream", "java.io.OutputStream", "Object", "java.lang.Object"},
        ),
        (
            r"new\s+(?:java\.io\.)?OutputStreamWriter\s*\([^;]+?\)",
            "new java.io.OutputStreamWriter(new java.io.ByteArrayOutputStream())",
            {"Writer", "java.io.Writer", "OutputStreamWriter", "java.io.OutputStreamWriter", "Object", "java.lang.Object"},
        ),
        (
            r"new\s+(?:java\.io\.)?FileWriter\s*\([^;]+?\)",
            "new java.io.StringWriter()",
            {"Writer", "java.io.Writer", "Object", "java.lang.Object"},
        ),
        (
            r"new\s+(?:java\.io\.)?BufferedWriter\s*\([^;]+?\)",
            "new java.io.BufferedWriter(new java.io.StringWriter())",
            {"Writer", "java.io.Writer", "BufferedWriter", "java.io.BufferedWriter", "Object", "java.lang.Object"},
        ),
    ]
    for pattern, replacement, assignable_types in replacements:
        match = re.search(pattern, line)
        if match:
            if declared_type and erased_return_type(declared_type) not in assignable_types:
                return None, {
                    "operator_rule": "redirect_declared_type_not_assignable",
                    "declared_type": declared_type,
                }
            return (
                source[: line_start + match.start()] + replacement + source[line_start + match.end() :],
                {
                    "operator_rule": "output_redirected_in_memory",
                    "call_behavior": "replaced",
                    "mutation_dimension": "external_effect",
                    "confounded_dimensions": [],
                },
            )
    return None, {"operator_rule": "not_output_constructor"}


def call_span_on_line(source: str, line_number: int) -> tuple[int, int, int, int, list[str]] | None:
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, line_end = bounds
    line = source[line_start:line_end]
    matches = list(
        re.finditer(
            r"\b(?:Files\.\w+|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+|new\s+[A-Za-z_$][\w$]*)\s*\(",
            line,
        )
    )
    if not matches:
        return None
    match = matches[-1]
    open_paren = line.find("(", match.start())
    close_paren = find_matching_paren(line, open_paren)
    if close_paren is None:
        return None
    args_text = line[open_paren + 1 : close_paren]
    return (
        line_start + match.start(),
        line_start + close_paren + 1,
        line_start + open_paren + 1,
        line_start + close_paren,
        split_java_arguments(args_text),
    )


RISKY_NUMERIC_ARGUMENT_RE = re.compile(r"\b(?:index|idx|offset|limit|length|size|capacity|port|position)\b", re.IGNORECASE)


def local_literal_assignments(source: str, signature: str | None, before_offset: int) -> dict[str, str]:
    span = target_method_span(source, signature)
    if span is None:
        return {}
    method_start, method_end = span
    search_end = min(before_offset, method_end)
    text = source[method_start:search_end]
    assignments: dict[str, str] = {}
    literal = r"(true|false|null|-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?[fFdDlL]?|\"(?:\\.|[^\"\\])*\")"
    for match in re.finditer(
        rf"\b(?:final\s+)?[A-Za-z_$][\w$.<>\[\]?]*\s+([A-Za-z_$][\w$]*)\s*=\s*{literal}\s*;",
        text,
    ):
        assignments[match.group(1)] = match.group(2)
    for match in re.finditer(rf"\b([A-Za-z_$][\w$]*)\s*=\s*{literal}\s*;", text):
        assignments[match.group(1)] = match.group(2)
    return assignments


def altered_argument(arg: str, context_text: str = "") -> tuple[str | None, str | None]:
    stripped = arg.strip()
    if stripped == "true":
        return "false", "boolean_argument_negated"
    if stripped == "false":
        return "true", "boolean_argument_negated"
    if re.fullmatch(r"-?\d+[lL]?", stripped):
        if RISKY_NUMERIC_ARGUMENT_RE.search(context_text):
            return None, "numeric_argument_skipped_crash_prone_context"
        suffix = "L" if stripped.endswith(("l", "L")) else ""
        value = int(stripped.rstrip("lL"))
        return ("1" if value == 0 else "0") + suffix, "numeric_argument_zero_or_one"
    if re.fullmatch(r"-?\d+\.\d+(?:[fFdD])?", stripped):
        if RISKY_NUMERIC_ARGUMENT_RE.search(context_text):
            return None, "numeric_argument_skipped_crash_prone_context"
        suffix = stripped[-1] if stripped[-1] in "fFdD" else ""
        value_text = stripped[:-1] if suffix else stripped
        return ("1.0" if float(value_text) == 0.0 else "0.0") + suffix, "numeric_argument_zero_or_one"
    if re.fullmatch(r'".*"', stripped, flags=re.DOTALL):
        return ('"MUTATED1"' if stripped == '""' else '""'), "string_argument_empty_or_mutated"
    return None, None


def replace_nth_argument(args: list[str], index: int, replacement: str) -> str:
    return ", ".join(replacement if i == index else arg.strip() for i, arg in enumerate(args))


def mutated_argument_source(source: str, item: dict[str, Any], signature: str | None = None) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_call_line", "operator": "interaction", "sub_variant": "argument_alteration"}
    span = call_span_on_line(source, line_number)
    if span is None:
        return None, {"operator_rule": "call_not_found", "operator": "interaction", "sub_variant": "argument_alteration"}
    _, _, args_start, args_end, args = span
    bounds = line_bounds(source, line_number)
    context_text = source[bounds[0] : bounds[1]] if bounds else ""
    parameter_names = signature_parameter_names(signature)
    literal_assignments = local_literal_assignments(source, signature, args_start)
    candidates: list[tuple[int, int, str, str | None]] = []
    for index, arg in enumerate(args):
        stripped = arg.strip()
        derived_from_parameter = any(re.search(rf"\b{re.escape(name)}\b", stripped) for name in parameter_names)
        if re.fullmatch(r"[A-Za-z_$][\w$]*", stripped) and stripped in literal_assignments:
            rank = 0 if derived_from_parameter else 1
            candidates.append((rank, index, stripped, literal_assignments[stripped]))
        elif derived_from_parameter:
            rank = 0
            candidates.append((rank, index, stripped, None))
        else:
            candidates.append((2, index, stripped, None))
    for _, index, arg, resolved_literal in sorted(candidates):
        replacement, rule = altered_argument(resolved_literal or arg, context_text)
        if replacement is not None and replacement != arg.strip():
            mutated_args = replace_nth_argument(args, index, replacement)
            return (
                source[:args_start] + mutated_args + source[args_end:],
                {
                    "operator_rule": rule,
                    "operator": "interaction",
                    "sub_variant": "argument_alteration",
                    "argument_index": index,
                    "original_argument": arg.strip(),
                    "replacement_argument": replacement,
                    "argument_source": "local_literal_constant" if resolved_literal else "direct_argument",
                    "call_behavior": "preserved",
                    "mutation_dimension": "argument",
                    "confounded_dimensions": [],
                },
            )
    return None, {"operator_rule": "no_safe_argument_to_alter", "operator": "interaction", "sub_variant": "argument_alteration"}


def neutral_stub_for_type(type_text: str | None, allow_opaque_null: bool = False) -> dict[str, Any]:
    """Return a type-compatible neutral value for dependency-result substitution."""
    typ = erased_return_type(type_text)
    simple = simple_type_name(type_text)
    if not typ or typ == "void" or typ == "var":
        return {"expression": None, "rule": "neutral_stub_type_unknown"}
    if typ == "boolean":
        return {"expression": "false", "rule": "neutral_boolean_false"}
    if typ in {"byte", "short", "int"}:
        return {"expression": "0", "rule": "neutral_integral_zero"}
    if typ == "long":
        return {"expression": "0L", "rule": "neutral_long_zero"}
    if typ == "float":
        return {"expression": "0.0f", "rule": "neutral_float_zero"}
    if typ == "double":
        return {"expression": "0.0d", "rule": "neutral_double_zero"}
    if typ == "char":
        return {"expression": "'\\0'", "rule": "neutral_char_nul"}
    if typ in {"Boolean"}:
        return {"expression": "false", "rule": "neutral_boxed_boolean_false"}
    if typ in {"Byte", "Short", "Integer"}:
        return {"expression": "0", "rule": "neutral_boxed_integral_zero"}
    if typ == "Long":
        return {"expression": "0L", "rule": "neutral_boxed_long_zero"}
    if typ == "Float":
        return {"expression": "0.0f", "rule": "neutral_boxed_float_zero"}
    if typ == "Double":
        return {"expression": "0.0d", "rule": "neutral_boxed_double_zero"}
    if typ == "Character":
        return {"expression": "'\\0'", "rule": "neutral_boxed_char_nul"}
    if typ in {"String", "java.lang.String", "CharSequence", "java.lang.CharSequence"}:
        return {"expression": '""', "rule": "neutral_empty_string"}
    if typ.endswith("[]"):
        return {"expression": f"new {typ[:-2].strip()}[0]", "rule": "neutral_empty_array"}
    if simple in {"List", "Collection", "Iterable"}:
        return {"expression": "java.util.Collections.emptyList()", "rule": "neutral_empty_list"}
    if simple in {"ArrayList", "LinkedList"}:
        return {"expression": f"new java.util.{simple}<>()", "rule": "neutral_empty_concrete_list"}
    if simple == "Set":
        return {"expression": "java.util.Collections.emptySet()", "rule": "neutral_empty_set"}
    if simple in {"HashSet", "LinkedHashSet", "TreeSet"}:
        return {"expression": f"new java.util.{simple}<>()", "rule": "neutral_empty_concrete_set"}
    if simple == "Map":
        return {"expression": "java.util.Collections.emptyMap()", "rule": "neutral_empty_map"}
    if simple in {"HashMap", "LinkedHashMap", "TreeMap"}:
        return {"expression": f"new java.util.{simple}<>()", "rule": "neutral_empty_concrete_map"}
    if simple == "Optional":
        return {"expression": "java.util.Optional.empty()", "rule": "neutral_empty_optional"}
    if simple in {"InputStream"}:
        return {
            "expression": "new java.io.ByteArrayInputStream(new byte[0])",
            "rule": "neutral_empty_input_stream",
        }
    if simple in {"Reader"}:
        return {"expression": "new java.io.StringReader(\"\")", "rule": "neutral_empty_reader"}
    if simple in {"OutputStream"}:
        return {"expression": "new java.io.ByteArrayOutputStream()", "rule": "neutral_in_memory_output_stream"}
    if simple in {"Writer"}:
        return {"expression": "new java.io.StringWriter()", "rule": "neutral_in_memory_writer"}
    if allow_opaque_null:
        return {
            "expression": "null",
            "rule": "opaque_reference_neutral_null",
            "degenerate_null": True,
        }
    return {"expression": None, "rule": "opaque_reference_neutral_null_disabled"}


def inferred_variable_type(
    source: str,
    variable: str,
    before_offset: int,
    signature: str | None,
) -> str | None:
    parameter_names = signature_parameter_names(signature)
    if variable in parameter_names and signature and "(" in signature and ")" in signature:
        params = signature[signature.find("(") + 1 : signature.rfind(")")]
        declarations = split_java_arguments(params)
        index = parameter_names.index(variable)
        if index < len(declarations):
            declaration = re.sub(r"@\w+(?:\([^)]*\))?\s*|\bfinal\b\s*", "", declarations[index]).strip()
            match = re.match(r"(.+?)\s+[A-Za-z_$][\w$]*(?:\[\])?$", declaration)
            if match:
                return match.group(1).strip()

    prefix = source[:before_offset]
    declaration_re = re.compile(
        rf"(?:^|[;{{}}])\s*(?:final\s+)?([A-Za-z_$][\w$.]*(?:\s*<[^;={{}}]+>)?(?:\[\])?)\s+"
        rf"{re.escape(variable)}\s*(?:=|;)",
        flags=re.MULTILINE,
    )
    matches = list(declaration_re.finditer(prefix))
    return matches[-1].group(1).strip() if matches else None


def assignment_rhs_span(source: str, statement_start: int, statement_end: int) -> tuple[int, int] | None:
    statement = source[statement_start:statement_end]
    match = re.search(r"(?<![=!<>])=(?!=)", statement)
    if not match:
        return None
    rhs_start = statement_start + match.end()
    rhs_end = statement_end
    while rhs_end > rhs_start and source[rhs_end - 1].isspace():
        rhs_end -= 1
    if rhs_end > rhs_start and source[rhs_end - 1] == ";":
        rhs_end -= 1
    return rhs_start, rhs_end


def assignment_declaration_type(statement: str) -> tuple[str | None, str | None, str | None]:
    declaration = re.match(
        r"\s*(?:final\s+)?([A-Za-z_$][\w$.<>\[\]?,\s]*)\s+([A-Za-z_$][\w$]*)\s*=\s*(.*)$",
        statement,
        flags=re.DOTALL,
    )
    if declaration:
        return declaration.group(1), declaration.group(2), declaration.group(3)
    assignment = re.match(r"\s*([A-Za-z_$][\w$]*)\s*=\s*(.*)$", statement, flags=re.DOTALL)
    if assignment:
        return None, assignment.group(1), assignment.group(2)
    return None, None, None


def variable_use_contexts(source: str, start: int, end: int, variable: str) -> list[str]:
    contexts: list[str] = []
    for match in variable_uses(source[start:end], variable):
        absolute = start + match.start()
        statement_start = statement_start_for_offset(source, absolute)
        statement_end = find_statement_end(source, statement_start) or end
        statement = source[statement_start:statement_end].strip()
        if re.fullmatch(rf"return\s+{re.escape(variable)}\s*;", statement):
            contexts.append("return")
        elif re.search(rf"\b(?:this\.)?[A-Za-z_$][\w$]*(?:Result|Response|Request|Payload|Body|Header|Value|Data|Event|Record|Sent|Written|Last)\s*=\s*{re.escape(variable)}\s*;", statement, flags=re.I):
            contexts.append("assertion_visible_field_write")
        elif re.search(rf"\bif\s*\([^)]*\b{re.escape(variable)}\b|\b(?:for|while|switch)\s*\([^)]*\b{re.escape(variable)}\b", statement):
            contexts.append("branch_or_loop")
        else:
            contexts.append("computation")
    return contexts


def is_java_statement_expression(expression: str) -> bool:
    text = expression.strip()
    open_paren = text.find("(")
    if open_paren < 1:
        return False
    prefix = text[:open_paren].strip()
    if not re.fullmatch(r"(?:new\s+)?[A-Za-z_$][\w$.]*(?:\s*<[^>]+>)?", prefix):
        return False
    close_paren = find_matching_paren(text, open_paren)
    if close_paren is None:
        return False
    cursor = close_paren + 1
    while cursor < len(text):
        chained = re.match(r"\s*\.\s*[A-Za-z_$][\w$]*\s*\(", text[cursor:])
        if not chained:
            return False
        next_open = cursor + chained.end() - 1
        next_close = find_matching_paren(text, next_open)
        if next_close is None:
            return False
        cursor = next_close + 1
    return True


def return_call_expression(statement: str) -> str | None:
    match = re.match(r"\s*return\s+(.*?)\s*;\s*$", statement, flags=re.DOTALL)
    return match.group(1).strip() if match else None


def mutated_return_call_neutral_source(
    source: str,
    statement_start: int,
    statement_end: int,
    return_type: str | None,
    preserve_call: bool,
    allow_opaque_null: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    replacement_meta = neutral_stub_for_type(return_type, allow_opaque_null)
    replacement = replacement_meta.get("expression")
    if replacement is None:
        return None, {"operator_rule": "return_stub_no_type_replacement", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    statement = source[statement_start:statement_end]
    call_expression = return_call_expression(statement)
    if call_expression is None:
        return None, {"operator_rule": "not_direct_return_call", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    if preserve_call and not is_java_statement_expression(call_expression):
        return None, {
            "operator_rule": "return_call_not_statement_expression",
            "operator": "interaction",
            "sub_variant": "call_preserved_value_neutralization",
        }
    leading = statement[: len(statement) - len(statement.lstrip())]
    indent = leading.rsplit("\n", 1)[-1]
    rewritten_statement = f"{leading}{call_expression}; return {replacement};" if preserve_call else re.sub(
        r"\breturn\b.*;",
        f"return {replacement};",
        statement,
        count=1,
        flags=re.DOTALL,
    )
    call_behavior = "preserved" if preserve_call else "suppressed"
    return (
        source[:statement_start] + rewritten_statement + source[statement_end:],
        {
            "operator_rule": f"direct_return_call_{call_behavior}_value_neutralized",
            "operator": "interaction",
            "sub_variant": f"call_{call_behavior}_value_neutralization",
            "replacement_expression": replacement,
            "stub_rule": replacement_meta.get("rule"),
            "degenerate_null": bool(replacement_meta.get("degenerate_null")),
            "call_behavior": call_behavior,
            "mutation_dimension": "return_value" if preserve_call else "invocation_and_return_value",
            "confounded_dimensions": [] if preserve_call else ["invocation", "return_value"],
        },
    )


def mutated_result_neutral_source(
    source: str,
    item: dict[str, Any],
    signature: str | None,
    return_type: str | None,
    preserve_call: bool,
    allow_opaque_null: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None, {"operator_rule": "missing_call_line", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    span = call_span_on_line(source, line_number)
    if span is None:
        return None, {"operator_rule": "call_not_found", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    call_start, _, _, _, _ = span
    statement_start = statement_start_for_offset(source, call_start)
    statement_end = find_statement_end(source, statement_start)
    if statement_end is None:
        return None, {"operator_rule": "statement_end_not_found", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    statement = source[statement_start:statement_end]
    if re.match(r"\s*return\b", statement):
        return mutated_return_call_neutral_source(
            source,
            statement_start,
            statement_end,
            return_type,
            preserve_call,
            allow_opaque_null,
        )
    declared_type, variable, _ = assignment_declaration_type(statement)
    if not variable:
        return None, {"operator_rule": "result_not_assigned_or_returned", "operator": "interaction", "sub_variant": "return_value_stubbing"}
    effective_type = declared_type or inferred_variable_type(source, variable, statement_start, signature)
    replacement_meta = neutral_stub_for_type(effective_type, allow_opaque_null)
    replacement = replacement_meta.get("expression")
    if replacement is None:
        return None, {
            "operator_rule": "result_stub_no_type_replacement",
            "operator": "interaction",
            "sub_variant": "return_value_stubbing",
            "defined_variable": variable,
            "inferred_type": effective_type,
        }
    method_span = target_method_span(source, signature)
    method_end = method_span[1] if method_span else len(source)
    contexts = variable_use_contexts(source, statement_end, method_end, variable)
    rhs_span = assignment_rhs_span(source, statement_start, statement_end)
    if rhs_span is None:
        return None, {
            "operator_rule": "assignment_rhs_not_found",
            "operator": "interaction",
            "sub_variant": "return_value_stubbing",
            "defined_variable": variable,
        }
    downstream_flow_risk = any(context in {"branch_or_loop", "computation"} for context in contexts)
    call_behavior = "preserved" if preserve_call else "suppressed"
    rhs_expression = source[rhs_span[0] : rhs_span[1]].strip()
    if preserve_call:
        if not is_java_statement_expression(rhs_expression):
            return None, {
                "operator_rule": "assignment_rhs_not_statement_expression",
                "operator": "interaction",
                "sub_variant": "call_preserved_value_neutralization",
                "defined_variable": variable,
                "inferred_type": effective_type,
            }
        stripped_statement = statement.strip()
        assignment_match = re.search(r"(?<![=!<>])=(?!=)", stripped_statement)
        if not assignment_match:
            return None, {
                "operator_rule": "assignment_operator_not_found",
                "operator": "interaction",
                "sub_variant": "call_preserved_value_neutralization",
            }
        assignment_prefix = stripped_statement[: assignment_match.end()].rstrip()
        leading = statement[: len(statement) - len(statement.lstrip())]
        indent = leading.rsplit("\n", 1)[-1]
        rewritten_statement = f"{leading}{rhs_expression}; {assignment_prefix} {replacement};"
        mutated_source = source[:statement_start] + rewritten_statement + source[statement_end:]
    else:
        mutated_source = source[: rhs_span[0]] + " " + replacement + source[rhs_span[1] :]
    return (
        mutated_source,
        {
            "operator_rule": f"assigned_result_call_{call_behavior}_value_neutralized",
            "operator": "interaction",
            "sub_variant": f"call_{call_behavior}_value_neutralization",
            "defined_variable": variable,
            "inferred_type": effective_type,
            "replacement_expression": replacement,
            "stub_rule": replacement_meta.get("rule"),
            "degenerate_null": bool(replacement_meta.get("degenerate_null")),
            "use_contexts": sorted(set(contexts)) or ["unused"],
            "downstream_flow_risk": downstream_flow_risk,
            "whole_rhs_substitution": True,
            "call_behavior": call_behavior,
            "mutation_dimension": "return_value" if preserve_call else "invocation_and_return_value",
            "confounded_dimensions": [] if preserve_call else ["invocation", "return_value"],
        },
    )


def mutated_result_call_preserved_source(
    source: str,
    item: dict[str, Any],
    signature: str | None,
    return_type: str | None,
    allow_opaque_null: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    return mutated_result_neutral_source(
        source,
        item,
        signature,
        return_type,
        preserve_call=True,
        allow_opaque_null=allow_opaque_null,
    )


def mutated_result_call_suppressed_source(
    source: str,
    item: dict[str, Any],
    signature: str | None,
    return_type: str | None,
    allow_opaque_null: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    return mutated_result_neutral_source(
        source,
        item,
        signature,
        return_type,
        preserve_call=False,
        allow_opaque_null=allow_opaque_null,
    )


def assertion_mutation_items(source: str, signature: str | None) -> list[dict[str, Any]]:
    if is_private_method_signature(signature):
        return []
    obligation_items = source_obligation_items(source, signature)
    items: list[dict[str, Any]] = []
    for item in return_obligation_items(source, signature):
        items.append({**item, "construct": "return_behavior", "signal": "return_behavior"})
    for item in obligation_items.get("exception_path", []):
        items.append({**item, "construct": "exception_behavior", "signal": "exception_behavior"})
    for item in obligation_items.get("interaction_dependency", []):
        signal = "side_effect_or_dependency" if is_assertable_external_side_effect_item(item) else "interaction_dependency"
        construct = signal
        items.append({**item, "construct": construct, "signal": signal})

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str]] = set()
    for item in items:
        key = (str(item.get("construct") or ""), item.get("line"), str(item.get("snippet") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def mutant_sources_for_item(
    source: str,
    item: dict[str, Any],
    return_type: str | None,
    signature: str | None,
    same_package_test: bool = False,
    allow_opaque_null_interaction: bool = False,
    allow_opaque_null_side_effect_sensitivity: bool = False,
    include_confounded_side_effect_neutralization: bool = False,
) -> list[tuple[str, str | None, dict[str, Any]]]:
    construct = str(item.get("construct") or "")
    if construct == "return_behavior":
        mutated, meta = mutated_return_source(source, item, return_type, same_package_test)
        return [("return_value_changed", mutated, meta)]
    if construct == "exception_behavior":
        mutated, meta = mutated_exception_type_source(source, item, signature)
        return [("exception_type_changed", mutated, meta)]
    if construct == "side_effect_or_dependency":
        constructor_mutant, constructor_meta = mutated_output_constructor_source(source, item, signature)
        if constructor_mutant is not None:
            return [("output_redirected_in_memory", constructor_mutant, constructor_meta)]
        redirected, redirect_meta = mutated_result_call_suppressed_source(
            source,
            item,
            signature,
            return_type,
            False,
        )
        if redirected is not None and redirect_meta.get("stub_rule") in {
            "neutral_in_memory_output_stream",
            "neutral_in_memory_writer",
        }:
            return [
                (
                    "output_redirected_in_memory",
                    redirected,
                    {
                        **redirect_meta,
                        "operator": "side_effect",
                        "sub_variant": "output_sink_redirect",
                        "call_behavior": "redirected",
                        "mutation_dimension": "external_effect_destination",
                        "confounded_dimensions": [],
                        "sensitivity_only": False,
                    },
                )
            ]
        if include_confounded_side_effect_neutralization:
            suppressed, suppressed_meta = mutated_result_call_suppressed_source(
                source,
                item,
                signature,
                return_type,
                allow_opaque_null_side_effect_sensitivity,
            )
            if suppressed is not None:
                return [
                    (
                        "confounded_call_suppressed_value_neutralized",
                        suppressed,
                        {**suppressed_meta, "sensitivity_only": True},
                    )
                ]
        deleted, delete_meta = mutated_statement_removed_source(source, item, signature)
        if deleted is None and constructor_meta.get("operator_rule") not in {"not_output_constructor", "missing_output_constructor_line", "output_constructor_line_not_found"}:
            delete_meta = {
                **delete_meta,
                "fallback_after_redirect_skip": constructor_meta.get("operator_rule"),
            }
        return [("statement_removed", deleted, delete_meta)]
    if construct == "interaction_dependency":
        mutants: list[tuple[str, str | None, dict[str, Any]]] = []
        altered, alter_meta = mutated_argument_source(source, item, signature)
        mutants.append(("argument_altered", altered, alter_meta))
        preserved, preserved_meta = mutated_result_call_preserved_source(
            source, item, signature, return_type, allow_opaque_null_interaction
        )
        mutants.append(("call_preserved_value_neutralized", preserved, preserved_meta))
        suppressed, suppressed_meta = mutated_result_call_suppressed_source(
            source, item, signature, return_type, allow_opaque_null_interaction
        )
        mutants.append(("call_suppressed_value_neutralized", suppressed, suppressed_meta))
        deleted, meta = mutated_statement_removed_source(source, item, signature)
        mutants.append(("call_suppressed_statement_removed", deleted, meta))
        return mutants
    return []


def status_for_unwritten_mutant(meta: dict[str, Any]) -> str:
    rule = str(meta.get("operator_rule") or "")
    if rule in {"replacement_constructor_incompatible", "replacement_not_assignable_to_declared_type"}:
        return "not_generated"
    if "out_of_scope" in rule or "flows_to_computation_or_branch" in rule or "hazard" in rule or "not_assignable" in rule:
        return "out_of_scope"
    return "not_generated"


def mutant_tags(mutant_meta: dict[str, Any], result_status: str) -> list[str]:
    tags: list[str] = []
    if mutant_meta.get("degenerate_null"):
        tags.append("degenerate_null")
    if mutant_meta.get("unobservable_return_state"):
        tags.append("unobservable_return_state")
    if mutant_meta.get("subsumed_by_broad_original"):
        tags.append("subsumed_by_broad_original")
    if mutant_meta.get("downstream_flow_risk"):
        tags.append("downstream_flow_risk")
    if result_status == "survived_path_changed":
        tags.append("path_coverage_changed")
    if result_status == "coverage_unverified":
        tags.append("coverage_unverified")
    return tags


def dominant_nonrepresented_outcome(statuses: list[str]) -> str:
    for status in ["coverage_unverified", "out_of_scope", "compile_error", "timeout", "command_missing", "not_generated"]:
        if status in statuses:
            return status
    return "not_generated"


def mutated_line_text(mutated_source: str | None, line_number: Any) -> str | None:
    if mutated_source is None or not isinstance(line_number, int):
        return None
    lines = mutated_source.splitlines()
    if line_number < 1 or line_number > len(lines):
        return None
    return lines[line_number - 1].strip()[:500]


def coverage_imprecise_line(source: str, line_number: Any) -> bool:
    if not isinstance(line_number, int):
        return False
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return False
    line = source[bounds[0] : bounds[1]]
    call_count = len(re.findall(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\(", line))
    return "?" in line or call_count > 1


def variant_score(status: str, incidental_score: str) -> float | None:
    if status == "killed_by_oracle":
        return 1.0
    if status == "survived":
        return 0.0
    if status == "killed_incidentally":
        return None if incidental_score == "exclude" else 0.0
    if status == "survived_path_changed":
        return None
    return None


def aggregate_variant_scores(mutants: list[dict[str, Any]], incidental_score: str) -> dict[str, Any]:
    represented_mutants = [
        mutant
        for mutant in mutants
        if mutant.get("outcome") in REPRESENTED_STATUSES
    ]
    primary_mutants = [
        mutant
        for mutant in represented_mutants
        if not mutant.get("sensitivity_only")
    ]
    scores = [
        variant_score(str(mutant.get("outcome") or ""), incidental_score)
        for mutant in primary_mutants
    ]
    scores = [score for score in scores if score is not None]
    any_killed = any(mutant.get("outcome") == "killed_by_oracle" for mutant in primary_mutants)
    all_killed = bool(primary_mutants) and all(mutant.get("outcome") == "killed_by_oracle" for mutant in primary_mutants)
    represented_any_killed = any(
        mutant.get("outcome") == "killed_by_oracle" for mutant in represented_mutants
    )
    mean_score = sum(scores) / len(scores) if scores else None
    if represented_any_killed:
        outcome = "killed_by_oracle"
    elif any(mutant.get("outcome") == "killed_incidentally" for mutant in represented_mutants):
        outcome = "killed_incidentally"
    elif any(mutant.get("outcome") == "survived_path_changed" for mutant in represented_mutants):
        outcome = "survived_path_changed"
    elif any(mutant.get("outcome") == "survived" for mutant in represented_mutants):
        outcome = "survived"
    else:
        outcome = None
    return {
        "score": mean_score,
        "score_any_killed": 1.0 if any_killed else (0.0 if primary_mutants else None),
        "score_all_killed": 1.0 if all_killed else (0.0 if primary_mutants else None),
        "represented_variant_count": len(represented_mutants),
        "primary_variant_count": len(primary_mutants),
        "scored_variant_count": len(scores),
        "outcome": outcome,
    }


def aggregate_scored_items(scored_items: list[dict[str, Any]], status_counts: Counter[str] | None = None) -> dict[str, Any]:
    status_counts = Counter(status_counts or {})
    by_construct: dict[str, Counter[str]] = defaultdict(Counter)
    by_return_subcategory: dict[str, Counter[str]] = defaultdict(Counter)
    by_return_object_rule: dict[str, Counter[str]] = defaultdict(Counter)
    by_operator: dict[str, Counter[str]] = defaultdict(Counter)
    mutant_pre_execution_status_counts: Counter[str] = Counter()
    mutant_execution_outcome_counts: Counter[str] = Counter()
    for item in scored_items:
        construct = str(item.get("construct") or "unknown")
        represented = bool(item.get("represented"))
        score = item.get("score")
        by_construct[construct]["total"] += 1
        status = str(item.get("status") or "")
        outcome = str(item.get("outcome") or "")
        effective = outcome or status
        if status:
            by_construct[construct][f"{status}_items"] += 1
        if outcome:
            by_construct[construct][f"{outcome}_items"] += 1
        if status in NON_REPRESENTED_STATUSES or outcome in NON_REPRESENTED_STATUSES:
            by_construct[construct]["non_representable_items"] += 1
        if represented:
            by_construct[construct]["represented"] += 1
        if score is not None:
            by_construct[construct]["scorable"] += 1
        if score is not None:
            by_construct[construct]["score_sum"] += float(score)
            if float(score) > 0:
                by_construct[construct]["checked"] += 1
        if any(mutant.get("degenerate_null") for mutant in item.get("mutants") or []):
            by_construct[construct]["degenerate_null_items"] += 1
        if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or []):
            by_construct[construct]["unobservable_return_state_items"] += 1
        if any(mutant.get("subsumed_by_broad_original") for mutant in item.get("mutants") or []):
            by_construct[construct]["subsumed_by_broad_original_items"] += 1
        if any(mutant.get("downstream_flow_risk") for mutant in item.get("mutants") or []):
            by_construct[construct]["downstream_flow_risk_items"] += 1
        if construct == "return_behavior":
            return_subcategory = None
            for mutant in item.get("mutants") or []:
                return_subcategory = mutant.get("return_subcategory") or return_subcategory_for_operator_rule(mutant.get("operator_rule"))
                if return_subcategory:
                    break
            if return_subcategory:
                sub_stats = by_return_subcategory[str(return_subcategory)]
                sub_stats["total"] += 1
                if status:
                    sub_stats[f"{status}_items"] += 1
                if outcome:
                    sub_stats[f"{outcome}_items"] += 1
                if status in NON_REPRESENTED_STATUSES or outcome in NON_REPRESENTED_STATUSES:
                    sub_stats["non_representable_items"] += 1
                if represented:
                    sub_stats["represented"] += 1
                if score is not None:
                    sub_stats["scorable"] += 1
                    sub_stats["score_sum"] += float(score)
                    if float(score) > 0:
                        sub_stats["checked"] += 1
                if any(mutant.get("degenerate_null") for mutant in item.get("mutants") or []):
                    sub_stats["degenerate_null_items"] += 1
                if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or []):
                    sub_stats["unobservable_return_state_items"] += 1
            object_rules = [
                str(mutant.get("operator_rule") or "")
                for mutant in item.get("mutants") or []
                if return_subcategory_for_operator_rule(mutant.get("operator_rule")) == "object"
            ]
            if object_rules:
                object_rule = ";".join(sorted(set(object_rules)))
                object_stats = by_return_object_rule[object_rule]
                object_stats["total"] += 1
                if status:
                    object_stats[f"{status}_items"] += 1
                if outcome:
                    object_stats[f"{outcome}_items"] += 1
                if status in NON_REPRESENTED_STATUSES or outcome in NON_REPRESENTED_STATUSES:
                    object_stats["non_representable_items"] += 1
                if represented:
                    object_stats["represented"] += 1
                if score is not None:
                    object_stats["scorable"] += 1
                    object_stats["score_sum"] += float(score)
                    if float(score) > 0:
                        object_stats["checked"] += 1
                if outcome == "killed_incidentally":
                    object_stats["incidental"] += 1
                if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or []):
                    object_stats["unobservable_return_state_items"] += 1
                observability_signals = sorted(
                    {
                        str(mutant.get("observability_signal"))
                        for mutant in item.get("mutants") or []
                        if mutant.get("observability_signal")
                    }
                )
                for signal in observability_signals:
                    object_stats[f"observability_signal_{signal}"] += 1
        for variant in item.get("mutant_variants") or []:
            status = item.get(f"{variant}_mutant_status")
            if status:
                by_construct[construct][f"{status}_mutants"] += 1
        for mutant in item.get("mutants") or []:
            operator_key = f"{mutant.get('operator') or 'unknown'}:{mutant.get('sub_variant') or mutant.get('variant') or 'unknown'}"
            status = str(mutant.get("status") or "unknown")
            execution_outcome = str(mutant.get("outcome") or "")
            outcome = execution_outcome or status
            if mutant.get("degenerate_null"):
                by_construct[construct]["degenerate_null_mutants"] += 1
            if mutant.get("unobservable_return_state"):
                by_construct[construct]["unobservable_return_state_mutants"] += 1
            if mutant.get("subsumed_by_broad_original"):
                by_construct[construct]["subsumed_by_broad_original_mutants"] += 1
            if mutant.get("downstream_flow_risk"):
                by_construct[construct]["downstream_flow_risk_mutants"] += 1
            by_operator[operator_key]["total"] += 1
            by_operator[operator_key][f"status_{status}"] += 1
            mutant_pre_execution_status_counts[status] += 1
            if execution_outcome:
                by_operator[operator_key][f"outcome_{execution_outcome}"] += 1
                mutant_execution_outcome_counts[execution_outcome] += 1
            by_operator[operator_key][outcome] += 1
            for tag in mutant.get("tags") or []:
                by_operator[operator_key][f"tag_{tag}"] += 1

    scorable_items = sum(1 for item in scored_items if item.get("score") is not None)
    represented_items = sum(1 for item in scored_items if item.get("represented"))
    score_sum = sum(float(item.get("score") or 0) for item in scored_items if item.get("score") is not None)
    by_construct_out = {
        construct: {
            "total": int(stats["total"]),
            "applicable": int(stats["total"]),
            "reached": int(stats["total"]) - int(stats["not_reached_items"]),
            "scorable": int(stats["scorable"]),
            "represented": int(stats["represented"]),
            "checked": int(stats["checked"]),
            "score_sum": float(stats["score_sum"]),
            "score": float(stats["score_sum"]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "generation_rate": int(stats["represented"]) / (int(stats["total"]) - int(stats["not_reached_items"])) if (int(stats["total"]) - int(stats["not_reached_items"])) else None,
            "killed_mutants": int(stats["killed_mutants"]),
            "killed_by_oracle_items": int(stats["killed_by_oracle_items"]),
            "killed_incidentally_items": int(stats["killed_incidentally_items"]),
            "survived_items": int(stats["survived_items"]),
            "survived_path_changed_items": int(stats["survived_path_changed_items"]),
            "coverage_unverified_items": int(stats["coverage_unverified_items"]),
            "degenerate_null_items": int(stats["degenerate_null_items"]),
            "unobservable_return_state_items": int(stats["unobservable_return_state_items"]),
            "subsumed_by_broad_original_items": int(stats["subsumed_by_broad_original_items"]),
            "downstream_flow_risk_items": int(stats["downstream_flow_risk_items"]),
            "not_reached_items": int(stats["not_reached_items"]),
            "non_representable_items": int(stats["non_representable_items"]),
            "compile_error_items": int(stats["compile_error_items"]),
            "timeout_items": int(stats["timeout_items"]),
            "not_generated_items": int(stats["not_generated_items"]),
            "out_of_scope_items": int(stats["out_of_scope_items"]),
            "degenerate_null_mutants": int(stats["degenerate_null_mutants"]),
            "unobservable_return_state_mutants": int(stats["unobservable_return_state_mutants"]),
            "subsumed_by_broad_original_mutants": int(stats["subsumed_by_broad_original_mutants"]),
            "downstream_flow_risk_mutants": int(stats["downstream_flow_risk_mutants"]),
            "killed_by_oracle_mutants": int(stats["killed_by_oracle_mutants"]),
            "killed_incidentally_mutants": int(stats["killed_incidentally_mutants"]),
            "survived_mutants": int(stats["survived_mutants"]),
            "survived_path_changed_mutants": int(stats["survived_path_changed_mutants"]),
            "coverage_unverified_mutants": int(stats["coverage_unverified_mutants"]),
            "compile_error_mutants": int(stats["compile_error_mutants"]),
            "timeout_mutants": int(stats["timeout_mutants"]),
            "not_generated_mutants": int(stats["not_generated_mutants"]),
        }
        for construct, stats in sorted(by_construct.items())
    }
    by_return_subcategory_out = {
        subcategory: {
            "total": int(stats["total"]),
            "applicable": int(stats["total"]),
            "reached": int(stats["total"]) - int(stats["not_reached_items"]),
            "scorable": int(stats["scorable"]),
            "represented": int(stats["represented"]),
            "checked": int(stats["checked"]),
            "score_sum": float(stats["score_sum"]),
            "score": float(stats["score_sum"]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "generation_rate": int(stats["represented"]) / (int(stats["total"]) - int(stats["not_reached_items"])) if (int(stats["total"]) - int(stats["not_reached_items"])) else None,
            "not_reached_items": int(stats["not_reached_items"]),
            "non_representable_items": int(stats["non_representable_items"]),
            "compile_error_items": int(stats["compile_error_items"]),
            "timeout_items": int(stats["timeout_items"]),
            "not_generated_items": int(stats["not_generated_items"]),
            "out_of_scope_items": int(stats["out_of_scope_items"]),
            "killed_by_oracle_items": int(stats["killed_by_oracle_items"]),
            "killed_incidentally_items": int(stats["killed_incidentally_items"]),
            "survived_items": int(stats["survived_items"]),
            "survived_path_changed_items": int(stats["survived_path_changed_items"]),
            "coverage_unverified_items": int(stats["coverage_unverified_items"]),
            "degenerate_null_items": int(stats["degenerate_null_items"]),
            "unobservable_return_state_items": int(stats["unobservable_return_state_items"]),
        }
        for subcategory, stats in sorted(by_return_subcategory.items())
    }
    by_return_object_rule_out = {
        rule: {
            "total": int(stats["total"]),
            "applicable": int(stats["total"]),
            "reached": int(stats["total"]) - int(stats["not_reached_items"]),
            "scorable": int(stats["scorable"]),
            "represented": int(stats["represented"]),
            "checked": int(stats["checked"]),
            "score_sum": float(stats["score_sum"]),
            "score": float(stats["score_sum"]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "generation_rate": int(stats["represented"]) / (int(stats["total"]) - int(stats["not_reached_items"])) if (int(stats["total"]) - int(stats["not_reached_items"])) else None,
            "representability": int(stats["represented"]) / (int(stats["total"]) - int(stats["not_reached_items"])) if (int(stats["total"]) - int(stats["not_reached_items"])) else None,
            "incidental": int(stats["incidental"]),
            "incidental_rate": int(stats["incidental"]) / int(stats["scorable"]) if int(stats["scorable"]) else None,
            "unobservable_return_state_items": int(stats["unobservable_return_state_items"]),
            "observability_signals": {
                key.removeprefix("observability_signal_"): int(value)
                for key, value in stats.items()
                if key.startswith("observability_signal_")
            },
            "not_reached_items": int(stats["not_reached_items"]),
            "non_representable_items": int(stats["non_representable_items"]),
            "compile_error_items": int(stats["compile_error_items"]),
            "timeout_items": int(stats["timeout_items"]),
            "not_generated_items": int(stats["not_generated_items"]),
            "out_of_scope_items": int(stats["out_of_scope_items"]),
            "killed_by_oracle_items": int(stats["killed_by_oracle_items"]),
            "killed_incidentally_items": int(stats["killed_incidentally_items"]),
            "survived_items": int(stats["survived_items"]),
            "survived_path_changed_items": int(stats["survived_path_changed_items"]),
            "coverage_unverified_items": int(stats["coverage_unverified_items"]),
        }
        for rule, stats in sorted(by_return_object_rule.items())
    }
    total = len(scored_items)
    reached_items = sum(1 for item in scored_items if item.get("status") != "not_reached")
    not_reached_items = sum(1 for item in scored_items if item.get("status") == "not_reached")
    non_representable_items = sum(
        1
        for item in scored_items
        if item.get("status") in NON_REPRESENTED_STATUSES
        or item.get("outcome") in NON_REPRESENTED_STATUSES
    )
    pessimistic_denominator = scorable_items + sum(
        1 for item in scored_items if item.get("status") in {"not_generated", "out_of_scope"}
    )
    any_killed_scores = [
        float(item["score_any_killed"])
        for item in scored_items
        if item.get("score_any_killed") is not None
    ]
    all_killed_scores = [
        float(item["score_all_killed"])
        for item in scored_items
        if item.get("score_all_killed") is not None
    ]
    primary_constructs = {"return_behavior", "exception_behavior"}
    # Future work: side-effect adequacy metric.
    side_effect_constructs = {"side_effect_or_dependency", "interaction_dependency"}
    primary_items = [item for item in scored_items if item.get("construct") in primary_constructs]
    side_effect_items = [item for item in scored_items if item.get("construct") in side_effect_constructs]

    def has_broad_original_tag(item: dict[str, Any]) -> bool:
        return any(mutant.get("subsumed_by_broad_original") for mutant in item.get("mutants") or [])

    def scoped_counts(scope_items: list[dict[str, Any]], exclude_broad_original: bool = False) -> dict[str, Any]:
        if exclude_broad_original:
            scope_items = [item for item in scope_items if not has_broad_original_tag(item)]
        scoped_scorable = sum(1 for item in scope_items if item.get("score") is not None)
        scoped_score_sum = sum(float(item.get("score") or 0) for item in scope_items if item.get("score") is not None)
        scoped_reached = sum(1 for item in scope_items if item.get("status") != "not_reached")
        scoped_represented = sum(1 for item in scope_items if item.get("represented"))
        scoped_pessimistic_denominator = scoped_scorable + sum(
            1 for item in scope_items if item.get("status") in {"not_generated", "out_of_scope"}
        )
        return {
            "total_items": len(scope_items),
            "reached_items": scoped_reached,
            "not_reached_items": sum(1 for item in scope_items if item.get("status") == "not_reached"),
            "represented_items": scoped_represented,
            "scorable_items": scoped_scorable,
            "score_sum": scoped_score_sum,
            "score": scoped_score_sum / scoped_scorable if scoped_scorable else None,
            "pessimistic_score": scoped_score_sum / scoped_pessimistic_denominator if scoped_pessimistic_denominator else None,
            "pessimistic_denominator": scoped_pessimistic_denominator,
            "checked_items": sum(1 for item in scope_items if item.get("score") is not None and float(item["score"]) > 0),
            "oracle_killed_items": sum(1 for item in scope_items if item.get("outcome") == "killed_by_oracle"),
            "incidentally_killed_items": sum(1 for item in scope_items if item.get("outcome") == "killed_incidentally"),
            "survived_items": sum(1 for item in scope_items if item.get("outcome") == "survived"),
            "survived_path_changed_items": sum(1 for item in scope_items if item.get("outcome") == "survived_path_changed"),
            "coverage_unverified_items": sum(1 for item in scope_items if item.get("status") == "coverage_unverified"),
            "non_representable_items": sum(
                1
                for item in scope_items
                if item.get("status") in NON_REPRESENTED_STATUSES
                or item.get("outcome") in NON_REPRESENTED_STATUSES
            ),
            "subsumed_by_broad_original_items": sum(1 for item in scope_items if has_broad_original_tag(item)),
            "unobservable_return_state_items": sum(
                1 for item in scope_items if any(mutant.get("unobservable_return_state") for mutant in item.get("mutants") or [])
            ),
            "representability_rate": scoped_represented / scoped_reached if scoped_reached else None,
            "scorable_rate": scoped_scorable / scoped_reached if scoped_reached else None,
        }

    primary_scope = scoped_counts(primary_items)
    side_effect_scope = scoped_counts(side_effect_items)
    exception_scope = scoped_counts([item for item in scored_items if item.get("construct") == "exception_behavior"])
    exception_scope_excluding_broad = scoped_counts(
        [item for item in scored_items if item.get("construct") == "exception_behavior"],
        exclude_broad_original=True,
    )
    return {
        "status": "passed",
        "collector_version": ASSERTION_MUTATION_VERSION,
        "reason": None,
        "total_items": total,
        "reached_items": reached_items,
        "not_reached_items": not_reached_items,
        "scorable_items": scorable_items,
        "non_representable_items": non_representable_items,
        "attempted_mutants": sum(len(item.get("mutant_variants") or []) for item in scored_items),
        "score_sum": primary_scope["score_sum"],
        "score": primary_scope["score"],
        "pessimistic_score": primary_scope["pessimistic_score"],
        "pessimistic_denominator": primary_scope["pessimistic_denominator"],
        "legacy_all_construct_score_sum": score_sum,
        "legacy_all_construct_score": score_sum / scorable_items if scorable_items else None,
        "legacy_all_construct_pessimistic_score": score_sum / pessimistic_denominator if pessimistic_denominator else None,
        "score_any_killed": sum(any_killed_scores) / len(any_killed_scores) if any_killed_scores else None,
        "score_all_killed": sum(all_killed_scores) / len(all_killed_scores) if all_killed_scores else None,
        "checked_items": primary_scope["checked_items"],
        "oracle_killed_items": primary_scope["oracle_killed_items"],
        "incidentally_killed_items": primary_scope["incidentally_killed_items"],
        "survived_items": primary_scope["survived_items"],
        "survived_path_changed_items": primary_scope["survived_path_changed_items"],
        "coverage_unverified_items": primary_scope["coverage_unverified_items"],
        "represented_items": represented_items,
        "downstream_flow_risk_items": sum(
            1
            for item in scored_items
            if any(mutant.get("downstream_flow_risk") for mutant in item.get("mutants") or [])
        ),
        "representability_rate": represented_items / reached_items if reached_items else None,
        "scorable_rate": scorable_items / reached_items if reached_items else None,
        "primary_assertion_scope": primary_scope,
        "exception_behavior_scope": exception_scope,
        "exception_behavior_excluding_broad_original_scope": exception_scope_excluding_broad,
        "side_effect_research_scope": side_effect_scope,
        "status_counts": dict(status_counts),
        "mutant_pre_execution_status_counts": dict(mutant_pre_execution_status_counts),
        "mutant_execution_outcome_counts": dict(mutant_execution_outcome_counts),
        "by_construct": by_construct_out,
        "return_behavior_by_subcategory": by_return_subcategory_out,
        "return_behavior_object_by_rule": by_return_object_rule_out,
        "by_operator": {
            operator: {
                **dict(counts),
                "generation_rate": (
                    sum(int(counts.get(status) or 0) for status in REPRESENTED_STATUSES)
                    / int(counts.get("total") or 0)
                    if int(counts.get("total") or 0)
                    else None
                ),
            }
            for operator, counts in sorted(by_operator.items())
        },
        "not_generated_by_operator": dict(
            Counter(
                str(item.get("not_generated_operator") or "unknown")
                for item in scored_items
                if item.get("status") in NON_REPRESENTED_STATUSES and item.get("not_generated_operator")
            )
        ),
        "not_generated_by_operator_rule": dict(
            Counter(
                str(item.get("not_generated_operator_rule") or "unknown")
                for item in scored_items
                if item.get("status") in NON_REPRESENTED_STATUSES and item.get("not_generated_operator_rule")
            )
        ),
        "not_generated_by_return_type_category": dict(
            Counter(
                str(item.get("return_type_category") or "unknown")
                for item in scored_items
                if item.get("status") in NON_REPRESENTED_STATUSES and item.get("construct") == "return_behavior"
            )
        ),
        "degenerate_null_items": sum(1 for item in scored_items if any(mutant.get("degenerate_null") for mutant in item.get("mutants") or [])),
        "items": scored_items,
    }


def assertion_item_key(item: dict[str, Any]) -> tuple[str, int | None, str]:
    line = item.get("line")
    return (
        str(item.get("construct") or ""),
        line if isinstance(line, int) else None,
        str(item.get("snippet") or "").strip(),
    )


def selection_row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("model_id") or ""),
        str(row.get("task_id") or ""),
        str(row.get("prompt_template") or ""),
    )


def selected_assertion_items(
    path: Path,
    statuses: set[str],
) -> dict[tuple[str, str, str], dict[tuple[str, int | None, str], dict[str, Any]]]:
    selected: dict[tuple[str, str, str], dict[tuple[str, int | None, str], dict[str, Any]]] = defaultdict(dict)
    for row in read_jsonl(path):
        items = row.get("assertion_strength_items")
        if items is None:
            items = (row.get("assertion_mutation") or {}).get("items") or []
        for item in items or []:
            if str(item.get("status") or "") in statuses:
                selected[selection_row_key(row)][assertion_item_key(item)] = item
    return dict(selected)


def collect_row(
    row: dict[str, Any],
    timeout: int,
    mvn: str,
    max_items: int | None = None,
    only_constructs: set[str] | None = None,
    incidental_score: str = "zero",
    degenerate_null_score: str = "score",
    selected_item_keys: set[tuple[str, int | None, str]] | dict[tuple[str, int | None, str], dict[str, Any]] | None = None,
    only_mutant_variants: set[str] | None = None,
    allow_opaque_null_interaction: bool = False,
    allow_opaque_null_side_effect_sensitivity: bool = False,
    include_confounded_side_effect_neutralization: bool = False,
) -> dict[str, Any]:
    test_class = panta_test_class(row)
    if not row.get("execution_passed"):
        return {
            "status": "skipped",
            "collector_version": ASSERTION_MUTATION_VERSION,
            "reason": "original_generated_test_not_executed",
            "total_items": 0,
            "score": None,
            "items": [],
        }
    signature = (row.get("task") or {}).get("signature") or row.get("signature")
    if is_private_method_signature(signature):
        return {
            "status": "skipped",
            "collector_version": ASSERTION_MUTATION_VERSION,
            "reason": "private_target_method",
            "total_items": 0,
            "score": None,
            "items": [],
        }
    workdir = Path(row.get("workdir") or "")
    if not workdir.is_absolute():
        workdir = ROOT / workdir
    source_path = workdir / "src/main/java/benchmark/Subject.java"
    if not source_path.exists():
        return {
            "status": "skipped",
            "collector_version": ASSERTION_MUTATION_VERSION,
            "reason": "missing_subject_java",
            "total_items": 0,
            "score": None,
            "items": [],
        }

    original_source = source_path.read_text(encoding="utf-8", errors="replace")
    return_type = method_return_type(signature)
    return_category = return_type_category(return_type)
    same_package_test = generated_test_same_package(workdir, original_source, test_class)
    items = assertion_mutation_items(original_source, signature)
    selected_item_records: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    if selected_item_keys is not None:
        if isinstance(selected_item_keys, dict):
            selected_item_records = selected_item_keys
            selected_key_set = set(selected_item_keys)
        else:
            selected_key_set = set(selected_item_keys)
        items = [item for item in items if assertion_item_key(item) in selected_key_set]
    if only_constructs:
        items = [item for item in items if str(item.get("construct") or "") in only_constructs]
    if max_items is not None:
        items = items[:max_items]

    prompt_name = str(row.get("prompt_template") or "unknown")
    reusable_coverage = bool(selected_item_records) and all(
        "covered_by_original_test" in prior for prior in selected_item_records.values()
    )
    if reusable_coverage:
        coverage = {
            "status": "passed",
            "stage": "selected_prior_item_coverage",
            "covered_lines": sorted(
                {
                    int(prior["line"])
                    for prior in selected_item_records.values()
                    if prior.get("covered_by_original_test") and isinstance(prior.get("line"), int)
                }
            ),
        }
    else:
        coverage = collect_subject_line_coverage(workdir, prompt_name, timeout, mvn, test_class)
    covered_lines = set(coverage.get("covered_lines") or [])
    coverage_available = coverage.get("status") == "passed" and (reusable_coverage or bool(covered_lines))
    method_span = target_method_span(original_source, signature)
    if method_span:
        offsets = line_start_offsets(original_source)
        method_lines = set(range(offset_to_line(offsets, method_span[0]), offset_to_line(offsets, method_span[1] - 1) + 1))
    else:
        method_lines = set()
    scored_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    pre_execution_status_counts: Counter[str] = Counter()
    execution_outcome_counts: Counter[str] = Counter()
    for index, item in enumerate(items, start=1):
            construct = str(item.get("construct") or "unknown")
            item_id = f"{construct}-{index:04d}-line-{item.get('line')}"
            item_result: dict[str, Any] = {
                "index": index,
                "line": item.get("line"),
                "construct": construct,
                "signal": item.get("signal"),
                "snippet": item.get("snippet"),
                "method_return_type": return_type,
                "return_type_category": return_category if construct == "return_behavior" else None,
                "same_package_test": same_package_test if construct == "return_behavior" else None,
                "coverage_status": coverage.get("status"),
                "coverage_stage": coverage.get("stage"),
                "coverage_scope": "per_generated_suite",
                "covered_by_original_test": None,
                "coverage_imprecise": coverage_imprecise_line(original_source, item.get("line")),
                "mutant_variants": [],
                "mutants": [],
                "artifact_class": item.get("artifact_class") or row.get("artifact_class") or row.get("human_artifact_class"),
                "status": "planned",
                "outcome": None,
            }
            line = item.get("line")
            if coverage_available and isinstance(line, int):
                item_result["covered_by_original_test"] = line in covered_lines
                if line not in covered_lines:
                    item_result["represented"] = False
                    item_result["score"] = None
                    item_result["status"] = "not_reached"
                    item_result["outcome"] = None
                    item_result["score_reason"] = "obligation_line_not_covered_by_original_test"
                    planned_mutants = mutant_sources_for_item(
                        original_source,
                        item,
                        return_type,
                        signature,
                        same_package_test,
                        allow_opaque_null_interaction,
                        allow_opaque_null_side_effect_sensitivity,
                        include_confounded_side_effect_neutralization,
                    )
                    if planned_mutants:
                        for variant, mutated_source, mutant_meta in planned_mutants:
                            item_result["mutant_variants"].append(variant)
                            item_result[f"{variant}_operator_rule"] = mutant_meta.get("operator_rule")
                            item_result["mutants"].append(
                                {
                                    "operator": mutant_meta.get("operator") or construct,
                                    "sub_variant": mutant_meta.get("sub_variant") or variant,
                                    "variant": variant,
                                    "source_line": item.get("line"),
                                    "mutant_text": mutated_line_text(mutated_source, item.get("line")),
                                    "covered": False,
                                    "coverage_imprecise": item_result.get("coverage_imprecise"),
                                    "status": "not_reached",
                                    "outcome": None,
                                    "failure_type": None,
                                    "artifact_class": item_result.get("artifact_class"),
                                    "operator_rule": mutant_meta.get("operator_rule"),
                                    "return_subcategory": return_subcategory_for_operator_rule(mutant_meta.get("operator_rule"))
                                    if construct == "return_behavior"
                                    else None,
                                    "tags": mutant_tags(mutant_meta, "not_reached"),
                                    "observability_signal": mutant_meta.get("observability_signal"),
                                    "same_package_test": mutant_meta.get("same_package_test"),
                                    "unobservable_return_state": bool(mutant_meta.get("unobservable_return_state")),
                                    "subsumed_by_broad_original": bool(mutant_meta.get("subsumed_by_broad_original")),
                                }
                            )
                        status_counts["not_reached"] += len(planned_mutants)
                    else:
                        item_result["mutants"].append(
                            {
                                "operator": construct,
                                "sub_variant": "not_reached",
                                "variant": "not_reached",
                                "source_line": item.get("line"),
                                "mutant_text": None,
                                    "covered": False,
                                    "coverage_imprecise": item_result.get("coverage_imprecise"),
                                    "status": "not_reached",
                                    "outcome": None,
                                "failure_type": None,
                                    "artifact_class": item_result.get("artifact_class"),
                                "operator_rule": "not_reached",
                                "return_subcategory": None,
                                "tags": [],
                            }
                        )
                        status_counts["not_reached"] += 1
                    scored_items.append(item_result)
                    continue
            mutants = mutant_sources_for_item(
                original_source,
                item,
                return_type,
                signature,
                same_package_test,
                allow_opaque_null_interaction,
                allow_opaque_null_side_effect_sensitivity,
                include_confounded_side_effect_neutralization,
            )
            if only_mutant_variants:
                mutants = [mutant for mutant in mutants if mutant[0] in only_mutant_variants]
            oracle_killed_count = 0
            incidental_killed_count = 0
            represented_status_count = 0
            variant_statuses: list[str] = []
            for variant, mutated_source, mutant_meta in mutants:
                item_result["mutant_variants"].append(variant)
                item_result[f"{variant}_operator_rule"] = mutant_meta.get("operator_rule")
                for meta_key, meta_value in sorted(mutant_meta.items()):
                    item_result[f"{variant}_{meta_key}"] = meta_value
                if mutated_source is None or mutated_source == original_source:
                    result = {
                        "status": status_for_unwritten_mutant(mutant_meta),
                        "stage": mutant_meta.get("operator_rule") or "rewrite",
                    }
                else:
                    result = run_isolated_mutant_test(
                        workdir,
                        prompt_name,
                        f"{item_id}-{variant}",
                        mutated_source,
                        timeout,
                        mvn,
                        test_class,
                        collect_coverage=bool(mutant_meta.get("downstream_flow_risk")),
                    )
                    if result["status"] == "killed":
                        replacement_exception = (
                            mutant_meta.get("replacement_exception_type")
                            if construct == "exception_behavior"
                            else None
                        )
                        status, reason = killed_status_from_execute_log(result.get("execute_log"), replacement_exception)
                        result = {**result, "status": status, "kill_classification_reason": reason}
                    elif result["status"] == "survived" and mutant_meta.get("downstream_flow_risk"):
                        mutant_covered_lines = set(result.get("covered_lines") or [])
                        if result.get("coverage_status") != "passed" or not coverage_available or not method_lines:
                            result = {
                                **result,
                                "status": "coverage_unverified",
                                "kill_classification_reason": "mutant_path_coverage_unavailable",
                            }
                        else:
                            original_method_coverage = covered_lines & method_lines
                            mutant_method_coverage = mutant_covered_lines & method_lines
                            changed_lines = sorted(original_method_coverage ^ mutant_method_coverage)
                            result = {
                                **result,
                                "status": "survived_path_changed" if changed_lines else "survived",
                                "path_coverage_changed": bool(changed_lines),
                                "path_coverage_changed_lines": changed_lines,
                                "original_method_covered_lines": sorted(original_method_coverage),
                                "mutant_method_covered_lines": sorted(mutant_method_coverage),
                            }
                item_result[f"{variant}_mutant_status"] = result["status"]
                item_result[f"{variant}_mutant_stage"] = result.get("stage")
                item_result[f"{variant}_mutant_kill_classification_reason"] = result.get("kill_classification_reason")
                item_result[f"{variant}_mutant_compile_log"] = result.get("compile_log")
                item_result[f"{variant}_mutant_execute_log"] = result.get("execute_log")
                item_result[f"{variant}_mutant_coverage_status"] = result.get("coverage_status")
                item_result[f"{variant}_path_coverage_changed"] = result.get("path_coverage_changed")
                item_result[f"{variant}_path_coverage_changed_lines"] = result.get("path_coverage_changed_lines")
                status_counts[result["status"]] += 1
                variant_statuses.append(str(result["status"]))
                if result["status"] == "killed_by_oracle":
                    oracle_killed_count += 1
                if result["status"] == "killed_incidentally":
                    incidental_killed_count += 1
                if result["status"] in REPRESENTED_STATUSES:
                    represented_status_count += 1
                pre_execution_status = result["status"] if result["status"] in {"not_generated", "out_of_scope", "coverage_unverified"} else "executed"
                execution_outcome = None if pre_execution_status in {"not_generated", "out_of_scope", "coverage_unverified"} else result["status"]
                tags = mutant_tags(mutant_meta, result["status"])
                excluded_from_primary = bool(
                    mutant_meta.get("sensitivity_only")
                    or result["status"] == "survived_path_changed"
                    or (
                        mutant_meta.get("degenerate_null")
                        and degenerate_null_score == "exclude"
                        and result["status"] in REPRESENTED_STATUSES
                    )
                )
                mutant_record = {
                    "operator": mutant_meta.get("operator") or construct,
                    "sub_variant": mutant_meta.get("sub_variant") or variant,
                    "variant": variant,
                    "source_line": item.get("line"),
                    "mutant_text": mutated_line_text(mutated_source, item.get("line")),
                    "covered": item_result.get("covered_by_original_test"),
                    "coverage_imprecise": item_result.get("coverage_imprecise"),
                    "status": pre_execution_status,
                    "outcome": execution_outcome,
                    "failure_type": result.get("kill_classification_reason") if str(result["status"]).startswith("killed_") else None,
                    "tooling_failure_reason": result.get("kill_classification_reason") if result["status"] == "coverage_unverified" else None,
                    "artifact_class": item_result.get("artifact_class"),
                    "operator_rule": mutant_meta.get("operator_rule"),
                    "return_subcategory": return_subcategory_for_operator_rule(mutant_meta.get("operator_rule"))
                    if construct == "return_behavior"
                    else None,
                    "tags": tags,
                    "degenerate_null": bool(mutant_meta.get("degenerate_null")),
                    "observability_signal": mutant_meta.get("observability_signal"),
                    "same_package_test": mutant_meta.get("same_package_test"),
                    "unobservable_return_state": bool(mutant_meta.get("unobservable_return_state")),
                    "subsumed_by_broad_original": bool(mutant_meta.get("subsumed_by_broad_original")),
                    "excluded_from_primary_score": excluded_from_primary,
                    "coverage_status": result.get("coverage_status"),
                    "path_coverage_changed": result.get("path_coverage_changed"),
                    "path_coverage_changed_lines": result.get("path_coverage_changed_lines") or [],
                }
                for trace_key in [
                    "argument_index",
                    "original_argument",
                    "replacement_argument",
                    "argument_source",
                    "replacement_expression",
                    "stub_rule",
                    "inferred_type",
                    "use_contexts",
                    "downstream_flow_risk",
                    "whole_rhs_substitution",
                    "call_behavior",
                    "mutation_dimension",
                    "confounded_dimensions",
                    "sensitivity_only",
                    "observability_signal",
                    "same_package_test",
                    "unobservable_return_state",
                    "subsumed_by_broad_original",
                    "original_exception_type",
                    "replacement_exception_type",
                    "constructor_rule",
                ]:
                    if trace_key in mutant_meta:
                        mutant_record[trace_key] = mutant_meta[trace_key]
                item_result["mutants"].append(mutant_record)
            represented = represented_status_count > 0
            item_result["represented"] = represented
            item_result["oracle_killed_mutants"] = oracle_killed_count
            item_result["incidentally_killed_mutants"] = incidental_killed_count
            item_result["score"] = None
            item_result["aggregation_rule"] = "mean_represented_variants"
            if not represented:
                item_result["status"] = dominant_nonrepresented_outcome(variant_statuses)
                item_result["outcome"] = None
                if any(item_result.get(f"{variant}_mutant_status") == "not_generated" for variant in item_result["mutant_variants"]):
                    item_result["not_generated_operator"] = ",".join(item_result["mutant_variants"]) or construct
                    item_result["not_generated_operator_rule"] = ",".join(
                        str(item_result.get(f"{variant}_operator_rule") or "unknown")
                        for variant in item_result["mutant_variants"]
                        if item_result.get(f"{variant}_mutant_status") == "not_generated"
                    )
                if any(item_result.get(f"{variant}_mutant_status") == "out_of_scope" for variant in item_result["mutant_variants"]):
                    item_result["not_generated_operator"] = ",".join(item_result["mutant_variants"]) or construct
                    item_result["not_generated_operator_rule"] = ",".join(
                        str(item_result.get(f"{variant}_operator_rule") or "unknown")
                        for variant in item_result["mutant_variants"]
                        if item_result.get(f"{variant}_mutant_status") in {"not_generated", "out_of_scope"}
                    )
                item_result["score_reason"] = f"custom_assertion_mutant_{item_result['status']}"
            else:
                aggregate = aggregate_variant_scores(item_result["mutants"], incidental_score)
                item_result["score"] = aggregate["score"]
                item_result["score_any_killed"] = aggregate["score_any_killed"]
                item_result["score_all_killed"] = aggregate["score_all_killed"]
                item_result["represented_variant_count"] = aggregate["represented_variant_count"]
                item_result["primary_variant_count"] = aggregate["primary_variant_count"]
                item_result["scored_variant_count"] = aggregate["scored_variant_count"]
                item_result["status"] = "executed"
                item_result["outcome"] = aggregate["outcome"]
                item_result["score_reason"] = f"custom_assertion_mutant_{item_result['outcome'] or item_result['status']}_mean_represented_variants"
            scored_items.append(item_result)

    return aggregate_scored_items(scored_items, status_counts)


def paper_mutation_result(result: dict[str, Any]) -> dict[str, Any]:
    """Build a report-only view without changing collected evidence."""
    if result.get("status") == "passed":
        items = [item for item in result.get("items") or [] if item.get("construct") in {"return_behavior", "exception_behavior"}]
        counts = Counter(str(item.get("outcome") or item.get("status") or "unknown") for item in items)
        result = {**result, **aggregate_scored_items(items, counts), "collector_version": result.get("collector_version", ASSERTION_MUTATION_VERSION)}
    return {
        key: value for key, value in result.items()
        if not key.startswith(("side_effect_", "legacy_all_construct_"))
    }


def paper_result_row(row: dict[str, Any]) -> dict[str, Any]:
    if "assertion_mutation" not in row:
        return row
    return {**row, "assertion_mutation": paper_mutation_result(row["assertion_mutation"])}


def summarize(rows: list[dict[str, Any]], path: Path) -> None:
    rows = [paper_result_row(row) for row in rows]
    collected = [row for row in rows if (row.get("assertion_mutation") or {}).get("status") == "passed"]
    total = sum(int((row.get("assertion_mutation") or {}).get("total_items") or 0) for row in collected)
    reached = sum(int((row.get("assertion_mutation") or {}).get("reached_items") or 0) for row in collected)
    not_reached = sum(int((row.get("assertion_mutation") or {}).get("not_reached_items") or 0) for row in collected)
    scorable = sum(int((row.get("assertion_mutation") or {}).get("scorable_items") or 0) for row in collected)
    non_representable = sum(int((row.get("assertion_mutation") or {}).get("non_representable_items") or 0) for row in collected)
    score_sum = sum(float((row.get("assertion_mutation") or {}).get("score_sum") or 0) for row in collected)
    pessimistic_score_sum = sum(
        float((row.get("assertion_mutation") or {}).get("score_sum") or 0)
        for row in collected
    )
    pessimistic_denominator = sum(
        int((row.get("assertion_mutation") or {}).get("pessimistic_denominator") or 0)
        for row in collected
    )
    row_scores = [
        float((row.get("assertion_mutation") or {}).get("score"))
        for row in collected
        if (row.get("assertion_mutation") or {}).get("score") is not None
    ]
    row_pessimistic_scores = [
        float((row.get("assertion_mutation") or {}).get("pessimistic_score"))
        for row in collected
        if (row.get("assertion_mutation") or {}).get("pessimistic_score") is not None
    ]
    checked = sum(int((row.get("assertion_mutation") or {}).get("checked_items") or 0) for row in collected)
    oracle_killed = sum(int((row.get("assertion_mutation") or {}).get("oracle_killed_items") or 0) for row in collected)
    incidentally_killed = sum(int((row.get("assertion_mutation") or {}).get("incidentally_killed_items") or 0) for row in collected)
    survived = sum(int((row.get("assertion_mutation") or {}).get("survived_items") or 0) for row in collected)
    survived_path_changed = sum(
        int((row.get("assertion_mutation") or {}).get("survived_path_changed_items") or 0)
        for row in collected
    )
    coverage_unverified = sum(
        int((row.get("assertion_mutation") or {}).get("coverage_unverified_items") or 0)
        for row in collected
    )
    status_counts: Counter[str] = Counter()
    pre_execution_status_counts: Counter[str] = Counter()
    execution_outcome_counts: Counter[str] = Counter()
    by_construct: dict[str, Counter[str]] = defaultdict(Counter)
    not_generated_by_operator: Counter[str] = Counter()
    not_generated_by_operator_rule: Counter[str] = Counter()
    not_generated_by_return_type: Counter[str] = Counter()
    by_operator: dict[str, Counter[str]] = defaultdict(Counter)
    degenerate_null_items = 0
    downstream_flow_risk_items = 0
    for row in collected:
        result = row.get("assertion_mutation") or {}
        status_counts.update(result.get("status_counts") or {})
        pre_execution_status_counts.update(result.get("mutant_pre_execution_status_counts") or {})
        execution_outcome_counts.update(result.get("mutant_execution_outcome_counts") or {})
        not_generated_by_operator.update(result.get("not_generated_by_operator") or {})
        not_generated_by_operator_rule.update(result.get("not_generated_by_operator_rule") or {})
        not_generated_by_return_type.update(result.get("not_generated_by_return_type_category") or {})
        for operator, stats in (result.get("by_operator") or {}).items():
            by_operator[operator].update(stats)
        degenerate_null_items += int(result.get("degenerate_null_items") or 0)
        downstream_flow_risk_items += int(result.get("downstream_flow_risk_items") or 0)
        for construct, stats in (result.get("by_construct") or {}).items():
            for key, value in stats.items():
                if isinstance(value, int):
                    by_construct[construct][key] += value
                elif isinstance(value, float) and key == "score_sum":
                    by_construct[construct][key] += value

    lines = [
        "# Custom Assertion Mutation Summary",
        "",
        f"- Rows with collected evidence: {len(collected)}",
        f"- Custom assertion items: {total}",
        f"- Reached assertion items: {reached}",
        f"- Not-reached assertion items: {not_reached}",
        f"- Scorable custom assertion items: {scorable}",
        f"- Non-representable custom assertion items: {non_representable}",
        f"- Checked custom assertion items: {checked}",
        f"- Oracle-killed items: {oracle_killed}",
        f"- Incidentally killed items: {incidentally_killed}",
        f"- Survived items: {survived}",
        f"- Survivors excluded because method coverage changed: {survived_path_changed}",
        f"- Non-representable mutants because coverage comparison failed: {coverage_unverified}",
        f"- Degenerate-null mutants across all constructs: {degenerate_null_items}",
        f"- Dependency-result items with downstream-flow risk: {downstream_flow_risk_items}",
        f"- Pooled custom assertion score over scorable items: {score_sum / scorable if scorable else 'N/A'}",
        f"- Macro custom assertion score over rows: {sum(row_scores) / len(row_scores) if row_scores else 'N/A'}",
        f"- Pooled pessimistic bound, counting not_generated/out_of_scope as survived: {pessimistic_score_sum / pessimistic_denominator if pessimistic_denominator else 'N/A'}",
        f"- Macro pessimistic bound over rows: {sum(row_pessimistic_scores) / len(row_pessimistic_scores) if row_pessimistic_scores else 'N/A'}",
        f"- Custom-mutant representability rate over reached items: {(reached - non_representable) / reached if reached else 'N/A'}",
        f"- Custom-mutant scorable rate over reached items: {scorable / reached if reached else 'N/A'}",
        "",
        "## By Construct",
        "",
        "| Construct | Total | Reached | Not Reached | Scorable | Scorable / Reached | Oracle Killed | Incidental | Unchanged-Path Survived | Path Changed | Coverage Failed | Flow Risk | Non-Rep | Generation Rate | Score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for construct, stats in sorted(by_construct.items()):
        scorable_construct = int(stats["scorable"])
        score = float(stats["score_sum"]) / scorable_construct if scorable_construct else None
        total_construct = int(stats["total"])
        not_reached_construct = int(stats["not_reached_items"])
        reached_construct = total_construct - not_reached_construct
        generation_rate = int(stats["represented"]) / reached_construct if reached_construct else None
        lines.append(
            f"| {construct} | {total_construct} | {reached_construct} | {not_reached_construct} | "
            f"{scorable_construct} | {scorable_construct / reached_construct if reached_construct else 'N/A'} | "
            f"{int(stats['killed_by_oracle_items'])} | "
            f"{int(stats['killed_incidentally_items'])} | {int(stats['survived_items'])} | "
            f"{int(stats['survived_path_changed_items'])} | {int(stats['coverage_unverified_items'])} | "
            f"{int(stats['downstream_flow_risk_items'])} | "
            f"{int(stats['non_representable_items'])} | {generation_rate if generation_rate is not None else 'N/A'} | "
            f"{score if score is not None else 'N/A'} |"
        )
    lines.extend(["", "## Mutant Status Counts", "", "| Status | Mutants |", "| --- | ---: |"])
    for status, count in sorted(status_counts.items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## Pre-Execution Status Counts", "", "| Status | Mutants |", "| --- | ---: |"])
    for status, count in sorted(pre_execution_status_counts.items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## Execution Outcome Counts", "", "| Outcome | Mutants |", "| --- | ---: |"])
    for outcome, count in sorted(execution_outcome_counts.items()):
        lines.append(f"| {outcome} | {count} |")
    outcome_columns = [
        "status_executed",
        "status_not_reached",
        "status_not_generated",
        "status_out_of_scope",
        "killed_by_oracle",
        "killed_incidentally",
        "survived",
        "survived_path_changed",
        "coverage_unverified",
        "compile_error",
        "timeout",
        "outcome_killed_by_oracle",
        "outcome_killed_incidentally",
        "outcome_survived",
        "outcome_survived_path_changed",
        "outcome_coverage_unverified",
        "outcome_compile_error",
        "outcome_timeout",
        "tag_degenerate_null",
        "tag_downstream_flow_risk",
        "tag_path_coverage_changed",
        "tag_coverage_unverified",
    ]
    lines.extend(
        [
            "",
            "## By Operator Outcome",
            "",
            "| Operator | Total | Generation Rate | " + " | ".join(outcome_columns) + " |",
            "| --- | ---: | ---: | " + " | ".join(["---:"] * len(outcome_columns)) + " |",
        ]
    )
    for operator, stats in sorted(by_operator.items()):
        represented_mutants = sum(int(stats.get(status) or 0) for status in REPRESENTED_STATUSES)
        generation_rate = represented_mutants / int(stats.get("total") or 0) if int(stats.get("total") or 0) else None
        lines.append(
            f"| {operator} | {int(stats.get('total') or 0)} | {generation_rate if generation_rate is not None else 'N/A'} | "
            + " | ".join(str(int(stats.get(column) or 0)) for column in outcome_columns)
            + " |"
        )
    lines.extend(["", "## Not Generated By Operator", "", "| Operator | Items |", "| --- | ---: |"])
    for operator, count in sorted(not_generated_by_operator.items()):
        lines.append(f"| {operator} | {count} |")
    lines.extend(["", "## Not Generated By Operator Rule", "", "| Operator Rule | Items |", "| --- | ---: |"])
    for operator_rule, count in sorted(not_generated_by_operator_rule.items()):
        lines.append(f"| {operator_rule} | {count} |")
    lines.extend(["", "## Not Generated Return Types", "", "| Return Type Category | Items |", "| --- | ---: |"])
    for category, count in sorted(not_generated_by_return_type.items()):
        lines.append(f"| {category} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_rows_checkpoint(rows: list[dict[str, Any]], out: Path, summary: Path) -> None:
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summarize(rows, summary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--mvn", default=DEFAULT_MVN)
    parser.add_argument("--include-prompt", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to process.")
    parser.add_argument("--max-items-per-row", type=int, default=None)
    parser.add_argument(
        "--only-construct",
        action="append",
        default=[],
        choices=["exception_behavior", "interaction_dependency", "return_behavior", "side_effect_or_dependency"],
        help="Only collect custom mutants for this construct; can be repeated.",
    )
    parser.add_argument(
        "--incidental-score",
        choices=["zero", "exclude"],
        default="zero",
        help="How to treat reached mutants killed by non-assertion runtime failures.",
    )
    parser.add_argument(
        "--degenerate-null-score",
        choices=["score", "exclude"],
        default="score",
        help="How to treat opaque-object return mutants whose only generated replacement is null.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--select-items-from",
        type=Path,
        default=None,
        help="Only run obligations selected from a prior analysis/assertion JSONL.",
    )
    parser.add_argument(
        "--select-item-status",
        action="append",
        default=[],
        help="Status to select with --select-items-from; defaults to out_of_scope.",
    )
    parser.add_argument(
        "--only-mutant-variant",
        action="append",
        default=[],
        help="Only execute the named mutant variant; can be repeated.",
    )
    parser.add_argument(
        "--allow-opaque-null-interaction",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-opaque-null-side-effect-sensitivity",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--include-confounded-side-effect-neutralization",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    out = args.out or args.result.with_name(args.result.stem + "-with-assertion-mutation.jsonl")
    summary = out.with_name(out.stem + "-summary.md")
    cleanliness_report = out.with_name(out.stem + "-cleanliness.json")
    git_status_before = git_status_short(ROOT)
    existing = existing_rows(out) if args.resume else {}
    include_prompts = set(args.include_prompt)
    only_constructs = set(args.only_construct)
    selected_items = (
        selected_assertion_items(args.select_items_from, set(args.select_item_status or ["out_of_scope"]))
        if args.select_items_from
        else None
    )
    if selected_items is not None and only_constructs:
        selected_items = {
            key: {item_key: item for item_key, item in items.items() if item_key[0] in only_constructs}
            for key, items in selected_items.items()
        }
        selected_items = {key: items for key, items in selected_items.items() if items}
    filtered_rows = [
        row
        for row in rows
        if (not include_prompts or str(row.get("prompt_template")) in include_prompts)
        and (selected_items is None or selection_row_key(row) in selected_items)
    ]
    only_mutant_variants = set(args.only_mutant_variant)
    processed = 0
    updated: list[dict[str, Any]] = []
    for index, row in enumerate(filtered_rows, start=1):
        key = row_key(row)
        existing_mutation = existing.get(key, {}).get("assertion_mutation") or {}
        if (
            args.resume
            and existing_mutation.get("status")
            and existing_mutation.get("collector_version") == ASSERTION_MUTATION_VERSION
        ):
            updated.append(existing[key])
            print(f"[{index}/{len(filtered_rows)}] {row.get('task_id')} {row.get('prompt_template')} skipped=resume")
            continue
        if args.limit is not None and processed >= args.limit:
            updated.append(row)
            continue
        processed += 1
        print(f"[{index}/{len(filtered_rows)}] {row.get('task_id')} {row.get('prompt_template')}")
        result = collect_row(
            row,
            args.timeout,
            args.mvn,
            args.max_items_per_row,
            only_constructs or None,
            args.incidental_score,
            args.degenerate_null_score,
            selected_items.get(selection_row_key(row), set()) if selected_items is not None else None,
            only_mutant_variants or None,
            args.allow_opaque_null_interaction,
            args.allow_opaque_null_side_effect_sensitivity,
            args.include_confounded_side_effect_neutralization,
        )
        row = {**row, "assertion_mutation": result}
        updated.append(row)
        print(
            f"  {result['status']} items={result.get('total_items')} "
            f"score={result.get('score')} checked={result.get('checked_items')}"
        )
        write_rows_checkpoint(updated, out, summary)

    write_rows_checkpoint(updated, out, summary)
    git_status_after = git_status_short(ROOT)
    write_cleanliness_report(cleanliness_report, git_status_before, git_status_after)
    print(f"wrote {out}")
    print(f"wrote {summary}")
    print(f"wrote {cleanliness_report}")
    print(f"git_status_unchanged={git_status_before.get('stdout') == git_status_after.get('stdout') and git_status_before.get('returncode') == git_status_after.get('returncode')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
