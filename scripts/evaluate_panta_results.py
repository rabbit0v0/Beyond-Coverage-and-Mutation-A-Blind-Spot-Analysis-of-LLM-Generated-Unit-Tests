#!/usr/bin/env python3
"""Evaluate PANTA-generated JUnit tests on Java benchmark tasks."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from collect_java_mutation import parse_mutations_xml, parse_pit_summary
from run_java_pilot import parse_jacoco_xml

PITEST_PLUGIN = """
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.25.9</version>
  <dependencies>
    <dependency>
      <groupId>org.pitest</groupId>
      <artifactId>pitest-junit5-plugin</artifactId>
      <version>1.2.3</version>
    </dependency>
  </dependencies>
</plugin>
"""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def timeout_output(exc: subprocess.TimeoutExpired) -> str:
    output = exc.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output + f"\n[TIMEOUT] command timed out after {exc.timeout} seconds\n"


def run_command(args: list[str], cwd: Path, log_path: Path, timeout: int) -> subprocess.CompletedProcess:
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
        result = subprocess.CompletedProcess(args, returncode=124, stdout=timeout_output(exc), stderr=None)
    log_path.write_text(result.stdout or "", encoding="utf-8", errors="replace")
    return result


def ensure_pitest_junit5_plugin(workdir: Path) -> bool:
    pom = workdir / "pom.xml"
    if not pom.exists():
        return False
    text = pom.read_text(encoding="utf-8")
    if "pitest-junit5-plugin" in text:
        return False
    if "<build>" not in text:
        text = text.replace("</project>", "<build><plugins></plugins></build></project>")
    elif "<plugins>" not in text:
        text = text.replace("</build>", "<plugins></plugins></build>")
    text = text.replace("</plugins>", PITEST_PLUGIN + "\n</plugins>")
    pom.write_text(text, encoding="utf-8")
    return True


def generated_test_stats(test_path: Path) -> dict[str, Any]:
    if not test_path.exists():
        return {
            "panta_test_status": "missing_test_file",
            "panta_test_bytes": 0,
            "panta_test_method_count": 0,
            "panta_generated_method_count": 0,
            "panta_test_preview": "",
        }
    text = test_path.read_text(encoding="utf-8", errors="replace")
    method_count = len(re.findall(r"@\s*Test\b", text))
    placeholder_count = len(re.findall(r"\btestPlaceHolder\s*\(", text))
    generated_count = max(0, method_count - placeholder_count)
    status = "generated_tests" if generated_count > 0 else "no_generated_tests"
    return {
        "panta_test_status": status,
        "panta_test_bytes": test_path.stat().st_size,
        "panta_test_method_count": method_count,
        "panta_generated_method_count": generated_count,
        "panta_test_preview": text[:4000],
    }


def summarize_surefire_reports(workdir: Path) -> dict[str, Any]:
    report_dir = workdir / "target/surefire-reports"
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not report_dir.exists():
        return totals
    for path in report_dir.glob("TEST-*.xml"):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for key in totals:
            totals[key] += int(root.attrib.get(key, 0))
    return totals


def evaluate_execution(workdir: Path, prompt_name: str, timeout: int) -> dict[str, Any]:
    log_path = workdir / f"panta-mvn-test-{prompt_name}.log"
    result = run_command(["mvn", "-q", "test", "jacoco:report", "-Dtest=PantaGeneratedTest"], workdir, log_path, timeout)
    return {
        "compile_passed": result.returncode == 0,
        "execution_passed": result.returncode == 0,
        "error_stage": None if result.returncode == 0 else ("timeout" if result.returncode == 124 else "test"),
        "test_exit_code": result.returncode,
        "test_log": str(log_path),
        "surefire": summarize_surefire_reports(workdir),
    }


def collect_panta_coverage(workdir: Path, prompt_name: str, timeout: int) -> dict[str, Any]:
    log_path = workdir / f"panta-jacoco-report-{prompt_name}.log"
    result = run_command(["mvn", "-q", "jacoco:report"], workdir, log_path, timeout)
    status = "passed" if result.returncode == 0 else "failed"
    return {
        "coverage_status": status,
        "coverage_error_stage": None if status == "passed" else ("report-timeout" if result.returncode == 124 else "report"),
        "coverage": parse_jacoco_xml(workdir / "target/jacoco/jacoco.xml"),
        "coverage_log": str(log_path),
    }


def collect_panta_mutation(workdir: Path, prompt_name: str, timeout: int) -> dict[str, Any]:
    ensure_pitest_junit5_plugin(workdir)
    compile_log = workdir / f"panta-pit-test-compile-{prompt_name}.log"
    compile_result = run_command(["mvn", "-q", "test-compile"], workdir, compile_log, timeout)
    if compile_result.returncode != 0:
        return {
            "mutation_status": "failed",
            "mutation_error_stage": "test-compile",
            "mutation_score": None,
            "mutation_details": [],
            "mutation_exit_code": compile_result.returncode,
            "mutation_log": str(compile_log),
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

    log_path = workdir / f"panta-pit-{prompt_name}.log"
    result = run_command(
        [
            "mvn",
            "-q",
            "org.pitest:pitest-maven:mutationCoverage",
            "-DtargetClasses=benchmark.Subject",
            "-DtargetTests=benchmark.PantaGeneratedTest",
            "-DoutputFormats=XML",
            "-DtimestampedReports=false",
        ],
        workdir,
        log_path,
        timeout,
    )
    report = parse_mutations_xml(workdir / "target/pit-reports/mutations.xml")
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    status = report["mutation_status"] if result.returncode == 0 else "failed"
    return {
        **report,
        "mutation_status": status,
        "mutation_error_stage": None if status == "passed" else status,
        "mutation_exit_code": result.returncode,
        "mutation_log": str(log_path),
        "pit_summary": parse_pit_summary(log_text),
    }


def existing_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(row.get("task_id")) for row in read_jsonl(path) if row.get("task_id")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path, help="PANTA generation JSONL file.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--mutation-timeout", type=int, default=600)
    parser.add_argument("--skip-mutation", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    out = args.out or args.result.with_name(args.result.stem + "-evaluated.jsonl")
    completed = existing_keys(out) if args.resume else set()

    for index, row in enumerate(rows, start=1):
        task_id = row["task_id"]
        if task_id in completed:
            print(f"[{index}/{len(rows)}] {task_id} skipped=already_evaluated")
            continue

        workdir = Path(row["workdir"])
        test_file_info = row.get("test_file") or {}
        test_path = Path(test_file_info.get("path") or workdir / "src/test/java/benchmark/PantaGeneratedTest.java")
        stats = generated_test_stats(test_path)
        prompt_name = "panta-" + str(row.get("model_id", "model")).replace("/", "_").replace(":", "_")

        print(f"[{index}/{len(rows)}] {task_id} {stats['panta_test_status']}")
        if stats["panta_generated_method_count"] == 0:
            evaluated = {
                **row,
                "prompt_template": "panta-control",
                **stats,
                "compile_passed": False,
                "execution_passed": False,
                "error_stage": "no_generated_tests",
                "coverage_status": "skipped",
                "coverage_error_stage": "no_generated_tests",
                "coverage": parse_jacoco_xml(workdir / "target/jacoco/jacoco.xml"),
                "mutation_status": "skipped",
                "mutation_error_stage": "no_generated_tests",
                "mutation_score": None,
                "mutation_details": [],
                "mutants_total": 0,
                "mutants_killed": 0,
                "mutants_survived": 0,
                "mutants_no_coverage": 0,
                "mutants_timed_out": 0,
                "mutants_non_viable": 0,
                "mutants_memory_error": 0,
                "mutants_run_error": 0,
            }
        else:
            execution = evaluate_execution(workdir, prompt_name, args.timeout)
            coverage = collect_panta_coverage(workdir, prompt_name, args.timeout) if execution["execution_passed"] else {
                "coverage_status": "skipped",
                "coverage_error_stage": "test_not_executed",
                "coverage": parse_jacoco_xml(workdir / "target/jacoco/jacoco.xml"),
                "coverage_log": None,
            }
            if execution["execution_passed"] and not args.skip_mutation:
                mutation = collect_panta_mutation(workdir, prompt_name, args.mutation_timeout)
            else:
                mutation = {
                    "mutation_status": "skipped",
                    "mutation_error_stage": "test_not_executed" if not args.skip_mutation else "disabled",
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
            evaluated = {
                **row,
                "prompt_template": "panta-control",
                **stats,
                **execution,
                **coverage,
                **mutation,
            }

        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(evaluated, ensure_ascii=False) + "\n")
        print(
            f"  exec={evaluated.get('execution_passed')} "
            f"coverage={evaluated.get('coverage_status')} "
            f"mutation={evaluated.get('mutation_status')} "
            f"score={evaluated.get('mutation_score')}"
        )

    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
