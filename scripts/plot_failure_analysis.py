#!/usr/bin/env python3
"""Plot Java generated-test run outcomes by model and pipeline."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OUTCOME_ORDER = [
    "passed",
    "model_generation_failure",
    "compile_failure",
    "execution_failure",
    "timeout_or_hang",
    "panta_generation_failure",
    "unknown_failure",
]

OUTCOME_LABELS = {
    "passed": "passed",
    "model_generation_failure": "model/API\ngeneration",
    "compile_failure": "compile\nfailure",
    "execution_failure": "execution\nfailure",
    "timeout_or_hang": "timeout/\nhang",
    "panta_generation_failure": "Panta\ngeneration",
    "unknown_failure": "unknown\nfailure",
}

OUTCOME_COLORS = {
    "passed": "#009E73",
    "model_generation_failure": "#56B4E9",
    "compile_failure": "#D55E00",
    "execution_failure": "#CC79A7",
    "timeout_or_hang": "#E69F00",
    "panta_generation_failure": "#8A5CC2",
    "unknown_failure": "#333333",
}

DETAIL_ORDER = [
    "syntax_or_other_compile_error",
    "unresolved_symbol_or_api_misuse",
    "private_or_reflection_access",
    "checked_exception_not_handled",
    "type_mismatch",
    "wrong_oracle",
    "uncaught_runtime_exception",
    "other_execution_failure",
    "compile_passed is false",
    "unknown",
]

DETAIL_LABELS = {
    "syntax_or_other_compile_error": "syntax/other\ncompile",
    "unresolved_symbol_or_api_misuse": "unresolved\nsymbol/API",
    "private_or_reflection_access": "private/access\nreflection",
    "checked_exception_not_handled": "checked exception\nnot handled",
    "type_mismatch": "type\nmismatch",
    "wrong_oracle": "wrong\noracle",
    "uncaught_runtime_exception": "uncaught runtime\nexception",
    "other_execution_failure": "other execution\nfailure",
    "compile_passed is false": "compile_passed\nfalse",
    "unknown": "unknown",
}

DETAIL_COLORS = {
    "syntax_or_other_compile_error": "#D55E00",
    "unresolved_symbol_or_api_misuse": "#E69F00",
    "private_or_reflection_access": "#CC79A7",
    "checked_exception_not_handled": "#0072B2",
    "type_mismatch": "#56B4E9",
    "wrong_oracle": "#009E73",
    "uncaught_runtime_exception": "#8A5CC2",
    "other_execution_failure": "#7A7A7A",
    "compile_passed is false": "#333333",
    "unknown": "#999999",
}

PROMPT_ORDER = [
    "zero-shot",
    "p3-intention-planning",
    "p5-mutation-feedback",
    "panta",
]


@dataclass(frozen=True)
class GroupKey:
    model: str
    pipeline: str

    @property
    def label(self) -> str:
        return f"{short_model_name(self.model)} / {self.pipeline}"


def read_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        rows: list[dict[str, Any]] = []
        for child in sorted([*path.glob("*.jsonl"), *path.glob("*.jsonl.gz")]):
            rows.extend(read_rows(child))
        if not rows:
            raise SystemExit(f"no JSONL files found in {path}")
        return rows
    rows = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def short_model_name(model: str) -> str:
    if model == "RedHatAI/gemma-4-31B-it-FP8-Dynamic":
        return "Gemma-4-31B"
    if model == "gpt-5.4":
        return "GPT-5.4"
    return model.split("/")[-1]


def prompt_rank(prompt: str) -> tuple[int, str]:
    if prompt in PROMPT_ORDER:
        return (PROMPT_ORDER.index(prompt), prompt)
    return (len(PROMPT_ORDER), prompt)


def group_key(row: dict[str, Any]) -> GroupKey:
    return GroupKey(
        model=str(row.get("model_id") or "unknown-model"),
        pipeline=str(row.get("prompt_template") or row.get("tool") or "unknown-pipeline"),
    )


def failure_log_path(row: dict[str, Any]) -> Path | None:
    workdir = row.get("workdir")
    prompt = row.get("prompt_template")
    if not workdir or not prompt:
        return None
    stage = row.get("error_stage")
    path = Path(workdir)
    if stage in {"compile", "compile_timeout"}:
        return path / f"mvn-test-{prompt}.log"
    if stage in {"execute", "execute_timeout"}:
        return path / f"execute-{prompt}.log"
    return path / f"mvn-test-{prompt}.log"


def read_log(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def normal_pipeline_outcome(row: dict[str, Any]) -> tuple[str, str]:
    stage = str(row.get("error_stage") or "")
    if stage == "model_error" or stage.startswith("model_error_") or row.get("model_error_type") or row.get("model_error_status_code"):
        status = row.get("model_error_status_code") or stage.replace("model_error_", "")
        error_type = row.get("model_error_type") or "model_error"
        return "model_generation_failure", f"{error_type} {status}".strip()
    if row.get("execution_passed"):
        return "passed", "generated test compiled and executed"

    if stage in {"compile_timeout", "execute_timeout"}:
        return "timeout_or_hang", str(stage)
    if stage == "compile":
        return "compile_failure", classify_log_detail(row, stage)
    if stage == "execute":
        return "execution_failure", classify_log_detail(row, stage)
    if row.get("compile_passed") is False:
        return "compile_failure", "compile_passed is false"
    return "unknown_failure", f"error_stage={stage}"


def panta_outcome(row: dict[str, Any]) -> tuple[str, str]:
    mutation_status = str(row.get("mutation_status") or "")
    mutation_stage = str(row.get("mutation_error_stage") or "")
    skip_reason = str(row.get("panta_skip_reason") or "")
    extreme_status = str((row.get("extreme_condition_mutation") or {}).get("status") or "")
    if mutation_status in {"passed", "no_mutants"} and extreme_status in {"", "passed"}:
        return "passed", "Panta generation and mutation evidence available"
    if mutation_stage == "panta_generation" or skip_reason.startswith("panta_"):
        return "panta_generation_failure", skip_reason or mutation_stage
    if mutation_status == "failed" or (extreme_status and extreme_status != "passed"):
        return "passed", "Panta generated a test; later mutation evidence failed or was skipped"
    return "unknown_failure", f"mutation_status={mutation_status}, panta_skip_reason={skip_reason}"


def classify_log_detail(row: dict[str, Any], stage: str | None) -> str:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import analyze_java_blindspots as ajb
    except Exception:
        return str(stage or "unknown")
    log = read_log(failure_log_path(row))
    _, category, matches = ajb.classify_failure(log, stage)
    if matches:
        return category
    return category or str(stage or "unknown")


def classify_outcome(row: dict[str, Any]) -> tuple[str, str]:
    if "failure_family" in row and "failure_category" in row:
        if row.get("execution_passed"):
            return "passed", "passed"
        family = str(row.get("failure_family") or "unknown")
        category = str(row.get("failure_category") or "unknown")
        if category == "timeout_or_hang" or row.get("error_stage") in {"compile_timeout", "execute_timeout"}:
            return "timeout_or_hang", category
        if family == "generation_failure":
            return ("panta_generation_failure" if category.startswith("panta_") else "model_generation_failure"), category
        if family in {"compile_failure", "execution_failure"}:
            return family, category
        return "unknown_failure", category
    if row.get("prompt_template") == "panta" or row.get("tool") == "panta":
        return panta_outcome(row)
    return normal_pipeline_outcome(row)


def aggregate(rows: list[dict[str, Any]]) -> tuple[dict[GroupKey, Counter[str]], list[dict[str, Any]]]:
    counts: dict[GroupKey, Counter[str]] = defaultdict(Counter)
    details: list[dict[str, Any]] = []
    for row in rows:
        key = group_key(row)
        outcome, detail = classify_outcome(row)
        counts[key][outcome] += 1
        counts[key]["total"] += 1
        details.append(
            {
                "task_id": row.get("task_id"),
                "model": key.model,
                "model_short": short_model_name(key.model),
                "pipeline": key.pipeline,
                "outcome": outcome,
                "detail": detail,
                "error_stage": row.get("error_stage"),
                "compile_passed": row.get("compile_passed"),
                "execution_passed": row.get("execution_passed"),
                "mutation_status": row.get("mutation_status"),
                "mutation_error_stage": row.get("mutation_error_stage"),
                "panta_skip_reason": row.get("panta_skip_reason"),
                "extreme_condition_mutation_status": (row.get("extreme_condition_mutation") or {}).get("status"),
            }
        )
    return counts, details


def ordered_groups(counts: dict[GroupKey, Counter[str]]) -> list[GroupKey]:
    return sorted(counts, key=lambda key: (short_model_name(key.model), prompt_rank(key.pipeline)))


def present_outcomes(counts: dict[GroupKey, Counter[str]]) -> list[str]:
    labels = {outcome for counter in counts.values() for outcome, count in counter.items() if outcome != "total" and count}
    return [outcome for outcome in OUTCOME_ORDER if outcome in labels] + sorted(labels - set(OUTCOME_ORDER))


def display_label(label: str) -> str:
    return OUTCOME_LABELS.get(label, label.replace("_", "\n"))


def display_detail_label(label: str) -> str:
    return DETAIL_LABELS.get(label, label.replace("_", "\n"))


def present_details(details: list[dict[str, Any]], outcome: str) -> list[str]:
    labels = {str(row["detail"]) for row in details if row["outcome"] == outcome}
    return [label for label in DETAIL_ORDER if label in labels] + sorted(labels - set(DETAIL_ORDER))


def write_summary(counts: dict[GroupKey, Counter[str]], details: list[dict[str, Any]], out_path: Path) -> None:
    outcomes = present_outcomes(counts)
    lines = [
        "# Generated Test Run Outcome Summary",
        "",
        f"- rows: {len(details)}",
        f"- groups: {len(counts)}",
        "",
        "| Model | Pipeline | Total | " + " | ".join(outcomes) + " |",
        "| --- | --- | ---: | " + " | ".join("---:" for _ in outcomes) + " |",
    ]
    for key in ordered_groups(counts):
        counter = counts[key]
        cells = [str(counter.get(outcome, 0)) for outcome in outcomes]
        lines.append(f"| {short_model_name(key.model)} | {key.pipeline} | {counter['total']} | {' | '.join(cells)} |")
    lines.extend(["", "## Failure Details", ""])
    for key in ordered_groups(counts):
        rows = [row for row in details if row["model"] == key.model and row["pipeline"] == key.pipeline and row["outcome"] != "passed"]
        if not rows:
            continue
        detail_counts = Counter((row["outcome"], row["detail"]) for row in rows)
        lines.extend([f"### {short_model_name(key.model)} / {key.pipeline}", "", "| Outcome | Detail | Count |", "| --- | --- | ---: |"])
        for (outcome, detail), count in sorted(detail_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {outcome} | {str(detail).replace('|', '/')} | {count} |")
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_details_jsonl(details: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in details), encoding="utf-8")


def write_svg(counts: dict[GroupKey, Counter[str]], out_path: Path) -> None:
    groups = ordered_groups(counts)
    outcomes = present_outcomes(counts)
    if not groups or not outcomes:
        raise SystemExit("no failure data to plot")
    left = 82
    right = 36
    top = 58
    bottom = 188
    plot_height = 350
    group_width = 112
    plot_width = group_width * len(groups)
    legend_cols = min(3, len(outcomes))
    legend_width = legend_cols * 245
    width = max(left + right + plot_width, left + right + legend_width)
    height = top + plot_height + bottom
    x0 = left
    y0 = top + plot_height
    bar_width = 48

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<title>Generated Test Run Outcome by Model and Pipeline</title>",
        "<desc>Each bar is one model and pipeline. Segments show whether generated tests reached compile and execution successfully, excluding PIT/custom mutation-stage failures.</desc>",
        "<style>",
        "text{font-family:Arial,'Helvetica Neue',sans-serif;fill:#202124}",
        ".axis{stroke:#4b5563;stroke-width:1}",
        ".grid{stroke:#d9d9d9;stroke-width:1}",
        ".muted{fill:#5f6368;font-size:11px}",
        ".label{font-size:11px;fill:#303134}",
        ".title{font-size:18px;font-weight:600}",
        ".legend{font-size:12px;fill:#303134}",
        ".bar{stroke:#ffffff;stroke-width:0.7}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="title" x="{width / 2:.1f}" y="28" text-anchor="middle">Generated Test Run Outcome by Model and Pipeline</text>',
    ]
    for tick in [0, 25, 50, 75, 100]:
        y = y0 - (tick / 100) * plot_height
        elements.append(f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x0 + plot_width}" y2="{y:.1f}"/>')
        elements.append(f'<text class="muted" x="{x0 - 10}" y="{y + 4:.1f}" text-anchor="end">{tick}%</text>')
    elements.append(f'<line class="axis" x1="{x0}" y1="{y0}" x2="{x0 + plot_width}" y2="{y0}"/>')
    elements.append(f'<line class="axis" x1="{x0}" y1="{top}" x2="{x0}" y2="{y0}"/>')
    elements.append('<text class="muted" transform="translate(18 233) rotate(-90)" text-anchor="middle">Rows by outcome (%)</text>')

    for index, key in enumerate(groups):
        counter = counts[key]
        total = float(counter["total"])
        x = x0 + index * group_width + (group_width - bar_width) / 2
        current_y = y0
        for outcome in outcomes:
            count = float(counter.get(outcome) or 0)
            if not count:
                continue
            pct = count / total * 100.0
            h = pct / 100.0 * plot_height
            current_y -= h
            color = OUTCOME_COLORS.get(outcome, "#777777")
            elements.append(
                f'<rect class="bar" x="{x:.1f}" y="{current_y:.1f}" width="{bar_width:.1f}" height="{h:.1f}" fill="{color}">'
                f'<title>{html.escape(key.label)}; {html.escape(outcome)}: {int(count)}/{int(total)} ({pct:.1f}%)</title></rect>'
            )
        center_x = x0 + index * group_width + group_width / 2
        elements.append(f'<text class="label" x="{center_x:.1f}" y="{y0 + 20}" text-anchor="middle">')
        label_lines = [short_model_name(key.model), key.pipeline]
        for line_index, line in enumerate(label_lines):
            dy = 0 if line_index == 0 else 13
            elements.append(f'<tspan x="{center_x:.1f}" dy="{dy}">{html.escape(line)}</tspan>')
        elements.append("</text>")

    legend_y = height - 74
    for index, outcome in enumerate(outcomes):
        x = left + (index % 3) * 245
        y = legend_y + (index // 3) * 22
        elements.append(f'<rect x="{x}" y="{y - 10}" width="13" height="13" fill="{OUTCOME_COLORS.get(outcome, "#777777")}"/>')
        elements.append(f'<text class="legend" x="{x + 20}" y="{y + 1}">{html.escape(display_label(outcome).replace(chr(10), " "))}</text>')

    elements.append("</svg>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def detail_counts_by_group(details: list[dict[str, Any]], outcome: str) -> dict[GroupKey, Counter[str]]:
    counts: dict[GroupKey, Counter[str]] = defaultdict(Counter)
    for row in details:
        if row["outcome"] != outcome:
            continue
        key = GroupKey(str(row["model"]), str(row["pipeline"]))
        counts[key][str(row["detail"])] += 1
        counts[key]["total"] += 1
    return counts


def nice_count_ticks(max_value: int) -> list[int]:
    if max_value <= 0:
        return [0]
    raw_step = max(1, -(-max_value // 4))
    magnitude = 10 ** (len(str(raw_step)) - 1)
    step = ((raw_step + magnitude - 1) // magnitude) * magnitude
    top = ((max_value + step - 1) // step) * step
    return list(range(0, top + step, step))


def write_detail_svg(
    counts: dict[GroupKey, Counter[str]],
    details: list[str],
    title: str,
    ylabel: str,
    out_path: Path,
) -> None:
    groups = ordered_groups(counts)
    if not groups or not details:
        print(f"skipping {out_path}: no subtype data")
        return
    max_total = max(int(counts[key]["total"]) for key in groups)
    ticks = nice_count_ticks(max_total)
    top_tick = max(ticks) if ticks else max_total
    left = 82
    right = 36
    top = 58
    bottom = 196
    plot_height = 350
    group_width = 112
    plot_width = group_width * len(groups)
    legend_cols = min(3, len(details))
    legend_width = legend_cols * 245
    width = max(left + right + plot_width, left + right + legend_width)
    height = top + plot_height + bottom
    x0 = left
    y0 = top + plot_height
    bar_width = 48

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f"<title>{html.escape(title)}</title>",
        f"<desc>{html.escape(title)}. Each stacked bar shows absolute generated-test failure counts by subtype for one model and pipeline.</desc>",
        "<style>",
        "text{font-family:Arial,'Helvetica Neue',sans-serif;fill:#202124}",
        ".axis{stroke:#4b5563;stroke-width:1}",
        ".grid{stroke:#d9d9d9;stroke-width:1}",
        ".muted{fill:#5f6368;font-size:11px}",
        ".label{font-size:11px;fill:#303134}",
        ".title{font-size:18px;font-weight:600}",
        ".legend{font-size:12px;fill:#303134}",
        ".bar{stroke:#ffffff;stroke-width:0.7}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="title" x="{width / 2:.1f}" y="28" text-anchor="middle">{html.escape(title)}</text>',
    ]
    for tick in ticks:
        y = y0 - (tick / top_tick) * plot_height if top_tick else y0
        elements.append(f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x0 + plot_width}" y2="{y:.1f}"/>')
        elements.append(f'<text class="muted" x="{x0 - 10}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>')
    elements.append(f'<line class="axis" x1="{x0}" y1="{y0}" x2="{x0 + plot_width}" y2="{y0}"/>')
    elements.append(f'<line class="axis" x1="{x0}" y1="{top}" x2="{x0}" y2="{y0}"/>')
    elements.append(
        f'<text class="muted" transform="translate(18 {top + plot_height / 2:.1f}) rotate(-90)" '
        f'text-anchor="middle">{html.escape(ylabel)}</text>'
    )

    for index, key in enumerate(groups):
        counter = counts[key]
        x = x0 + index * group_width + (group_width - bar_width) / 2
        current_y = y0
        for detail in details:
            count = int(counter.get(detail) or 0)
            if not count:
                continue
            h = (count / top_tick) * plot_height if top_tick else 0
            current_y -= h
            color = DETAIL_COLORS.get(detail, "#777777")
            elements.append(
                f'<rect class="bar" x="{x:.1f}" y="{current_y:.1f}" width="{bar_width:.1f}" height="{h:.1f}" fill="{color}">'
                f'<title>{html.escape(key.label)}; {html.escape(detail)}: {count}</title></rect>'
            )
        center_x = x0 + index * group_width + group_width / 2
        elements.append(f'<text class="label" x="{center_x:.1f}" y="{y0 + 20}" text-anchor="middle">')
        for line_index, line in enumerate([short_model_name(key.model), key.pipeline]):
            dy = 0 if line_index == 0 else 13
            elements.append(f'<tspan x="{center_x:.1f}" dy="{dy}">{html.escape(line)}</tspan>')
        elements.append("</text>")

    legend_y = height - 82
    for index, detail in enumerate(details):
        x = left + (index % 3) * 245
        y = legend_y + (index // 3) * 22
        label = display_detail_label(detail).replace("\n", " ")
        elements.append(f'<rect x="{x}" y="{y - 10}" width="13" height="13" fill="{DETAIL_COLORS.get(detail, "#777777")}"/>')
        elements.append(f'<text class="legend" x="{x + 20}" y="{y + 1}">{html.escape(label)}</text>')

    elements.append("</svg>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(elements) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path, nargs="+", help="Result JSONL files or directories containing JSONL files.")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/figures/final-mixed-runs/custom-control-flow/treatment"))
    parser.add_argument("--prefix", default="final-treatment")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for path in args.inputs:
        rows.extend(read_rows(path))
    counts, details = aggregate(rows)
    write_details_jsonl(details, args.out_dir / f"{args.prefix}-failure-analysis.jsonl")
    write_summary(counts, details, args.out_dir / f"{args.prefix}-failure-analysis-summary.md")
    write_svg(counts, args.out_dir / f"{args.prefix}-failure-analysis.svg")
    for outcome, title in [
        ("compile_failure", "Compile Failure Types by Model and Pipeline"),
        ("execution_failure", "Execution Failure Types by Model and Pipeline"),
    ]:
        subtype_counts = detail_counts_by_group(details, outcome)
        write_detail_svg(
            subtype_counts,
            present_details(details, outcome),
            title,
            "Failed generated tests",
            args.out_dir / f"{args.prefix}-{outcome.replace('_', '-')}-types.svg",
        )
    print(f"wrote {args.out_dir / f'{args.prefix}-failure-analysis.jsonl'}")
    print(f"wrote {args.out_dir / f'{args.prefix}-failure-analysis-summary.md'}")
    print(f"wrote {args.out_dir / f'{args.prefix}-failure-analysis.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
