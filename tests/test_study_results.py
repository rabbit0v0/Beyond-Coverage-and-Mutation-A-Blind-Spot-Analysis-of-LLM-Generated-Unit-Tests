import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_java_blindspots as analyzer
import compare_complexity
import package_study_results as package
import plot_failure_analysis as failures


def test_compact_profiles_preserve_scores_and_comparison_counts(tmp_path):
    row = {
        "task_id": "task", "model_id": "gpt-5.4", "prompt_template": "zero-shot",
        "execution_passed": True, "compile_passed": True,
        "boundary_strength_score": 0.5, "boundary_applicable_count": 2, "boundary_covered_count": 1,
        "boundary_applicable_items": [{"category": "zero", "snippet": "private source"}, {"category": "negative"}],
        "boundary_matched_items": [{"category": "zero", "snippet": "test code"}],
        "extreme_condition_mutation_status": "passed", "if_else_if_condition_avg_item_score": 0.5,
        "if_else_if_condition_by_construct": {"if": {"scorable": 2, "score_sum": 1.0}},
        "assertion_strength_score": 0.75, "assertion_mutation_status": "passed",
        "assertion_strength_by_construct": {"return_behavior": {"total": 1}, "side_effect_or_dependency": {"total": 1}},
        "side_effect_research_scope": {"score": 0.2}, "workdir": "/private/workspace",
    }
    compact = package.compact_profile(row)
    for field in ("boundary_strength_score", "if_else_if_condition_avg_item_score", "assertion_strength_score"):
        assert compact[field] == row[field]
    assert "workdir" not in compact and "side_effect" not in json.dumps(compact)
    original = tmp_path / "original.jsonl"
    original.write_text(json.dumps(row) + "\n")
    compressed = tmp_path / "blindspot_profiles.jsonl.gz"
    with gzip.open(compressed, "wt") as handle:
        handle.write(json.dumps(compact) + "\n")
    assert compare_complexity.collect(original) == compare_complexity.collect(compressed)
    assert analyzer.read_jsonl(compressed) == [compact]
    report = tmp_path / "summary.md"
    analyzer.write_summary([compact], report)
    assert "## Oracle Strength Details" in report.read_text()


def test_packaged_release_has_exact_configurations_and_no_duplicates():
    root = Path(__file__).resolve().parents[1] / "analysis/final-mixed-runs/release"
    inventory = json.loads((root / "inventory.json").read_text())
    for split, models, tasks in (("treatment", package.TREATMENT_MODELS, 895), ("control", package.CONTROL_MODELS, 700)):
        rows = analyzer.read_jsonl(root / split / "blindspot_profiles.jsonl.gz")
        keys = {(row["model_id"], row["prompt_template"], row["task_id"]) for row in rows}
        assert len(rows) == len(keys) == len(models) * len(package.STRATEGIES) * tasks
        assert {row["model_id"] for row in rows} == models
        assert {row["prompt_template"] for row in rows} == package.STRATEGIES
        assert all(row["run_id"] not in package.EXCLUDED_RUNS for row in rows)
        path = root / split / "blindspot_profiles.jsonl.gz"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == inventory["groups"][split]["profile_sha256"]


def test_release_provenance_matches_v11_scoring_and_public_profile_contract():
    root = Path(__file__).resolve().parents[1] / "analysis/final-mixed-runs/release"
    inventory = json.loads((root / "inventory.json").read_text())
    assert inventory["schema_version"] == 2
    collection = inventory["os_collection"]
    assert collection["collector_version"] == "custom_assertion_mutation_v11"
    assert collection["settings"]["incidental_score"] == "zero"
    assert inventory["preserved_non_os_evidence"]["fields_unchanged"] is True
    for split in ("treatment", "control"):
        group = inventory["groups"][split]
        assert group["source"] == f"mutation-evidence-{split}-v11-zero.zip::profiles/blindspot_profiles.jsonl.gz"
        assert group["source_sha256"] == group["profile_sha256"]
        with gzip.open(root / split / "blindspot_profiles.jsonl.gz", "rt") as handle:
            for line in handle:
                row = json.loads(line)
                assert not ({"workdir", "assertion_strength_items", "extracted_test_code", "side_effect_research_scope"} & row.keys())
                assert set(row["assertion_strength_by_construct"]) <= {"return_behavior", "exception_behavior"}
                assert row.get("assertion_strength_score") is None or 0 <= row["assertion_strength_score"] <= 1


def test_saved_failure_classifications_do_not_require_logs():
    row = {"failure_family": "compile_failure", "failure_category": "type_mismatch", "execution_passed": False}
    assert failures.classify_outcome(row) == ("compile_failure", "type_mismatch")
    row.update(failure_family="generation_failure", failure_category="panta_placeholder_stub", prompt_template="panta")
    assert failures.classify_outcome(row) == ("panta_generation_failure", "panta_placeholder_stub")
