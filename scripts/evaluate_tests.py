#!/usr/bin/env python3
"""Evaluate an existing test class in an isolated ObligBench-format project."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import analyze_java_blindspots as analyzer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--test-class", required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("evaluation-output"))
    parser.add_argument("--mvn", default="mvn")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--mutation", action="store_true", help="Collect custom control-flow and assertion mutations.")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.mutation:
        from collect_java_assertion_mutation import JACOCO_AGENT
        if not JACOCO_AGENT.is_file():
            parser.error("JaCoCo agent is missing; fetch org.jacoco:org.jacoco.agent:0.8.14:jar:runtime with Maven or set JACOCO_AGENT")
    project = args.project.resolve()
    output = args.out_dir.resolve()
    source = project / "src/main/java/benchmark/Subject.java"
    if not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", args.test_class):
        parser.error("--test-class must be a fully qualified Java class name")
    test_rel = Path("src/test/java", *args.test_class.split(".")).with_suffix(".java")
    if not source.is_file() or not (project / "pom.xml").is_file() or not (project / test_rel).is_file():
        parser.error("project must contain pom.xml, benchmark/Subject.java, and the selected test class")
    if analyzer.target_method_source(source.read_text(encoding="utf-8"), args.signature) is None:
        parser.error("signature does not identify a method in Subject.java")
    if output == project or project in output.parents or output in project.parents:
        parser.error("output must be outside the input project and cannot contain it")
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be empty; choose a new --out-dir")
    output.mkdir(parents=True, exist_ok=True)
    workdir = output / "workdir"
    shutil.copytree(project, workdir, ignore=shutil.ignore_patterns("target", ".git", ".DS_Store"))
    row = {
        "run_id": "external-evaluation", "task_id": "external", "language": "java",
        "model_id": "external", "prompt_template": "external",
        "task": {"signature": args.signature}, "test_class": args.test_class,
        "test_file": str(test_rel), "workdir": str(workdir),
        "extracted_test_code": (workdir / test_rel).read_text(encoding="utf-8"),
        "compile_passed": False, "execution_passed": False,
    }
    for stage, goals in [("compile", ["test-compile"]), ("execute", [f"-Dtest={args.test_class}", "-DfailIfNoTests=true", "test"])]:
        try:
            result = subprocess.run([args.mvn, "-q", *goals], cwd=workdir, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=args.timeout)
            log = result.stdout
            passed = result.returncode == 0
            if stage == "execute" and passed:
                reports = list((workdir / "target/surefire-reports").glob("TEST-*.xml"))
                executed = 0
                for report in reports:
                    suite = ET.parse(report).getroot()
                    executed += int(suite.get("tests", "0")) - int(suite.get("skipped", "0"))
                if executed <= 0:
                    passed = False
                    log += "\nNo executed tests were found in Surefire XML reports.\n"
        except (OSError, subprocess.TimeoutExpired) as exc:
            log = str(exc)
            passed = False
        log_path = output / f"{stage}.txt"
        log_path.write_text(log, encoding="utf-8")
        row[f"{stage}_log"] = str(log_path)
        row["compile_passed" if stage == "compile" else "execution_passed"] = passed
        if not passed:
            row["error_stage"] = stage
            break
    if args.mutation and row["execution_passed"]:
        from collect_java_extreme_condition_mutation import collect_row as collect_control
        from collect_java_assertion_mutation import collect_row as collect_assertion
        row["extreme_condition_mutation"] = collect_control(row, args.timeout, args.mvn)
        row["assertion_mutation"] = collect_assertion(row, args.timeout, args.mvn)
    (output / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    profile = analyzer.analyze_row(row)
    # Avoid publishing the legacy static oracle proxy as mutation-based Oracle Strength.
    if not args.mutation or not row["execution_passed"]:
        for field in ("assertion_strength_score", "branch_condition_strength_score", "side_effect_strength_score", "if_else_if_condition_avg_item_score"):
            profile[field] = None
    (output / "profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    analyzer.write_summary([profile], output / "summary.md")
    metrics = {}
    for name, score, evidence, applicable in [
        ("boundary_value", profile.get("boundary_strength_score"), None, len(profile.get("boundary_applicable_items") or [])),
        ("control_flow", (row.get("extreme_condition_mutation") or {}).get("score"), "extreme_condition_mutation", (row.get("extreme_condition_mutation") or {}).get("total_items")),
        ("oracle_strength", profile.get("assertion_strength_score"), "assertion_mutation", profile.get("assertion_strength_total_items")),
    ]:
        if not row["execution_passed"]:
            status, score = "test_failed", None
        elif evidence and not args.mutation:
            status, score = "not_collected", None
        elif evidence and (row.get(evidence) or {}).get("status") != "passed":
            status, score = "evidence_unavailable", None
        elif score is None:
            status = "not_applicable" if applicable == 0 else "not_scorable"
        else:
            status = "scored"
        metrics[name] = {"status": status, "score": score}
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "schema_version": 1, "signature": args.signature, "test_class": args.test_class,
        "mutation_requested": args.mutation,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "test_sha256": hashlib.sha256((project / test_rel).read_bytes()).hexdigest(),
        "python_version": sys.version.split()[0],
        "code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in
                        (Path(__file__), Path(analyzer.__file__))},
        "parser_package_installed": analyzer.get_parser is not None,
        "packages": {name: importlib.metadata.version(name) for name in ("tree-sitter-language-pack",) if analyzer.get_parser is not None},
    }
    (output / "evaluation.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Results: {output / 'summary.md'}")
    if not row["execution_passed"]:
        return 1
    if args.mutation and any((row.get(key) or {}).get("status") != "passed" for key in ("extreme_condition_mutation", "assertion_mutation")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
