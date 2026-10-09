#!/usr/bin/env python3
"""Run stronger Java test-generation baselines.

P3: intention-planning pipeline.
P5: mutation-feedback pipeline.

The generated tests follow the repository's existing Java pilot contract:
`src/test/java/benchmark/GeneratedSmokeTest.java` is a plain Java class with a
`main` method that throws `AssertionError` on failure. This keeps the output
compatible with the existing coverage, PIT, and blind-spot analysis scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import APIStatusError, OpenAI

from collect_java_mutation import collect_mutation
from run_java_pilot import (
    DEFAULT_MANIFEST,
    RESULT_ROOT,
    ROOT,
    WORK_ROOT,
    collect_coverage,
    extract_java_code,
    parse_jacoco_xml,
    parse_task_ids,
    prepare_workdir,
    read_completed_keys,
    read_jsonl,
    run_generated_test,
    select_tasks,
)


PROMPT_DIR = ROOT / "prompts"
PLAN_PROMPT = PROMPT_DIR / "java_intention_planning_plan.txt"
GENERATE_PROMPT = PROMPT_DIR / "java_intention_planning_generate.txt"
MUTATION_PROMPT = PROMPT_DIR / "java_mutation_feedback_generate.txt"
REPAIR_PROMPT = PROMPT_DIR / "java_compile_repair.txt"

PIPELINE_NAMES = {
    "p3": "p3-intention-planning",
    "p5": "p5-mutation-feedback",
}

DETERMINISTIC_SCAFFOLD = """package benchmark;

public class GeneratedSmokeTest {
    public static void main(String[] args) throws Exception {
        // Call test helper methods from here.
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    private static void checkEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " expected=" + expected + " actual=" + actual);
        }
    }

    private static void checkThrows(Class<? extends Throwable> expected, ThrowingRunnable action, String message) {
        try {
            action.run();
        } catch (Throwable actual) {
            if (expected.isInstance(actual)) {
                return;
            }
            throw new AssertionError(message + " wrong exception=" + actual);
        }
        throw new AssertionError(message + " expected exception=" + expected.getName());
    }

    private interface ThrowingRunnable {
        void run() throws Exception;
    }
}
"""


class ModelCallError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def metadata_for_prompt(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "symbol_name": task["symbol_name"],
        "signature": task.get("signature"),
        "complexity_bucket": task.get("complexity_bucket"),
        "cyclomatic_complexity": task.get("cyclomatic_complexity"),
        "cognitive_complexity": task.get("cognitive_complexity"),
        "self_containment": task.get("self_containment"),
        "dependency_category": task.get("dependency_category"),
        "repo_name": task.get("repo_name"),
        "source_dataset": task.get("source_dataset"),
    }


def render_template(path: Path, **values: Any) -> str:
    return path.read_text(encoding="utf-8").format(**values)


def java_context_summary(source_code: str, symbol_name: str) -> dict[str, Any]:
    """Extract cheap, prompt-oriented Java context without a heavyweight parser."""

    lines = source_code.splitlines()
    accessible_methods = []
    constructors = []
    fields = []
    branch_conditions = []
    throws = []
    constants = []

    method_pattern = re.compile(
        r"^\s*(public|protected|static|final|synchronized|\s)*(?:[\w<>\[\], ?]+)\s+(\w+)\s*\(([^)]*)\)"
    )
    constructor_pattern = re.compile(r"^\s*(public|protected)?\s*Subject\s*\(([^)]*)\)")
    field_pattern = re.compile(r"^\s*(public|protected|static|final|\s)*(?:[\w<>\[\], ?]+)\s+(\w+)\s*(?:=|;)")
    constant_pattern = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if constructor_pattern.search(line):
            constructors.append({"line": idx, "signature": stripped})
        method_match = method_pattern.search(line)
        if method_match and " private " not in f" {line} ":
            accessible_methods.append({"line": idx, "name": method_match.group(2), "signature": stripped})
        field_match = field_pattern.search(line)
        if field_match and " private " not in f" {line} ":
            fields.append({"line": idx, "name": field_match.group(2), "declaration": stripped})
        if re.search(r"\b(if|else if|switch|case|for|while|catch)\b", line):
            branch_conditions.append({"line": idx, "code": stripped})
        if re.search(r"\bthrow\s+new\b|\bthrows\b", line):
            throws.append({"line": idx, "code": stripped})
        for name in constant_pattern.findall(line):
            if name not in constants:
                constants.append(name)

    focal_lines = []
    capture = False
    brace_balance = 0
    for idx, line in enumerate(lines, start=1):
        if not capture and re.search(rf"\b{re.escape(symbol_name)}\s*\(", line):
            capture = True
        if capture:
            focal_lines.append({"line": idx, "code": line.rstrip()})
            brace_balance += line.count("{") - line.count("}")
            if "{" in line and brace_balance <= 0:
                break
            if len(focal_lines) > 120:
                break

    called_methods = sorted(
        {
            match.group(1)
            for item in focal_lines
            for match in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", item["code"])
        }
    )

    return {
        "focal_symbol": symbol_name,
        "accessible_constructors": constructors[:12],
        "accessible_methods": accessible_methods[:30],
        "accessible_fields": fields[:20],
        "branch_or_guard_lines": branch_conditions[:60],
        "throw_or_exception_lines": throws[:30],
        "named_constants_seen": constants[:40],
        "called_methods_in_focal": called_methods[:40],
        "focal_excerpt": focal_lines[:120],
        "notes": [
            "This is a lightweight regex-based context summary, not a complete Java semantic model.",
            "Private members and private nested classes must still be treated as inaccessible from GeneratedSmokeTest.",
        ],
    }


def build_plan_prompt(task: dict[str, Any], source_code: str) -> str:
    static_context = java_context_summary(source_code, task["symbol_name"])
    return render_template(
        PLAN_PROMPT,
        symbol_name=task["symbol_name"],
        metadata_json=json.dumps(metadata_for_prompt(task), indent=2, ensure_ascii=False),
        static_context_json=json.dumps(static_context, indent=2, ensure_ascii=False),
        deterministic_scaffold=DETERMINISTIC_SCAFFOLD,
        source_code=source_code,
    )


def build_generation_prompt(task: dict[str, Any], source_code: str, intention_plan: str) -> str:
    static_context = java_context_summary(source_code, task["symbol_name"])
    return render_template(
        GENERATE_PROMPT,
        symbol_name=task["symbol_name"],
        metadata_json=json.dumps(metadata_for_prompt(task), indent=2, ensure_ascii=False),
        static_context_json=json.dumps(static_context, indent=2, ensure_ascii=False),
        deterministic_scaffold=DETERMINISTIC_SCAFFOLD,
        intention_plan=intention_plan,
        source_code=source_code,
    )


def build_mutation_feedback_prompt(
    task: dict[str, Any],
    source_code: str,
    intention_plan: str,
    current_test_code: str,
    mutation_feedback: dict[str, Any],
) -> str:
    static_context = java_context_summary(source_code, task["symbol_name"])
    return render_template(
        MUTATION_PROMPT,
        symbol_name=task["symbol_name"],
        metadata_json=json.dumps(metadata_for_prompt(task), indent=2, ensure_ascii=False),
        static_context_json=json.dumps(static_context, indent=2, ensure_ascii=False),
        deterministic_scaffold=DETERMINISTIC_SCAFFOLD,
        intention_plan=intention_plan,
        current_test_code=current_test_code,
        mutation_feedback_json=json.dumps(mutation_feedback, indent=2, ensure_ascii=False),
        source_code=source_code,
    )


def build_repair_prompt(
    task: dict[str, Any],
    source_code: str,
    intention_plan: str,
    current_test_code: str,
    failure_log: str,
) -> str:
    static_context = java_context_summary(source_code, task["symbol_name"])
    return render_template(
        REPAIR_PROMPT,
        symbol_name=task["symbol_name"],
        metadata_json=json.dumps(metadata_for_prompt(task), indent=2, ensure_ascii=False),
        static_context_json=json.dumps(static_context, indent=2, ensure_ascii=False),
        deterministic_scaffold=DETERMINISTIC_SCAFFOLD,
        intention_plan=intention_plan,
        current_test_code=current_test_code,
        failure_log=failure_log,
        source_code=source_code,
    )


def extract_json_object(response_text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", response_text, re.DOTALL | re.I)
    if fenced:
        response_text = fenced.group(1)
    response_text = response_text.strip()
    if response_text.startswith("{") and response_text.endswith("}"):
        return response_text
    start = response_text.find("{")
    end = response_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return response_text[start : end + 1]
    return response_text


def read_failure_log(workdir: Path, prompt_name: str, max_chars: int = 12000) -> str:
    candidates = [
        workdir / f"mvn-test-{prompt_name}.log",
        workdir / f"execute-{prompt_name}.log",
        workdir / f"coverage-test-compile-{prompt_name}.log",
        workdir / f"coverage-execute-{prompt_name}.log",
    ]
    chunks = []
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks.append(f"## {path.name}\n{text[-max_chars:]}")
    return "\n\n".join(chunks)[-max_chars:]


class ModelClient:
    def __init__(
        self,
        *,
        model: str,
        api: str,
        base_url: str | None,
        api_key: str | None,
        temperature: float,
        max_output_tokens: int,
        service_tier: str | None,
        model_timeout: int,
        model_retries: int,
    ) -> None:
        self.model = model
        self.api = api
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.service_tier = service_tier
        self.model_timeout = model_timeout
        self.model_retries = model_retries
        if self.api == "gemini":
            self.client = None
            return
        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        elif base_url:
            kwargs["api_key"] = "local"
        self.client = OpenAI(**kwargs)

    def call(self, *, system: str, prompt: str) -> tuple[str, dict[str, Any]]:
        if self.api == "gemini":
            if not self.base_url:
                raise ValueError("--base-url must be the Gemini generateContent endpoint when --api gemini is used")
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": self.max_output_tokens,
                },
            }
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Ocp-Apim-Subscription-Key"] = self.api_key
            request = urllib.request.Request(
                self.base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            last_error: Exception | None = None
            for attempt in range(self.model_retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=self.model_timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    if exc.code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < self.model_retries:
                        last_error = exc
                        time.sleep(min(2 ** attempt, 10))
                        continue
                    raise ModelCallError(
                        f"Gemini generateContent failed with HTTP {exc.code}: {detail}",
                        status_code=exc.code,
                    ) from exc
                except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                    last_error = exc
                    if attempt < self.model_retries:
                        time.sleep(min(2 ** attempt, 10))
                        continue
                    raise ModelCallError(
                        f"Gemini generateContent request failed after {self.model_retries + 1} attempt(s): {exc}"
                    ) from exc
            else:
                raise ModelCallError(f"Gemini generateContent request failed: {last_error}")
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            text = "".join(part.get("text", "") for part in parts)
            usage = data.get("usageMetadata") or {}
            return text, usage
        if self.api == "responses":
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                service_tier=self.service_tier,
            )
            usage = response.usage.model_dump() if response.usage else {}
            return response.output_text, usage

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
            service_tier=self.service_tier,
        )
        message = response.choices[0].message.content or ""
        usage = response.usage.model_dump() if response.usage else {}
        return message, usage


def categorize_mutant(item: dict[str, Any]) -> str:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("mutator", "description", "mutated_method")
    ).lower()
    if "conditionalsboundary" in text or "changed conditional boundary" in text:
        return "boundary_value"
    if "negateconditionals" in text or "conditional" in text or "switch" in text:
        return "branch_condition"
    if "null" in text:
        return "null_handling"
    if "return" in text or "empty returns" in text or "false returns" in text or "true returns" in text:
        return "weak_oracle_return"
    if "void method call" in text or "removed call" in text:
        return "interaction_or_state"
    if "increments" in text or "math" in text or "arithmetic" in text:
        return "numeric_boundary"
    if "constructor" in text:
        return "construction"
    return "other"


def compact_mutation_feedback(mutation: dict[str, Any], max_mutants: int) -> dict[str, Any]:
    details = mutation.get("mutation_details") or []
    interesting = [
        item
        for item in details
        if item.get("status") in {"SURVIVED", "NO_COVERAGE"}
    ]
    compact = []
    groups: dict[str, dict[str, Any]] = {}
    for item in interesting[:max_mutants]:
        category = categorize_mutant(item)
        entry = {
            "status": item.get("status"),
            "line_number": item.get("line_number"),
            "mutated_method": item.get("mutated_method"),
            "mutator": item.get("mutator"),
            "description": item.get("description"),
            "source_file": item.get("source_file"),
            "category": category,
        }
        compact.append(entry)
        group = groups.setdefault(category, {"count": 0, "examples": []})
        group["count"] += 1
        if len(group["examples"]) < 3:
            group["examples"].append(entry)
    return {
        "mutation_status": mutation.get("mutation_status"),
        "mutation_score": mutation.get("mutation_score"),
        "mutants_total": mutation.get("mutants_total"),
        "mutants_killed": mutation.get("mutants_killed"),
        "mutants_survived": mutation.get("mutants_survived"),
        "mutants_no_coverage": mutation.get("mutants_no_coverage"),
        "grouped_by_behavior_hint": groups,
        "surviving_or_uncovered_mutants": compact,
        "repair_instruction": "Generate targeted assertions/inputs for the grouped surviving or uncovered mutants; do not merely repeat calls.",
        "truncated": len(interesting) > len(compact),
    }


def write_test(workdir: Path, test_code: str) -> None:
    test_path = workdir / "src/test/java/benchmark/GeneratedSmokeTest.java"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test_code, encoding="utf-8")


def evaluate(workdir: Path, prompt_name: str, mutation_timeout: int, run_mutation: bool) -> dict[str, Any]:
    outcome = run_generated_test(workdir, prompt_name)
    coverage_outcome = (
        collect_coverage(workdir, prompt_name)
        if outcome["execution_passed"]
        else {
            "coverage_status": "skipped",
            "coverage_error_stage": "test_not_executed",
            "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml"),
        }
    )
    if run_mutation and outcome["execution_passed"]:
        mutation = collect_mutation(workdir, prompt_name, mutation_timeout)
    else:
        mutation = {
            "mutation_status": "skipped",
            "mutation_error_stage": None if not run_mutation else "test_not_executed",
            "mutation_score": None,
            "mutation_details": [],
            "mutation_exit_code": None,
            "mutation_log": None,
            "pit_summary": {},
            "mutants_total": 0,
            "mutants_killed": 0,
            "mutants_survived": 0,
            "mutants_no_coverage": 0,
            "mutants_timed_out": 0,
            "mutants_non_viable": 0,
            "mutants_memory_error": 0,
            "mutants_run_error": 0,
        }
    return {**outcome, **coverage_outcome, **mutation}


def repair_once_if_needed(
    *,
    client: ModelClient | None,
    task: dict[str, Any],
    source_code: str,
    workdir: Path,
    prompt_name: str,
    intention_plan: str,
    test_code: str,
    dry_run: bool,
    enabled: bool,
) -> dict[str, Any]:
    precheck = run_generated_test(workdir, prompt_name + "-repair-precheck")
    if precheck["compile_passed"] and precheck["execution_passed"]:
        return {
            "compile_repair_enabled": enabled,
            "compile_repair_used": False,
            "compile_repair_precheck": precheck,
            "compile_repair_prompt": None,
            "raw_compile_repair_response": None,
            "compile_repair_usage": {},
            "post_repair_test_code": test_code,
        }
    if not enabled or dry_run:
        return {
            "compile_repair_enabled": enabled,
            "compile_repair_used": False,
            "compile_repair_precheck": precheck,
            "compile_repair_prompt": None,
            "raw_compile_repair_response": None,
            "compile_repair_usage": {},
            "post_repair_test_code": test_code,
        }

    assert client is not None
    failure_log = read_failure_log(workdir, prompt_name + "-repair-precheck")
    repair_prompt = build_repair_prompt(
        task=task,
        source_code=source_code,
        intention_plan=intention_plan,
        current_test_code=test_code,
        failure_log=failure_log,
    )
    raw_repair, repair_usage = client.call(
        system="You repair Java tests using compiler/runtime feedback. Return only Java code.",
        prompt=repair_prompt,
    )
    repaired_code = extract_java_code(raw_repair)
    write_test(workdir, repaired_code)
    postcheck = run_generated_test(workdir, prompt_name + "-repair-postcheck")
    return {
        "compile_repair_enabled": enabled,
        "compile_repair_used": True,
        "compile_repair_precheck": precheck,
        "compile_repair_postcheck": postcheck,
        "compile_repair_prompt": repair_prompt,
        "raw_compile_repair_response": raw_repair,
        "compile_repair_usage": repair_usage,
        "post_repair_test_code": repaired_code,
    }


def p3_generate(
    *,
    client: ModelClient | None,
    task: dict[str, Any],
    source_code: str,
    dry_run: bool,
) -> dict[str, Any]:
    plan_prompt = build_plan_prompt(task, source_code)
    generation_usage: dict[str, Any] = {}
    if dry_run:
        raw_plan = json.dumps(
            {
                "target_summary": "dry-run placeholder",
                "observability_notes": "uses existing benchmark smoke test",
                "intentions": [],
            },
            indent=2,
        )
        intention_plan = raw_plan
        raw_generation = (Path(task["task_dir"]) / "src/test/java/benchmark/GeneratedSmokeTest.java").read_text(
            encoding="utf-8"
        )
        test_code = raw_generation
        plan_usage: dict[str, Any] = {"dry_run": True}
    else:
        assert client is not None
        raw_plan, plan_usage = client.call(
            system="You design precise, concise Java unit-test intentions. Return only JSON.",
            prompt=plan_prompt,
        )
        intention_plan = extract_json_object(raw_plan)
        generation_prompt = build_generation_prompt(task, source_code, intention_plan)
        raw_generation, generation_usage = client.call(
            system="You generate concise, executable Java tests. Return only Java code.",
            prompt=generation_prompt,
        )
        test_code = extract_java_code(raw_generation)

    return {
        "plan_prompt": plan_prompt,
        "raw_intention_plan_response": raw_plan,
        "intention_plan": intention_plan,
        "plan_usage": plan_usage,
        "generation_prompt": build_generation_prompt(task, source_code, intention_plan),
        "raw_response": raw_generation,
        "extracted_test_code": test_code,
        "generation_usage": generation_usage,
    }


def run_pipeline(
    *,
    pipeline: str,
    client: ModelClient | None,
    task: dict[str, Any],
    run_id: str,
    dry_run: bool,
    mutation_timeout: int,
    max_feedback_mutants: int,
    skip_mutation: bool,
    resume: bool,
    compile_repair: bool,
) -> dict[str, Any]:
    prompt_name = PIPELINE_NAMES[pipeline]
    workdir = prepare_workdir(task, run_id, prompt_name, keep_existing=resume)
    source_code = (workdir / "src/main/java/benchmark/Subject.java").read_text(encoding="utf-8")

    p3 = p3_generate(client=client, task=task, source_code=source_code, dry_run=dry_run)
    write_test(workdir, p3["extracted_test_code"])
    repair = repair_once_if_needed(
        client=client,
        task=task,
        source_code=source_code,
        workdir=workdir,
        prompt_name=prompt_name,
        intention_plan=p3["intention_plan"],
        test_code=p3["extracted_test_code"],
        dry_run=dry_run,
        enabled=compile_repair,
    )
    current_test_code = repair["post_repair_test_code"]

    if pipeline == "p3":
        evaluation = evaluate(workdir, prompt_name, mutation_timeout, run_mutation=not skip_mutation)
        return {
            "workdir": str(workdir),
            "prompt_template": prompt_name,
            "pipeline_stage": "final",
            **p3,
            **repair,
            "extracted_test_code": current_test_code,
            **evaluation,
        }

    initial_eval = evaluate(workdir, prompt_name + "-initial", mutation_timeout, run_mutation=not skip_mutation)
    feedback = compact_mutation_feedback(initial_eval, max_feedback_mutants)
    has_feedback = bool(feedback["surviving_or_uncovered_mutants"])
    final_generation_usage: dict[str, Any] = {}
    final_prompt = None
    raw_final = None
    final_test_code = current_test_code

    if has_feedback and not dry_run and not skip_mutation:
        assert client is not None
        final_prompt = build_mutation_feedback_prompt(
            task=task,
            source_code=source_code,
            intention_plan=p3["intention_plan"],
            current_test_code=current_test_code,
            mutation_feedback=feedback,
        )
        raw_final, final_generation_usage = client.call(
            system="You improve Java tests using mutation feedback. Return only Java code.",
            prompt=final_prompt,
        )
        final_test_code = extract_java_code(raw_final)
        write_test(workdir, final_test_code)
        feedback_repair = repair_once_if_needed(
            client=client,
            task=task,
            source_code=source_code,
            workdir=workdir,
            prompt_name=prompt_name + "-feedback",
            intention_plan=p3["intention_plan"],
            test_code=final_test_code,
            dry_run=dry_run,
            enabled=compile_repair,
        )
        final_test_code = feedback_repair["post_repair_test_code"]
    else:
        feedback_repair = {
            "compile_repair_enabled": compile_repair,
            "compile_repair_used": False,
            "compile_repair_prompt": None,
            "raw_compile_repair_response": None,
            "compile_repair_usage": {},
            "post_repair_test_code": final_test_code,
        }

    final_eval = evaluate(workdir, prompt_name, mutation_timeout, run_mutation=not skip_mutation)
    return {
        "workdir": str(workdir),
        "prompt_template": prompt_name,
        "pipeline_stage": "final",
        **p3,
        **repair,
        "initial_test_code": current_test_code,
        "initial_evaluation": initial_eval,
        "mutation_feedback": feedback,
        "mutation_feedback_used": has_feedback and not dry_run and not skip_mutation,
        "mutation_feedback_prompt": final_prompt,
        "raw_mutation_feedback_response": raw_final,
        "mutation_feedback_usage": final_generation_usage,
        "mutation_feedback_repair": feedback_repair,
        "extracted_test_code": final_test_code,
        **final_eval,
    }


def pipeline_sequence(value: str) -> list[str]:
    if value == "both":
        return ["p3", "p5"]
    return [value]


def model_error_record(error: Exception, prompt_name: str) -> dict[str, Any]:
    status_code = getattr(error, "status_code", None)
    error_stage = f"model_error_{status_code}" if status_code else "model_error"
    return {
        "workdir": None,
        "prompt_template": prompt_name,
        "pipeline_stage": "model_error",
        "compile_passed": False,
        "execution_passed": False,
        "error_stage": error_stage,
        "coverage_status": "skipped",
        "coverage_error_stage": error_stage,
        "coverage": {},
        "mutation_status": "skipped",
        "mutation_error_stage": error_stage,
        "mutation_score": None,
        "mutation_details": [],
        "mutation_exit_code": None,
        "mutation_log": None,
        "pit_summary": {},
        "mutants_total": 0,
        "mutants_killed": 0,
        "mutants_survived": 0,
        "mutants_no_coverage": 0,
        "mutants_timed_out": 0,
        "mutants_non_viable": 0,
        "mutants_memory_error": 0,
        "mutants_run_error": 0,
        "model_error_type": type(error).__name__,
        "model_error_status_code": status_code,
        "model_error_message": str(error),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--task-ids", default=None)
    parser.add_argument("--pipeline", choices=["p3", "p5", "both"], default="both")
    parser.add_argument("--model", default=os.environ.get("PILOT_MODEL", "gpt-5.4-mini"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-output-tokens", type=int, default=5000)
    parser.add_argument("--model-timeout", type=int, default=300, help="Per-request model API timeout in seconds.")
    parser.add_argument("--model-retries", type=int, default=2, help="Retry count for transient Gemini API failures.")
    parser.add_argument(
        "--api",
        choices=["responses", "chat", "gemini"],
        default=os.environ.get("PILOT_API", "responses"),
        help="Use OpenAI Responses API, OpenAI-compatible chat completions, or Gemini generateContent REST.",
    )
    parser.add_argument("--base-url", default=os.environ.get("PILOT_BASE_URL"))
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable containing the API key. For local OpenAI-compatible servers, omit it or set any value.",
    )
    parser.add_argument(
        "--service-tier",
        choices=["auto", "default", "flex", "priority"],
        default=os.environ.get("PILOT_SERVICE_TIER"),
        help="Optional OpenAI service tier. Use 'flex' for Flex processing. Leave unset for local/proxy endpoints.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Use existing smoke tests instead of calling an LLM.")
    parser.add_argument("--run-id", default=None, help="Use a fixed run id, useful for resuming an interrupted run.")
    parser.add_argument("--resume", action="store_true", help="Skip rows already present in the result file.")
    parser.add_argument("--mutation-timeout", type=int, default=240)
    parser.add_argument("--max-feedback-mutants", type=int, default=20)
    parser.add_argument("--skip-mutation", action="store_true", help="Skip PIT. P5 will not perform feedback when set.")
    parser.add_argument(
        "--no-compile-repair",
        action="store_true",
        help="Disable the one-round compile/execution repair step.",
    )
    args = parser.parse_args()

    tasks = select_tasks(args.manifest, args.limit, parse_task_ids(args.task_ids))
    if not tasks:
        raise SystemExit("No tasks selected.")

    api_key = os.environ.get(args.api_key_env)
    client = None
    if not args.dry_run:
        client = ModelClient(
            model=args.model,
            api=args.api,
            base_url=args.base_url,
            api_key=api_key,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            service_tier=args.service_tier,
            model_timeout=args.model_timeout,
            model_retries=args.model_retries,
        )

    run_id = args.run_id or f"java-sota-{args.model.replace('/', '_')}-{uuid.uuid4().hex[:8]}"
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    results_path = RESULT_ROOT / f"{run_id}.jsonl"
    completed = read_completed_keys(results_path) if args.resume else set()
    print(f"run_id={run_id}")
    print(f"results={results_path}")
    if args.base_url:
        print(f"base_url={args.base_url} api={args.api}")
    if args.resume:
        print(f"resume=true completed_rows={len(completed)}")

    for task in tasks:
        for pipeline in pipeline_sequence(args.pipeline):
            prompt_name = PIPELINE_NAMES[pipeline]
            if (task["task_id"], prompt_name) in completed:
                print(f"task={task['task_id']} pipeline={pipeline} skipped=already_completed")
                continue
            print(f"task={task['task_id']} pipeline={pipeline}")
            try:
                record_body = run_pipeline(
                    pipeline=pipeline,
                    client=client,
                    task=task,
                    run_id=run_id,
                    dry_run=args.dry_run,
                    mutation_timeout=args.mutation_timeout,
                    max_feedback_mutants=args.max_feedback_mutants,
                    skip_mutation=args.skip_mutation,
                    resume=args.resume,
                    compile_repair=not args.no_compile_repair,
                )
            except (APIStatusError, ModelCallError) as exc:
                record_body = model_error_record(exc, prompt_name)
            record = {
                "run_id": run_id,
                "task_id": task["task_id"],
                "language": "java",
                "model_id": args.model,
                "api": args.api,
                "base_url": args.base_url,
                "service_tier": args.service_tier,
                "temperature": args.temperature,
                "dry_run": args.dry_run,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "task": task,
                **record_body,
            }
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"  compile={record.get('compile_passed')} "
                f"execute={record.get('execution_passed')} "
                f"mutation={record.get('mutation_status')} "
                f"score={record.get('mutation_score')}"
            )

    print(f"wrote {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
