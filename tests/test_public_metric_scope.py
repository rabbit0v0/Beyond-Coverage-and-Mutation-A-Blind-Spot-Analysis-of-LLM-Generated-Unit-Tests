import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_java_blindspots as analyze
import collect_java_assertion_mutation as collector


def mixed_evidence():
    return collector.aggregate_scored_items([
        {"construct": "return_behavior", "status": "executed", "outcome": "killed_by_oracle", "represented": True, "score": 1.0},
        {"construct": "exception_behavior", "status": "executed", "outcome": "survived", "represented": True, "score": 0.0},
        {"construct": "side_effect_or_dependency", "status": "executed", "outcome": "survived", "represented": True, "score": 0.0},
    ])


def test_report_view_preserves_primary_score_without_changing_raw_evidence():
    evidence = mixed_evidence()
    public = collector.paper_mutation_result(evidence)
    assert public["score"] == evidence["score"] == 0.5
    assert public["total_items"] == public["scorable_items"] == 2
    assert set(public["by_construct"]) == {"return_behavior", "exception_behavior"}
    assert "side_effect" not in json.dumps(public)
    assert "interaction_dependency" not in json.dumps(public)
    assert len(evidence["items"]) == 3


def test_public_profile_preserves_numeric_parameter_keys():
    assert analyze.paper_profile({"parameters": {0: {"boundary": "zero"}}}) == {
        "parameters": {0: {"boundary": "zero"}}
    }


def test_raw_profiles_retain_future_work_but_summaries_hide_it(tmp_path):
    project = tmp_path / "project"
    source = project / "src/main/java/benchmark/Subject.java"
    source.parent.mkdir(parents=True)
    source.write_text("package benchmark; public class Subject { public static int value(int x) { return x; } }")
    row = analyze.analyze_row({
        "task_id": "scope-test", "model_id": "scope-model", "prompt_template": "external",
        "task": {"signature": "public static int value(int x)"}, "workdir": str(project),
        "execution_passed": True, "compile_passed": True,
        "extracted_test_code": "class T { void test() { assertEquals(0, Subject.value(0)); } }",
        "assertion_mutation": mixed_evidence(),
    })
    assert row["assertion_strength_score"] == 0.5
    assert "side_effect_research_scope" in row
    assert "interaction_dependency" in json.dumps(analyze.flatten(row))
    historical = {**row, "side_effect_research_by_construct": {"side_effect_or_dependency": {"total": 5, "score_sum": 4}}}
    summary = tmp_path / "summary.md"
    analyze.write_summary([historical], summary)
    text = summary.read_text()
    assert "Side-Effect Research Question" not in text
    assert "side_effect_or_dependency" not in text
    assert [line[3:] for line in text.splitlines() if line.startswith("## ")] == [
        "Execution Funnel",
        "Failure Classification",
        "Boundary-Value Adequacy Details",
        "Extreme Mutation Funnel",
        "Control-Flow Adequacy Details",
        "Assertion Mutation Funnel",
        "Oracle Strength Details",
    ]
    assert all(not line or line.startswith(("#", "|", "- ")) for line in text.splitlines())
    assert "uses custom" not in text
    assert "scoring denominator" not in text
    for removed in (
        "### Boundary Unresolvable Argument Rate",
        "### Oracle Strength By Construct",
        "### Object Return By Rule",
        "### Assertion Mutation Outcome Breakdown",
        "| Group | Rows With Evidence |",
    ):
        assert removed not in text
    assert "## Oracle Strength Details\n\n| Construct |" in text
    assert "| return_behavior |" in text
    assert "| exception_behavior |" in text
    assert "side_effect_research_scope" in row


def test_extreme_mutation_funnel_uses_only_eligible_passed_evidence():
    evidence = {
        "execution_passed": True, "target_is_private": False,
        "extreme_condition_mutation_status": "passed",
        "if_else_if_condition_total": 5,
        "if_else_if_condition_represented": 4,
        "if_else_if_condition_scorable": 3,
        "if_else_if_condition_checked_items": 2,
    }
    rows = [
        evidence,
        {**evidence, "execution_passed": False},
        {**evidence, "target_is_private": True},
        {**evidence, "extreme_condition_mutation_status": "failed"},
        {"execution_passed": True},
    ]
    text = "\n".join(analyze.extreme_mutation_funnel_table(rows))
    assert "| Rows in set | 5 |" in text
    assert "| Execution passed | 4 |" in text
    assert "| Executable public-target rows | 3 |" in text
    assert "| Rows with extreme mutation evidence | 2 |" in text
    assert "| Rows with passed extreme mutation evidence | 1 |" in text
    assert "| Applicable obligations | 5 |" in text
    assert "| Represented obligations | 4 |" in text
    assert "| Scorable obligations | 3 |" in text
    assert "| Checked obligations | 2 |" in text


def test_execution_funnel_counts_compilation_and_execution():
    rows = [
        {"compile_passed": False, "execution_passed": False},
        {"compile_passed": True, "execution_passed": False},
        {"compile_passed": True, "execution_passed": True},
    ]
    text = "\n".join(analyze.execution_summary_lines(rows))
    assert "| Total | 3 |" in text
    assert "| Compilation passed | 2 |" in text
    assert "| Execution passed | 1 |" in text
    assert "| Non-executable | 2 |" in text


def test_checkpoint_preserves_raw_evidence_and_hides_future_work_in_summary(tmp_path):
    evidence = mixed_evidence()
    rows = [{"assertion_mutation": evidence}]
    output = tmp_path / "results.jsonl"
    summary = tmp_path / "summary.md"
    collector.write_rows_checkpoint(rows, output, summary)
    assert json.loads(output.read_text())["assertion_mutation"] == evidence
    assert "side_effect_or_dependency" not in summary.read_text()
    assert len(evidence["items"]) == 3
