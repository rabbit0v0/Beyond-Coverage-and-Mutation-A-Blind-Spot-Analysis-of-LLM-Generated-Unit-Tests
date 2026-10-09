#!/usr/bin/env python3
"""Run PIT mutation testing for successful Java pilot rows."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PIT_VERSION = "1.20.3"
JUNIT_VERSION = "4.13.2"
ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("task_id")), str(row.get("prompt_template")))


def existing_mutation_rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    return {row_key(row): row for row in read_jsonl(path)}


def run_command(args: list[str], cwd: Path, log_path: Path, timeout: int) -> subprocess.CompletedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    log_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    return result


def ensure_junit_wrapper(workdir: Path) -> None:
    wrapper = workdir / "src/test/java/benchmark/GeneratedPitTest.java"
    wrapper.write_text(
        """package benchmark;

import org.junit.Test;

public class GeneratedPitTest {
    @Test
    public void generatedSmokeTestPasses() throws Exception {
        GeneratedSmokeTest.main(new String[0]);
    }
}
""",
        encoding="utf-8",
    )


def ensure_pom_config(workdir: Path) -> None:
    pom = workdir / "pom.xml"
    text = pom.read_text(encoding="utf-8")

    if "<dependencies>" not in text:
        text = text.replace("</project>", "<dependencies></dependencies></project>")
    if "junit:junit" not in text and "<artifactId>junit</artifactId>" not in text:
        junit_dep = f"""
<dependency>
  <groupId>junit</groupId>
  <artifactId>junit</artifactId>
  <version>{JUNIT_VERSION}</version>
  <scope>test</scope>
</dependency>"""
        text = text.replace("</dependencies>", junit_dep + "\n</dependencies>")

    if "<build>" not in text:
        text = text.replace("</project>", "<build><plugins></plugins></build></project>")
    elif "<plugins>" not in text:
        text = text.replace("</build>", "<plugins></plugins></build>")

    if "pitest-maven" not in text:
        pit_plugin = f"""
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>{PIT_VERSION}</version>
  <configuration>
    <targetClasses>
      <param>benchmark.Subject</param>
    </targetClasses>
    <targetTests>
      <param>benchmark.GeneratedPitTest</param>
    </targetTests>
    <outputFormats>
      <param>XML</param>
    </outputFormats>
    <timestampedReports>false</timestampedReports>
  </configuration>
</plugin>"""
        text = text.replace("</plugins>", pit_plugin + "\n</plugins>")

    pom.write_text(text, encoding="utf-8")


def parse_mutations_xml(path: Path) -> dict[str, Any]:
    empty = {
        "mutation_status": "missing_report",
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
    if not path.exists():
        return empty

    counts = {
        "KILLED": 0,
        "SURVIVED": 0,
        "NO_COVERAGE": 0,
        "TIMED_OUT": 0,
        "NON_VIABLE": 0,
        "MEMORY_ERROR": 0,
        "RUN_ERROR": 0,
    }
    root = ET.parse(path).getroot()
    details: list[dict[str, Any]] = []
    for mutation in root.findall("mutation"):
        status = mutation.attrib.get("status", "")
        if status in counts:
            counts[status] += 1
        details.append(
            {
                "status": status,
                "detected": mutation.attrib.get("detected"),
                "number_of_tests_run": mutation.attrib.get("numberOfTestsRun"),
                "source_file": mutation.findtext("sourceFile"),
                "mutated_class": mutation.findtext("mutatedClass"),
                "mutated_method": mutation.findtext("mutatedMethod"),
                "method_description": mutation.findtext("methodDescription"),
                "line_number": int(mutation.findtext("lineNumber") or 0) or None,
                "mutator": mutation.findtext("mutator"),
                "indexes": mutation.findtext("indexes"),
                "blocks": mutation.findtext("blocks"),
                "killing_test": mutation.findtext("killingTest"),
                "description": mutation.findtext("description"),
            }
        )

    total = sum(counts.values())
    killed = counts["KILLED"]
    detected_denominator = counts["KILLED"] + counts["SURVIVED"] + counts["NO_COVERAGE"]
    score = None if detected_denominator == 0 else killed / detected_denominator
    status = "no_mutants" if total == 0 else "passed"
    return {
        "mutation_status": status,
        "mutation_score": score,
        "mutation_details": details,
        "mutants_total": total,
        "mutants_killed": counts["KILLED"],
        "mutants_survived": counts["SURVIVED"],
        "mutants_no_coverage": counts["NO_COVERAGE"],
        "mutants_timed_out": counts["TIMED_OUT"],
        "mutants_non_viable": counts["NON_VIABLE"],
        "mutants_memory_error": counts["MEMORY_ERROR"],
        "mutants_run_error": counts["RUN_ERROR"],
    }


def parse_pit_summary(log_text: str) -> dict[str, str | None]:
    useful = {
        "line_coverage": None,
        "mutation_coverage": None,
        "test_strength": None,
    }
    patterns = {
        "line_coverage": r"Line Coverage\s*:\s*([^\n\r]+)",
        "mutation_coverage": r"Mutation Coverage\s*:\s*([^\n\r]+)",
        "test_strength": r"Test strength\s*:\s*([^\n\r]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, log_text)
        if match:
            useful[key] = match.group(1).strip()
    return useful


def collect_mutation(workdir: Path, prompt_name: str, timeout: int) -> dict[str, Any]:
    ensure_junit_wrapper(workdir)
    ensure_pom_config(workdir)

    compile_log = workdir / f"pit-test-compile-{prompt_name}.log"
    compile_result = run_command(["mvn", "-q", "test-compile"], workdir, compile_log, timeout=timeout)
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

    log_path = workdir / f"pit-{prompt_name}.log"
    result = run_command(
        [
            "mvn",
            "-q",
            "org.pitest:pitest-maven:mutationCoverage",
            "-DtargetClasses=benchmark.Subject",
            "-DtargetTests=benchmark.GeneratedPitTest",
            "-DoutputFormats=XML",
            "-DtimestampedReports=false",
        ],
        workdir,
        log_path,
        timeout=timeout,
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


def summarize(rows: list[dict[str, Any]], out: Path) -> None:
    successful = [row for row in rows if row.get("mutation_status") == "passed" and row.get("mutation_score") is not None]
    weighted_denominator = sum(
        (row.get("mutants_killed") or 0)
        + (row.get("mutants_survived") or 0)
        + (row.get("mutants_no_coverage") or 0)
        for row in successful
    )
    weighted_numerator = sum(row.get("mutants_killed") or 0 for row in successful)
    lines = [
        "# Java PIT Mutation Summary",
        "",
        f"- rows: {len(rows)}",
        f"- PIT passed with score: {len(successful)}",
        f"- skipped: {sum(1 for row in rows if row.get('mutation_status') == 'skipped')}",
        f"- PIT failed: {sum(1 for row in rows if row.get('mutation_status') == 'failed')}",
    ]
    if successful:
        avg = sum(row["mutation_score"] for row in successful) / len(successful)
        weighted = weighted_numerator / weighted_denominator if weighted_denominator else None
        lines.append(f"- average mutation score, per row: {avg:.4f}")
        lines.append(f"- weighted mutation score, by mutant count: {weighted:.4f}" if weighted is not None else "- weighted mutation score, by mutant count: ")

    def add_group(title: str, key: str) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in successful:
            value = row.get(key)
            if value is None and key == "complexity_bucket":
                value = row.get("task", {}).get("complexity_bucket")
            groups.setdefault(str(value), []).append(row)
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Group | Count | Avg Mutation | Weighted Mutation | Avg Line Coverage | Avg Branch Coverage |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for group, group_rows in sorted(groups.items()):
            avg_mut = sum(row["mutation_score"] for row in group_rows) / len(group_rows)
            group_denominator = sum(
                (row.get("mutants_killed") or 0)
                + (row.get("mutants_survived") or 0)
                + (row.get("mutants_no_coverage") or 0)
                for row in group_rows
            )
            group_numerator = sum(row.get("mutants_killed") or 0 for row in group_rows)
            weighted_mut = group_numerator / group_denominator if group_denominator else None
            cov_rows = [row for row in group_rows if row.get("coverage")]
            line_values = [row["coverage"].get("line_rate") for row in cov_rows if row["coverage"].get("line_rate") is not None]
            branch_values = [row["coverage"].get("branch_rate") for row in cov_rows if row["coverage"].get("branch_rate") is not None]
            avg_line = sum(line_values) / len(line_values) if line_values else None
            avg_branch = sum(branch_values) / len(branch_values) if branch_values else None
            lines.append(
                f"| {group} | {len(group_rows)} | {avg_mut:.4f} | "
                f"{'' if weighted_mut is None else f'{weighted_mut:.4f}'} | "
                f"{'' if avg_line is None else f'{avg_line:.4f}'} | "
                f"{'' if avg_branch is None else f'{avg_branch:.4f}'} |"
            )

    add_group("By Prompt", "prompt_template")
    add_group("By Complexity Bucket", "complexity_bucket")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--include-failed", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse rows already present in the mutation output file.")
    parser.add_argument(
        "--hydrate-existing-reports",
        action="store_true",
        help="Parse existing target/pit-reports/mutations.xml from each workdir instead of rerunning PIT when available.",
    )
    parser.add_argument(
        "--hydrate-only",
        action="store_true",
        help="With --hydrate-existing-reports, do not rerun PIT when a report is missing; keep existing aggregate mutation fields.",
    )
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    out = args.out or args.result.with_name(args.result.stem + "-with-mutation.jsonl")
    summary = out.with_name(out.stem + "-summary.md")
    existing = existing_mutation_rows(out) if args.resume else {}
    updated = []

    for index, row in enumerate(rows, start=1):
        key = row_key(row)
        if key in existing:
            updated.append(existing[key])
            print(f"[{index}/{len(rows)}] {row['task_id']} {row['prompt_template']} skipped=already_mutated")
            continue
        workdir = Path(row["workdir"])
        if not workdir.is_absolute():
            workdir = ROOT / workdir
        prompt_name = row["prompt_template"]
        print(f"[{index}/{len(rows)}] {row['task_id']} {prompt_name}")
        existing_report = workdir / "target/pit-reports/mutations.xml"
        if args.hydrate_existing_reports and existing_report.exists():
            mutation = {
                **parse_mutations_xml(existing_report),
                "mutation_error_stage": None,
                "mutation_exit_code": row.get("mutation_exit_code"),
                "mutation_log": row.get("mutation_log"),
                "pit_summary": row.get("pit_summary") or {},
            }
        elif args.hydrate_existing_reports and args.hydrate_only:
            mutation = {
                "mutation_status": row.get("mutation_status") or "missing_report",
                "mutation_error_stage": row.get("mutation_error_stage"),
                "mutation_score": row.get("mutation_score"),
                "mutation_details": row.get("mutation_details") or [],
                "mutation_exit_code": row.get("mutation_exit_code"),
                "mutation_log": row.get("mutation_log"),
                "pit_summary": row.get("pit_summary") or {},
                "mutants_total": row.get("mutants_total") or 0,
                "mutants_killed": row.get("mutants_killed") or 0,
                "mutants_survived": row.get("mutants_survived") or 0,
                "mutants_no_coverage": row.get("mutants_no_coverage") or 0,
                "mutants_timed_out": row.get("mutants_timed_out") or 0,
                "mutants_non_viable": row.get("mutants_non_viable") or 0,
                "mutants_memory_error": row.get("mutants_memory_error") or 0,
                "mutants_run_error": row.get("mutants_run_error") or 0,
            }
        elif row.get("execution_passed") or args.include_failed:
            mutation = collect_mutation(workdir, prompt_name, args.timeout)
        else:
            mutation = {
                "mutation_status": "skipped",
                "mutation_error_stage": "test_not_executed",
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
        row = {**row, **mutation}
        updated.append(row)
        print(f"  {row['mutation_status']} score={row.get('mutation_score')} mutants={row.get('mutants_total')}")

    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in updated), encoding="utf-8")
    summarize(updated, summary)
    print(f"wrote {out}")
    print(f"wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
