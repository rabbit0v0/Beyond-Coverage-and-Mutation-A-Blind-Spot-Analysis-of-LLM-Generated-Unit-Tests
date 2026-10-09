import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_java_blindspots as analyzer
import collect_java_assertion_mutation as assertion


def test_historical_panta_preview_is_the_analysis_input_even_with_full_file(tmp_path):
    source = "public class Subject { public static int clamp(int value) { return value; } }"
    signature = "public static int clamp(int value)"
    code = "class T { void test() {\n" + "// padding\n" * 500
    code += "Subject.clamp(0); Subject.clamp(-1); Subject.clamp(1); } }"
    path = tmp_path / "PantaGeneratedTest.java"
    path.write_text(code)
    preview = code[:4000]
    row = {"test_file": {"path": str(path), "preview": preview, "bytes": len(code)}}
    assert analyzer.generated_test_code(row) == preview
    partial = analyzer.classify_boundary_strength(preview, source, signature, analyzer.assertion_features(preview))
    complete = analyzer.classify_boundary_strength(code, source, signature, analyzer.assertion_features(code))
    assert partial["boundary_strength_score"] == 0.0
    assert complete["boundary_strength_score"] > partial["boundary_strength_score"]


def test_incidental_scoring_policy_changes_the_historical_denominator():
    mutants = [{"outcome": "killed_incidentally"}]
    default = assertion.aggregate_variant_scores(mutants, "zero")
    historical = assertion.aggregate_variant_scores(mutants, "exclude")
    assert default["score"] == 0.0
    assert default["scored_variant_count"] == 1
    assert historical["score"] is None
    assert historical["scored_variant_count"] == 0
