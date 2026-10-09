import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_complexity import collect


def test_comparison_uses_scorable_denominator_and_partial_credit(tmp_path):
    base = {
        "execution_passed": True, "model_id": "model", "prompt_template": "zero-shot",
        "boundary_applicable_items": [{"category": "zero"}, {"category": "negative"}],
        "boundary_matched_items": [{"category": "zero"}],
        "extreme_condition_mutation_status": "passed",
        "if_else_if_condition_by_construct": {"ternary": {"total": 5, "scorable": 2, "score_sum": 1.5}},
    }
    path = tmp_path / "profiles.jsonl"
    rows = [base, {**base, "execution_passed": False},
            {**base, "run_id": "java-v2-mixed-gemma-panta-control-full"}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    totals = collect(path)
    assert totals[("model", "zero-shot", "CFA", "ternary")] == [1.5, 2]
    assert totals[("model", "zero-shot", "BVA", "zero")] == [1.0, 1]
    assert totals[("model", "zero-shot", "BVA", "negative")] == [0.0, 1]
