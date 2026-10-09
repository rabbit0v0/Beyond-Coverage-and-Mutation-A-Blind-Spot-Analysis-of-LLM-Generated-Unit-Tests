#!/usr/bin/env python3
"""Split Java mixed-v2 run artifacts into treatment and control subsets.

Treatment rows are the non-easy Java v2 tasks. Control rows are the easy tasks.
The original mixed files are left untouched; split artifacts are written under
``<original-parent>/<run-id>/``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path("benchmarks/java-complexity-v2/manifests/java_v2_mixed.jsonl")
DEFAULT_RESULTS_DIR = Path("pilot-results")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_manifest_easy_labels(path: Path) -> dict[str, bool]:
    labels: dict[str, bool] = {}
    if not path.exists():
        return labels
    for row in read_jsonl(path):
        task_id = row.get("task_id")
        easy = ((row.get("v2_category_labels") or {}).get("easy"))
        if task_id and easy is not None:
            labels[str(task_id)] = bool(easy)
    return labels


def row_is_easy(row: dict[str, Any], manifest_easy: dict[str, bool]) -> bool | None:
    task = row.get("task") or {}
    labels = task.get("v2_category_labels") or row.get("v2_category_labels") or {}
    if "easy" in labels:
        return bool(labels["easy"])
    task_id = row.get("task_id") or task.get("task_id")
    if task_id in manifest_easy:
        return manifest_easy[str(task_id)]
    split = task.get("v2_primary_split") or row.get("v2_primary_split")
    if split == "easy":
        return True
    memberships = task.get("v2_split_memberships") or row.get("v2_split_memberships") or []
    if "easy" in memberships:
        return True
    return None


def suffix_path(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}-{suffix}{path.suffix}")


def prompt_suffix(prompt_template: str) -> str:
    return prompt_template.replace("_", "-").replace(" ", "-")


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def avg(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def coverage_value(row: dict[str, Any], key: str) -> float | int | None:
    coverage = row.get("coverage") or {}
    return coverage.get(key)


def mutation_denominator(row: dict[str, Any]) -> int:
    return (
        int(row.get("mutants_killed") or 0)
        + int(row.get("mutants_survived") or 0)
        + int(row.get("mutants_no_coverage") or 0)
        + int(row.get("mutants_timed_out") or 0)
    )


def mutation_numerator(row: dict[str, Any]) -> int:
    return int(row.get("mutants_killed") or 0) + int(row.get("mutants_timed_out") or 0)


def passed_mutation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("mutation_status") == "passed" and row.get("mutation_score") is not None
    ]


def weighted_mutation(rows: list[dict[str, Any]]) -> float | None:
    denominator = sum(mutation_denominator(row) for row in rows)
    if not denominator:
        return None
    return sum(mutation_numerator(row) for row in rows) / denominator


def group_value(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        value = (row.get("task") or {}).get(key)
    return str(value)


def write_mutation_summary(path: Path, rows: list[dict[str, Any]], split_name: str) -> None:
    successful = passed_mutation_rows(rows)
    lines = [
        "# Java PIT Mutation Summary",
        "",
        f"- split: {split_name}",
        f"- rows: {len(rows)}",
        f"- PIT passed with score: {len(successful)}",
        f"- skipped: {sum(1 for row in rows if row.get('mutation_status') == 'skipped')}",
        f"- PIT failed: {sum(1 for row in rows if row.get('mutation_status') == 'failed')}",
    ]
    if successful:
        lines.extend(
            [
                f"- average mutation score, per row: {fmt(avg([row.get('mutation_score') for row in successful]))}",
                f"- weighted mutation score, by mutant count: {fmt(weighted_mutation(successful))}",
            ]
        )

    def add_group(title: str, key: str) -> None:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in successful:
            groups[group_value(row, key)].append(row)
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
            lines.append(
                f"| {group} | {len(group_rows)} | "
                f"{fmt(avg([row.get('mutation_score') for row in group_rows]))} | "
                f"{fmt(weighted_mutation(group_rows))} | "
                f"{fmt(avg([coverage_value(row, 'line_rate') for row in group_rows]))} | "
                f"{fmt(avg([coverage_value(row, 'branch_rate') for row in group_rows]))} |"
            )

    add_group("By Prompt", "prompt_template")
    add_group("By Complexity Bucket", "complexity_bucket")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def split_rows(
    rows: list[dict[str, Any]], manifest_easy: dict[str, bool], source: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    treatment: list[dict[str, Any]] = []
    control: list[dict[str, Any]] = []
    unknown = 0
    for row in rows:
        easy = row_is_easy(row, manifest_easy)
        if easy is None:
            unknown += 1
            continue
        if easy:
            control.append(row)
        else:
            treatment.append(row)
    if unknown:
        raise SystemExit(f"{source}: {unknown} rows could not be classified as easy/non-easy")
    return treatment, control, unknown


def split_jsonl_artifact(
    source: Path, run_id: str, manifest_easy: dict[str, bool], summary: list[dict[str, Any]]
) -> None:
    rows = read_jsonl(source)
    treatment, control, _ = split_rows(rows, manifest_easy, source)
    out_dir = source.parent / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    treatment_path = out_dir / suffix_path(source.name and Path(source.name), "treatment").name
    control_path = out_dir / suffix_path(source.name and Path(source.name), "control").name
    write_jsonl(treatment_path, treatment)
    write_jsonl(control_path, control)
    summary.append(
        {
            "source": str(source),
            "treatment": str(treatment_path),
            "control": str(control_path),
            "rows": len(rows),
            "treatment_rows": len(treatment),
            "control_rows": len(control),
        }
    )


def split_prompt_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_prompt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        prompt = row.get("prompt_template")
        if not prompt:
            raise SystemExit("row is missing prompt_template")
        by_prompt[str(prompt)].append(row)
    return dict(by_prompt)


def split_existing_outputs_by_prompt(run_dir: Path, run_id: str, summary: list[dict[str, Any]]) -> None:
    for source in sorted(run_dir.glob(f"{run_id}*-[tc]*.jsonl")):
        if not (source.name.endswith("-treatment.jsonl") or source.name.endswith("-control.jsonl")):
            continue
        rows = read_jsonl(source)
        for prompt, prompt_rows in sorted(split_prompt_rows(rows).items()):
            out_path = suffix_path(source, prompt_suffix(prompt))
            write_jsonl(out_path, prompt_rows)
            summary.append(
                {
                    "source": str(source),
                    "prompt": prompt,
                    "out": str(out_path),
                    "rows": len(prompt_rows),
                }
            )

            if "-with-mutation-" in source.name:
                summary_path = out_path.with_name(out_path.stem.replace("-with-mutation", "-with-mutation-summary") + ".md")
                split_name = out_path.stem.removeprefix(f"{run_id}-").replace("-", " ")
                write_mutation_summary(summary_path, prompt_rows, split_name)


def expected_artifacts(results_dir: Path, run_id: str) -> list[Path]:
    candidates = [
        results_dir / f"{run_id}.jsonl",
        results_dir / f"{run_id}-with-mutation.jsonl",
    ]
    return [path for path in candidates if path.exists()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_ids", nargs="+", help="Run IDs to split, e.g. java-v2-mixed-...")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--split-prompts",
        action="store_true",
        help="Also split the generated treatment/control outputs by prompt_template.",
    )
    args = parser.parse_args()

    manifest_easy = load_manifest_easy_labels(args.manifest)
    if not manifest_easy:
        raise SystemExit(f"no task labels loaded from manifest: {args.manifest}")

    all_summaries: list[dict[str, Any]] = []
    for run_id in args.run_ids:
        artifacts = expected_artifacts(args.results_dir, run_id)
        if not artifacts:
            raise SystemExit(f"no generated or with-mutation JSONL artifacts found for run: {run_id}")
        for artifact in artifacts:
            split_jsonl_artifact(artifact, run_id, manifest_easy, all_summaries)

        mutation_path = args.results_dir / f"{run_id}-with-mutation.jsonl"
        if mutation_path.exists():
            rows = read_jsonl(mutation_path)
            treatment, control, _ = split_rows(rows, manifest_easy, mutation_path)
            out_dir = mutation_path.parent / run_id
            write_mutation_summary(
                out_dir / f"{run_id}-with-mutation-summary-treatment.md",
                treatment,
                "treatment (non-easy)",
            )
            write_mutation_summary(
                out_dir / f"{run_id}-with-mutation-summary-control.md",
                control,
                "control (easy)",
            )
        if args.split_prompts:
            split_existing_outputs_by_prompt(args.results_dir / run_id, run_id, all_summaries)

    for item in all_summaries:
        if "prompt" in item:
            print(f"{item['source']}: prompt={item['prompt']} rows={item['rows']}")
            print(f"  out: {item['out']}")
        else:
            print(
                f"{item['source']}: rows={item['rows']} "
                f"treatment={item['treatment_rows']} control={item['control_rows']}"
            )
            print(f"  treatment: {item['treatment']}")
            print(f"  control:   {item['control']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
