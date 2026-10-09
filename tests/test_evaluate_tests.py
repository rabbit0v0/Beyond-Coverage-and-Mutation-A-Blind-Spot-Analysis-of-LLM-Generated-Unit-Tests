import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_tests as evaluator
from collect_java_extreme_condition_mutation import panta_test_class


def prepare(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = project / "src/main/java/benchmark/Subject.java"
    test = project / "src/test/java/benchmark/SubjectTest.java"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("package benchmark; public class Subject { public static int clamp(int value) { return value; } }")
    test.write_text("package benchmark; public class SubjectTest {}")
    (project / "pom.xml").write_text("<project/>")
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["evaluate_tests", "--project", str(project),
                        "--signature", "public static int clamp(int value)",
                        "--test-class", "benchmark.SubjectTest", "--out-dir", str(output)])
    return project, output


def test_failed_execution_is_retained_without_adequacy_credit(tmp_path, monkeypatch):
    project, output = prepare(tmp_path, monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0 if len(calls) == 1 else 1, "test failure")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    before = (project / "src/main/java/benchmark/Subject.java").read_bytes()
    assert evaluator.main() == 1
    row = json.loads((output / "results.jsonl").read_text())
    assert row["compile_passed"] and not row["execution_passed"]
    assert all(metric["score"] is None for metric in json.loads((output / "metrics.json").read_text()).values())
    assert (project / "src/main/java/benchmark/Subject.java").read_bytes() == before
    assert len(calls) == 2


def test_success_without_executed_tests_is_rejected(tmp_path, monkeypatch):
    _, output = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(evaluator.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, ""))
    assert evaluator.main() == 1
    assert "No executed tests" in (output / "execute.txt").read_text()


def test_verified_tests_distinguish_uncollected_mutation(tmp_path, monkeypatch):
    _, output = prepare(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "test" in command:
            reports = Path(kwargs["cwd"]) / "target/surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-benchmark.SubjectTest.xml").write_text('<testsuite tests="1" skipped="0"/>')
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(evaluator.subprocess, "run", run)
    assert evaluator.main() == 0
    metrics = json.loads((output / "metrics.json").read_text())
    assert set(metrics) == {"boundary_value", "control_flow", "oracle_strength"}
    assert metrics["oracle_strength"] == {"status": "not_collected", "score": None}
    assert metrics["control_flow"] == {"status": "not_collected", "score": None}


def test_explicit_test_class_is_used_for_external_mutation():
    assert panta_test_class({"prompt_template": "external", "test_class": "benchmark.SubjectTest"}) == "benchmark.SubjectTest"
