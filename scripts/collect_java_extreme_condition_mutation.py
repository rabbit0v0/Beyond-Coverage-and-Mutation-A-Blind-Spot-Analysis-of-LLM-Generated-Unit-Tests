#!/usr/bin/env python3
"""Run deterministic custom control-flow mutants for Java generated tests.

The main custom score covers:

  * if / else-if: condition forced to true and false
  * ternary: condition forced to true and false
  * case / default: case body replaced by no-op
  * catch: catch body replaced by no-op
  * exception_path: throw statement removed, with a type-aware return fallback

Loop conditions, enhanced-for iteration, and try blocks are intentionally not
part of this checked-control-flow score.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from analyze_java_blindspots import control_flow_obligation_items


ROOT = Path(__file__).resolve().parents[1]
EXEC_PLUGIN = "org.codehaus.mojo:exec-maven-plugin:3.5.0:java"
DEFAULT_MVN = shutil.which("mvn") or ("/opt/homebrew/bin/mvn" if Path("/opt/homebrew/bin/mvn").exists() else "mvn")
CUSTOM_MUTATION_VERSION = "custom_control_flow_v8"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("task_id")), str(row.get("prompt_template")))


def panta_test_class(row: dict[str, Any]) -> str | None:
    if row.get("test_class"):
        return str(row["test_class"])
    if row.get("prompt_template") != "panta":
        return None
    value = row.get("test_file")
    path_value = value.get("path") if isinstance(value, dict) else value
    if not path_value:
        return "benchmark.PantaGeneratedTest"
    path = Path(path_value)
    if not path.is_absolute():
        workdir = Path(row.get("workdir") or "")
        if not workdir.is_absolute():
            workdir = ROOT / workdir
        path = workdir / path
    if not path.exists():
        return "benchmark.PantaGeneratedTest"
    text = path.read_text(encoding="utf-8", errors="replace")
    package_match = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.MULTILINE)
    class_match = re.search(r"\b(?:public\s+)?class\s+([A-Za-z_]\w*)\b", text)
    package = package_match.group(1) if package_match else ""
    class_name = class_match.group(1) if class_match else path.stem
    return f"{package}.{class_name}" if package else class_name


def existing_rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    return {row_key(row): row for row in read_jsonl(path)}


def run_command(args: list[str], cwd: Path, log_path: Path, timeout: int) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        log_path.write_text(output + "\n[TIMEOUT]\n", encoding="utf-8", errors="replace")
        return {"status": "timeout", "returncode": None, "log": str(log_path)}
    except FileNotFoundError as exc:
        log_path.write_text(f"[COMMAND_MISSING] {exc}\n", encoding="utf-8", errors="replace")
        return {"status": "command_missing", "returncode": None, "log": str(log_path)}
    log_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    return {"status": "completed", "returncode": result.returncode, "log": str(log_path)}


def find_matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "'":
            in_char = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def find_matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "'":
            in_char = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", source):
        offsets.append(match.end())
    return offsets


def line_bounds(source: str, line_number: int) -> tuple[int, int] | None:
    offsets = line_start_offsets(source)
    if line_number < 1 or line_number > len(offsets):
        return None
    line_start = offsets[line_number - 1]
    line_end = source.find("\n", line_start)
    if line_end == -1:
        line_end = len(source)
    return line_start, line_end


def mutated_if_source_for_item(source: str, item: dict[str, Any], replacement: str) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, line_end = bounds
    line_text = source[line_start:line_end]
    match = re.search(r"\bif\s*\(", line_text)
    if not match:
        return None
    if_start = line_start + match.start()
    open_index = source.find("(", if_start)
    if open_index == -1:
        return None
    close_index = find_matching_paren(source, open_index)
    if close_index is None:
        return None
    return source[: open_index + 1] + replacement + source[close_index:]


def ternary_question_and_colon(source: str, line_number: int) -> tuple[int, int] | None:
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    start, end = bounds
    depth = 0
    in_string = False
    escaped = False
    question_index: int | None = None
    question_depth: int | None = None
    for index in range(start, end):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        elif char == "?" and question_index is None:
            question_index = index
            question_depth = depth
        elif char == ":" and question_index is not None and depth == question_depth:
            return question_index, index
    return None


def ternary_condition_start(source: str, line_start: int, question_index: int) -> int:
    prefix = source[line_start:question_index]
    return_match = re.search(r"\breturn\s+([^;=]+)$", prefix)
    if return_match:
        return line_start + return_match.start(1)
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for index in range(question_index - 1, line_start - 1, -1):
        char = source[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "'":
            in_char = True
            continue
        if char in ")]}":
            depth += 1
            continue
        if char in "([{":
            if depth > 0:
                depth -= 1
                continue
            return index + 1
        if depth > 0:
            continue
        if char == "=":
            prev_char = source[index - 1] if index > line_start else ""
            next_char = source[index + 1] if index + 1 < len(source) else ""
            if prev_char in {"=", "!", "<", ">"} or next_char == "=":
                continue
            return index + 1
        if char in ",;":
            return index + 1
    return line_start


def mutated_ternary_source_for_item(source: str, item: dict[str, Any], replacement: str) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    positions = ternary_question_and_colon(source, line_number)
    if bounds is None or positions is None:
        return None
    line_start, _ = bounds
    question_index, _ = positions
    condition_start = ternary_condition_start(source, line_start, question_index)
    if condition_start >= question_index:
        return None
    return source[:condition_start] + replacement + " " + source[question_index:]


def find_case_label_colon(source: str, line_number: int) -> int | None:
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, line_end = bounds
    colon = source.find(":", line_start, line_end)
    return colon if colon != -1 else None


def find_case_label_colon_from_index(source: str, label_start: int) -> int | None:
    line_end = source.find("\n", label_start)
    if line_end == -1:
        line_end = len(source)
    colon = source.find(":", label_start, line_end)
    return colon if colon != -1 else None


def find_case_body_end(source: str, body_start: int) -> int | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    index = body_start
    while index < len(source):
        char = source[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "'":
            in_char = True
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0:
            match = re.match(r"\s*(?:case\b|default\s*:)", source[index:])
            if match:
                return index + len(match.group(0)) - len(match.group(0).lstrip())
        index += 1
    return None


def next_case_or_default_start(source: str, body_start: int) -> int | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    index = body_start
    while index < len(source):
        char = source[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "'":
            in_char = True
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return None
            depth -= 1
        elif depth == 0:
            match = re.match(r"\s*(?:case\b|default\s*:)", source[index:])
            if match:
                return index + len(match.group(0)) - len(match.group(0).lstrip())
        index += 1
    return None


def case_body_start_for_line(source: str, line_number: int) -> int | None:
    colon = find_case_label_colon(source, line_number)
    if colon is None:
        return None
    body_start = colon + 1
    seen_body_starts: set[int] = set()
    while True:
        if body_start in seen_body_starts:
            return body_start
        seen_body_starts.add(body_start)
        next_label = next_case_or_default_start(source, body_start)
        body_end = find_case_body_end(source, body_start)
        if next_label is None or body_end is None:
            return body_start
        if next_label > body_end:
            return body_start
        between = source[body_start:next_label].strip()
        if between:
            return body_start
        next_colon = find_case_label_colon_from_index(source, next_label)
        if next_colon is None or next_colon < body_start:
            return body_start
        body_start = next_colon + 1


def indentation_before(source: str, index: int) -> str:
    line_start = source.rfind("\n", 0, index) + 1
    match = re.match(r"[ \t]*", source[line_start:index])
    return match.group(0) if match else ""


def mutated_case_source_for_item(source: str, item: dict[str, Any]) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    body_start = case_body_start_for_line(source, line_number)
    if body_start is None:
        return None
    body_end = find_case_body_end(source, body_start)
    if body_end is None or body_end <= body_start:
        return None
    indent = indentation_before(source, body_start) + "    "
    replacement = "\n" + indent + ";\n"
    return source[:body_start] + replacement + source[body_end:]


def mutated_catch_source_for_item(source: str, item: dict[str, Any]) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    start, _ = bounds
    match = re.search(r"\bcatch\s*\(", source[start:])
    if not match:
        return None
    catch_start = start + match.start()
    open_paren = source.find("(", catch_start)
    close_paren = find_matching_paren(source, open_paren)
    if close_paren is None:
        return None
    open_brace = source.find("{", close_paren)
    if open_brace == -1:
        return None
    close_brace = find_matching_brace(source, open_brace)
    if close_brace is None:
        return None
    indent = indentation_before(source, open_brace) + "    "
    replacement = "\n" + indent + ";\n" + indentation_before(source, open_brace)
    return source[: open_brace + 1] + replacement + source[close_brace:]


def find_statement_end(source: str, start: int) -> int | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif in_string and char == '"':
                in_string = False
            elif in_char and char == "'":
                in_char = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "'":
            in_char = True
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        elif char == ";" and depth == 0:
            return index + 1
    return None


def mutated_exception_source_for_item(source: str, item: dict[str, Any]) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, _ = bounds
    match = re.search(r"\bthrow\b", source[line_start:])
    if not match:
        return None
    throw_start = line_start + match.start()
    statement_end = find_statement_end(source, throw_start)
    if statement_end is None:
        return None
    return source[:throw_start] + ";" + source[statement_end:]


def method_return_type(signature: str | None) -> str | None:
    if not signature:
        return None
    cleaned = re.sub(r"@\w+(?:\([^)]*\))?", " ", signature)
    cleaned = " ".join(cleaned.replace("\n", " ").split())
    match = re.search(
        r"(?:public|protected|private|static|final|synchronized|abstract|native|strictfp|\s)+"
        r"([A-Za-z_$][\w$.\[\]<>?,\s]*?)\s+[A-Za-z_$][\w$]*\s*\(",
        cleaned,
    )
    if match:
        return " ".join(match.group(1).split())
    return None


def alternate_return_expression(return_type: str | None, original_expr: str | None = None) -> str | None:
    if not return_type:
        return None
    typ = re.sub(r"<.*>", "", return_type).strip()
    typ = typ.replace("final ", "").strip()
    original = (original_expr or "").strip()
    if typ == "void":
        return None
    if typ in {"boolean", "Boolean"}:
        if original == "true":
            return "false"
        if original == "false":
            return "true"
        return f"!({original})" if original else "false"
    if typ in {"byte", "short", "int", "long", "float", "double", "Byte", "Short", "Integer", "Long", "Float", "Double"}:
        return "1" if original in {"0", "0L", "0f", "0.0", "0.0f", "0.0d"} else "0"
    if typ in {"char", "Character"}:
        return "'\\0'" if original != "'\\0'" else "'x'"
    if typ in {"String", "java.lang.String"}:
        return '"__mutated__"' if original in {'""', '"__mutated__"'} else '""'
    if typ.endswith("[]"):
        element_type = typ[:-2].strip()
        return f"new {element_type}[0]"
    return "null"


def first_return_statement_in_span(source: str, start: int, end: int) -> tuple[int, int, str | None] | None:
    for match in re.finditer(r"\breturn\b", source[start:end]):
        return_start = start + match.start()
        expr_start = start + match.end()
        statement_end = find_statement_end(source, return_start)
        if statement_end is None or statement_end > end:
            continue
        expr = source[expr_start : statement_end - 1].strip()
        return return_start, statement_end, expr or None
    return None


def mutated_return_expression_in_span(source: str, start: int, end: int, return_type: str | None) -> str | None:
    found = first_return_statement_in_span(source, start, end)
    if found is None:
        return None
    return_start, statement_end, expr = found
    replacement = alternate_return_expression(return_type, expr)
    if replacement is None:
        return None
    return source[:return_start] + f"return {replacement};" + source[statement_end:]


def mutated_case_return_fallback_source(source: str, item: dict[str, Any], return_type: str | None) -> str | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    body_start = case_body_start_for_line(source, line_number)
    if body_start is None:
        return None
    body_end = find_case_body_end(source, body_start)
    if body_end is None:
        return None
    return mutated_return_expression_in_span(source, body_start, body_end, return_type)


def catch_body_span(source: str, item: dict[str, Any]) -> tuple[int, int] | None:
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    start, _ = bounds
    match = re.search(r"\bcatch\s*\(", source[start:])
    if not match:
        return None
    catch_start = start + match.start()
    open_paren = source.find("(", catch_start)
    close_paren = find_matching_paren(source, open_paren)
    if close_paren is None:
        return None
    open_brace = source.find("{", close_paren)
    if open_brace == -1:
        return None
    close_brace = find_matching_brace(source, open_brace)
    if close_brace is None:
        return None
    return open_brace + 1, close_brace


def mutated_catch_return_fallback_source(source: str, item: dict[str, Any], return_type: str | None) -> str | None:
    span = catch_body_span(source, item)
    if span is None:
        return None
    return mutated_return_expression_in_span(source, span[0], span[1], return_type)


def mutated_exception_return_fallback_source(source: str, item: dict[str, Any], return_type: str | None) -> str | None:
    if not return_type:
        return None
    if return_type == "void":
        replacement_statement = "return;"
    else:
        replacement = alternate_return_expression(return_type)
        if replacement is None:
            return None
        replacement_statement = f"return {replacement};"
    line_number = item.get("line")
    if not isinstance(line_number, int):
        return None
    bounds = line_bounds(source, line_number)
    if bounds is None:
        return None
    line_start, _ = bounds
    match = re.search(r"\bthrow\b", source[line_start:])
    if not match:
        return None
    throw_start = line_start + match.start()
    statement_end = find_statement_end(source, throw_start)
    if statement_end is None:
        return None
    return source[:throw_start] + replacement_statement + source[statement_end:]


def primary_mutant_sources_for_item(source: str, item: dict[str, Any]) -> list[tuple[str, str | None]]:
    construct = str(item.get("construct") or "")
    if construct in {"if", "else_if"}:
        return [
            ("condition_true", mutated_if_source_for_item(source, item, "true")),
            ("condition_false", mutated_if_source_for_item(source, item, "false")),
        ]
    if construct == "ternary":
        return [
            ("condition_true", mutated_ternary_source_for_item(source, item, "true")),
            ("condition_false", mutated_ternary_source_for_item(source, item, "false")),
        ]
    if construct in {"case", "default"}:
        return [("body_noop", mutated_case_source_for_item(source, item))]
    if construct == "catch":
        return [("catch_body_noop", mutated_catch_source_for_item(source, item))]
    if construct == "exception_path":
        return [("throw_removed", mutated_exception_source_for_item(source, item))]
    return []


def fallback_mutant_sources_for_item(source: str, item: dict[str, Any], return_type: str | None) -> list[tuple[str, str | None]]:
    construct = str(item.get("construct") or "")
    if construct in {"case", "default"}:
        return [("fallback_return_value_changed", mutated_case_return_fallback_source(source, item, return_type))]
    if construct == "catch":
        return [("fallback_return_value_changed", mutated_catch_return_fallback_source(source, item, return_type))]
    if construct == "exception_path":
        return [("fallback_throw_to_default_return", mutated_exception_return_fallback_source(source, item, return_type))]
    return []


CUSTOM_CONTROL_FLOW_CONSTRUCTS = {"if", "else_if", "ternary", "case", "default", "catch", "exception_path"}


def custom_control_flow_items(source: str, signature: str | None) -> list[dict[str, Any]]:
    seen_ternary_lines: set[int] = set()
    seen_case_body_starts: set[int] = set()
    items = []
    for item in control_flow_obligation_items(source, signature):
        construct = item.get("construct")
        if construct not in CUSTOM_CONTROL_FLOW_CONSTRUCTS:
            continue
        if item.get("excluded_from_main_control_flow"):
            continue
        if construct in {"case", "default"}:
            line = item.get("line")
            if isinstance(line, int):
                body_start = case_body_start_for_line(source, line)
                if body_start is None:
                    items.append(item)
                    continue
                if body_start in seen_case_body_starts:
                    continue
                seen_case_body_starts.add(body_start)
                item = {**item, "case_group_body_start": body_start}
        if construct == "ternary":
            line = item.get("line")
            if isinstance(line, int) and line in seen_ternary_lines:
                continue
            if isinstance(line, int):
                seen_ternary_lines.add(line)
        items.append(item)
    return items


def run_generated_smoke_test(
    workdir: Path,
    prompt_name: str,
    label: str,
    timeout: int,
    mvn: str,
    test_class: str | None = None,
) -> dict[str, Any]:
    log_dir = workdir / "extreme-condition-mutants" / prompt_name
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
        exec_args = [mvn, "-q", f"-Dtest={test_class}", "test"]
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
        return {"status": "timeout", "stage": "execute", "compile_log": compile_result["log"], "execute_log": exec_result["log"]}
    if exec_result["status"] == "command_missing":
        return {"status": "command_missing", "stage": "execute", "compile_log": compile_result["log"], "execute_log": exec_result["log"]}
    if exec_result["returncode"] == 0:
        return {
            "status": "survived",
            "stage": "execute",
            "compile_log": compile_result["log"],
            "execute_log": exec_result["log"],
            "execute_returncode": exec_result["returncode"],
        }
    return {
        "status": "killed",
        "stage": "execute",
        "compile_log": compile_result["log"],
        "execute_log": exec_result["log"],
        "execute_returncode": exec_result["returncode"],
    }


def aggregate_scored_items(scored_items: list[dict[str, Any]], status_counts: Counter[str] | None = None) -> dict[str, Any]:
    status_counts = Counter(status_counts or {})
    by_construct: dict[str, Counter[str]] = defaultdict(Counter)
    for item in scored_items:
        construct = str(item.get("construct") or "unknown")
        score = item.get("score")
        represented = bool(item.get("represented"))
        total_variants = int(item.get("total_variants") or len(item.get("mutant_variants") or []))
        represented_variants = (
            int(item["represented_variants"])
            if item.get("represented_variants") is not None
            else (total_variants if represented else 0)
        )
        killed_count = sum(
            1
            for key, value in item.items()
            if key.endswith("_mutant_status") and value == "killed"
        )
        by_construct[construct]["total"] += 1
        by_construct[construct]["total_variants"] += total_variants
        by_construct[construct]["represented_variants"] += represented_variants
        if represented:
            by_construct[construct]["scorable"] += 1
            by_construct[construct]["represented"] += 1
        if score is not None:
            by_construct[construct]["score_sum"] += float(score)
            if float(score) > 0:
                by_construct[construct]["checked"] += 1
        if item.get("condition_true_mutant_status") == "killed":
            by_construct[construct]["true_killed"] += 1
        if item.get("condition_false_mutant_status") == "killed":
            by_construct[construct]["false_killed"] += 1
        by_construct[construct]["killed_mutants"] += killed_count
        for key, value in item.items():
            if key.endswith("_mutant_status") and value:
                status_counts[str(value)] += 1

    total = len(scored_items)
    scorable_items = sum(1 for item in scored_items if item.get("represented"))
    score_sum = sum(float(item.get("score") or 0) for item in scored_items if item.get("score") is not None)
    total_variants = sum(int(item.get("total_variants") or len(item.get("mutant_variants") or [])) for item in scored_items)
    represented_variants = sum(int(item.get("represented_variants") or 0) for item in scored_items)
    by_construct_out = {
        construct: {
            "total": int(stats["total"]),
            "scorable": int(stats["scorable"]),
            "score_sum": float(stats["score_sum"]),
            "score": float(stats["score_sum"]) / int(stats["scorable"]) if stats["scorable"] else None,
            "checked": int(stats["checked"]),
            "represented": int(stats["represented"]),
            "total_variants": int(stats["total_variants"]),
            "represented_variants": int(stats["represented_variants"]),
            "variant_representability_rate": (
                int(stats["represented_variants"]) / int(stats["total_variants"])
                if int(stats["total_variants"])
                else None
            ),
            "true_killed": int(stats["true_killed"]),
            "false_killed": int(stats["false_killed"]),
            "killed_mutants": int(stats["killed_mutants"]),
        }
        for construct, stats in sorted(by_construct.items())
    }
    return {
        "status": "passed",
        "collector_version": CUSTOM_MUTATION_VERSION,
        "reason": None,
        "total_conditions": total,
        "total_items": total,
        "scorable_conditions": scorable_items,
        "scorable_items": scorable_items,
        "non_representable_conditions": total - scorable_items,
        "non_representable_items": total - scorable_items,
        "attempted_mutants": sum(len(item.get("mutant_variants") or []) for item in scored_items),
        "score_sum": score_sum,
        "score": score_sum / scorable_items if scorable_items else None,
        "checked_items": sum(1 for item in scored_items if item.get("score") is not None and float(item["score"]) > 0),
        "fully_checked_items": sum(1 for item in scored_items if item.get("score") is not None and float(item["score"]) == 1),
        "represented_items": sum(1 for item in scored_items if item.get("represented")),
        "total_variants": total_variants,
        "represented_variants": represented_variants,
        "variant_representability_rate": represented_variants / total_variants if total_variants else None,
        "representability_rate": (
            sum(1 for item in scored_items if item.get("represented")) / total if total else None
        ),
        "status_counts": dict(status_counts),
        "by_construct": by_construct_out,
        "items": scored_items,
    }


def merge_construct_results(existing_result: dict[str, Any], refreshed_result: dict[str, Any], constructs: set[str]) -> dict[str, Any]:
    if refreshed_result.get("status") != "passed":
        return refreshed_result
    old_items = [
        item
        for item in existing_result.get("items") or []
        if str(item.get("construct") or "unknown") not in constructs
    ]
    new_items = [
        item
        for item in refreshed_result.get("items") or []
        if str(item.get("construct") or "unknown") in constructs
    ]
    merged = aggregate_scored_items([*old_items, *new_items])
    merged["reason"] = None
    merged["updated_constructs"] = sorted(constructs)
    merged["merged_from_collector_version"] = existing_result.get("collector_version")
    return merged


def scoped_result_summary(result: dict[str, Any], constructs: set[str] | None) -> dict[str, Any]:
    if not constructs:
        return result
    scoped_items = [
        item
        for item in result.get("items", [])
        if str(item.get("construct") or "unknown") in constructs
    ]
    return aggregate_scored_items(scoped_items)


def collect_row(
    row: dict[str, Any],
    timeout: int,
    mvn: str,
    max_conditions: int | None = None,
    only_constructs: set[str] | None = None,
) -> dict[str, Any]:
    test_class = panta_test_class(row)
    if not row.get("execution_passed") and not test_class:
        return {
            "status": "skipped",
            "reason": "original_generated_test_not_executed",
            "total_conditions": 0,
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
            "reason": "missing_subject_java",
            "total_conditions": 0,
            "score": None,
            "items": [],
        }

    source = source_path.read_text(encoding="utf-8", errors="replace")
    signature = (row.get("task") or {}).get("signature") or row.get("signature")
    return_type = method_return_type(signature)
    items = custom_control_flow_items(source, signature)
    if only_constructs:
        items = [item for item in items if str(item.get("construct") or "unknown") in only_constructs]
    if not items:
        return aggregate_scored_items([])
    if max_conditions is not None:
        items = items[:max_conditions]
    original_source = source
    prompt_name = str(row.get("prompt_template") or "unknown")

    scored_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    by_construct: dict[str, Counter[str]] = defaultdict(Counter)
    score_sum = 0.0
    scorable_items = 0

    try:
        for index, item in enumerate(items, start=1):
            construct = str(item.get("construct") or "unknown")
            item_id = f"{construct}-{index:04d}-line-{item.get('line')}"
            item_result: dict[str, Any] = {
                "index": index,
                "line": item.get("line"),
                "construct": construct,
                "signal": item.get("signal"),
                "condition": item.get("condition"),
                "snippet": item.get("snippet"),
                "method_return_type": return_type,
            }
            used_attempt = "primary"
            mutants = primary_mutant_sources_for_item(original_source, item)
            killed_count = 0

            def run_mutant_attempt(attempt: str, attempt_mutants: list[tuple[str, str | None]]) -> tuple[bool, int]:
                attempt_killed = 0
                for variant, mutated_source in attempt_mutants:
                    key = variant if attempt == "primary" else f"{attempt}_{variant}"
                    if mutated_source is None:
                        result = {"status": "not_generated", "stage": "rewrite"}
                    else:
                        source_path.write_text(mutated_source, encoding="utf-8")
                        result = run_generated_smoke_test(
                            workdir,
                            prompt_name,
                            f"{item_id}-{key}",
                            timeout,
                            mvn,
                            test_class,
                        )
                    item_result[f"{key}_mutant_status"] = result["status"]
                    item_result[f"{key}_mutant_stage"] = result.get("stage")
                    item_result[f"{key}_mutant_compile_log"] = result.get("compile_log")
                    item_result[f"{key}_mutant_execute_log"] = result.get("execute_log")
                    if result["status"] == "killed":
                        attempt_killed += 1
                    status_counts[result["status"]] += 1
                attempt_represented = bool(attempt_mutants) and all(
                    item_result.get(f"{variant if attempt == 'primary' else f'{attempt}_{variant}'}_mutant_status")
                    in {"killed", "survived"}
                    for variant, _ in attempt_mutants
                )
                return attempt_represented, attempt_killed

            represented, killed_count = run_mutant_attempt("primary", mutants)
            fallback_mutants: list[tuple[str, str | None]] = []
            if not represented:
                fallback_mutants = fallback_mutant_sources_for_item(original_source, item, return_type)
                if fallback_mutants:
                    fallback_represented, fallback_killed = run_mutant_attempt("fallback", fallback_mutants)
                    item_result["primary_represented"] = represented
                    item_result["fallback_attempted"] = True
                    item_result["fallback_represented"] = fallback_represented
                    if fallback_represented:
                        used_attempt = "fallback"
                        mutants = fallback_mutants
                        represented = True
                        killed_count = fallback_killed
                else:
                    item_result["fallback_attempted"] = False
                    item_result["fallback_represented"] = False
            score = killed_count / len(mutants) if represented and mutants else None
            total_variants = len(mutants)
            status_prefix = "" if used_attempt == "primary" else "fallback_"
            represented_variants = sum(
                1
                for variant, _ in mutants
                if item_result.get(f"{status_prefix}{variant}_mutant_status") in {"killed", "survived"}
            )
            item_result["used_attempt"] = used_attempt if represented else None
            item_result["primary_mutant_variants"] = [variant for variant, _ in primary_mutant_sources_for_item(original_source, item)]
            item_result["fallback_mutant_variants"] = [variant for variant, _ in fallback_mutants]
            item_result["mutant_variants"] = [variant for variant, _ in mutants]
            item_result["total_variants"] = total_variants
            item_result["represented_variants"] = represented_variants
            item_result["variant_representability_rate"] = (
                represented_variants / total_variants if total_variants else None
            )
            item_result["score"] = score
            item_result["represented"] = represented
            if not represented:
                item_result["score_reason"] = "cannot_be_mutated_or_non_viable_custom_mutant"
            elif score == 1:
                item_result["score_reason"] = "all_custom_mutants_killed"
            elif score == 0.5:
                item_result["score_reason"] = "half_custom_mutants_killed"
            else:
                item_result["score_reason"] = "no_custom_mutant_killed"
            by_construct[construct]["total"] += 1
            if represented:
                scorable_items += 1
                score_sum += float(score)
                by_construct[construct]["scorable"] += 1
                by_construct[construct]["score_sum"] += float(score)
                by_construct[construct]["represented"] += 1
            if score is not None and score > 0:
                by_construct[construct]["checked"] += 1
            if item_result.get("condition_true_mutant_status") == "killed":
                by_construct[construct]["true_killed"] += 1
            if item_result.get("condition_false_mutant_status") == "killed":
                by_construct[construct]["false_killed"] += 1
            by_construct[construct]["killed_mutants"] += killed_count
            scored_items.append(item_result)
    finally:
        source_path.write_text(original_source, encoding="utf-8")
        run_command(
            [mvn, "-q", "test-compile"],
            workdir,
            workdir / "extreme-condition-mutants" / prompt_name / "restore-original-compile.log",
            timeout,
        )

    return aggregate_scored_items(scored_items, status_counts)


def summarize(rows: list[dict[str, Any]], path: Path) -> None:
    collected = [row for row in rows if (row.get("extreme_condition_mutation") or {}).get("status") == "passed"]
    total = sum(int((row.get("extreme_condition_mutation") or {}).get("total_conditions") or 0) for row in collected)
    scorable = sum(int((row.get("extreme_condition_mutation") or {}).get("scorable_conditions") or 0) for row in collected)
    score_sum = sum(float((row.get("extreme_condition_mutation") or {}).get("score_sum") or 0) for row in collected)
    checked = sum(int((row.get("extreme_condition_mutation") or {}).get("checked_items") or 0) for row in collected)
    fully = sum(int((row.get("extreme_condition_mutation") or {}).get("fully_checked_items") or 0) for row in collected)
    status_counts: Counter[str] = Counter()
    for row in collected:
        status_counts.update((row.get("extreme_condition_mutation") or {}).get("status_counts") or {})

    lines = [
        "# Custom Control-Flow Mutation Summary",
        "",
        f"- Rows with collected evidence: {len(collected)}",
        f"- Custom control-flow items: {total}",
        f"- Scorable custom control-flow items: {scorable}",
        f"- Non-representable custom control-flow items: {total - scorable}",
        f"- Checked custom control-flow items: {checked}",
        f"- Fully checked custom control-flow items: {fully}",
        f"- Average custom control-flow score over scorable items: {score_sum / scorable if scorable else 'N/A'}",
        f"- Custom-mutant representability rate: {scorable / total if total else 'N/A'}",
        "",
        "## Mutant Status Counts",
        "",
        "| Status | Mutants |",
        "| --- | ---: |",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"| {status} | {count} |")
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
    parser.add_argument("--max-conditions-per-row", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--max-items-per-row", type=int, default=None)
    parser.add_argument(
        "--only-construct",
        action="append",
        default=[],
        choices=sorted(CUSTOM_CONTROL_FLOW_CONSTRUCTS),
        help="Only collect custom mutants for this construct; can be repeated.",
    )
    parser.add_argument(
        "--merge-existing-custom",
        action="store_true",
        help="When --only-construct is used on a file with existing custom-control-flow results, replace only those constructs and preserve the rest.",
    )
    parser.add_argument(
        "--skip-task-id",
        action="append",
        default=[],
        help="Skip a known pathological task_id and preserve/write a skipped result row.",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    out = args.out or args.result.with_name(args.result.stem + "-with-extreme-condition.jsonl")
    summary = out.with_name(out.stem + "-summary.md")
    existing = existing_rows(out) if args.resume else {}
    include_prompts = set(args.include_prompt)
    filtered_rows = [
        row
        for row in rows
        if not include_prompts or str(row.get("prompt_template")) in include_prompts
    ]
    processed = 0
    updated: list[dict[str, Any]] = []
    skip_task_ids = set(args.skip_task_id)
    only_constructs = set(args.only_construct)

    for index, row in enumerate(filtered_rows, start=1):
        key = row_key(row)
        existing_mutation = existing.get(key, {}).get("extreme_condition_mutation") or {}
        if (
            args.resume
            and existing_mutation.get("status")
            and existing_mutation.get("collector_version") == CUSTOM_MUTATION_VERSION
        ):
            updated.append(existing[key])
            print(f"[{index}/{len(filtered_rows)}] {row.get('task_id')} {row.get('prompt_template')} skipped=resume")
            continue
        if args.limit is not None and processed >= args.limit:
            updated.append(row)
            continue
        processed += 1
        print(f"[{index}/{len(filtered_rows)}] {row.get('task_id')} {row.get('prompt_template')}")
        if str(row.get("task_id")) in skip_task_ids:
            result = {
                "status": "skipped",
                "collector_version": CUSTOM_MUTATION_VERSION,
                "reason": "skipped_by_user",
                "total_conditions": 0,
                "total_items": 0,
                "scorable_conditions": 0,
                "scorable_items": 0,
                "non_representable_conditions": 0,
                "non_representable_items": 0,
                "attempted_mutants": 0,
                "score_sum": 0.0,
                "score": None,
                "checked_items": 0,
                "fully_checked_items": 0,
                "represented_items": 0,
                "representability_rate": None,
                "status_counts": {},
                "by_construct": {},
                "items": [],
            }
        else:
            max_items = args.max_items_per_row if args.max_items_per_row is not None else args.max_conditions_per_row
            result = collect_row(row, args.timeout, args.mvn, max_items, only_constructs or None)
            if args.merge_existing_custom and only_constructs:
                result = merge_construct_results(row.get("extreme_condition_mutation") or {}, result, only_constructs)
        row = {**row, "extreme_condition_mutation": result}
        updated.append(row)
        display_result = scoped_result_summary(result, only_constructs or None)
        print(
            f"  {result['status']} items={display_result.get('total_items', display_result.get('total_conditions'))} "
            f"score={display_result.get('score')} checked={display_result.get('checked_items')}"
        )
        write_rows_checkpoint(updated, out, summary)

    write_rows_checkpoint(updated, out, summary)
    print(f"wrote {out}")
    print(f"wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
