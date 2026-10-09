#!/usr/bin/env python3
"""Export evidence-derived treatment/control BVA and CFA comparisons."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path

EXCLUDED_RUNS = {"java-v2-mixed-gemma-panta-control-full"}


def collect(path: Path) -> dict:
    if path.is_dir():
        path = path / "blindspot_profiles.jsonl"
        if not path.exists():
            path = path.with_suffix(".jsonl.gz")
    totals = defaultdict(lambda: [0.0, 0])
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = path.read_text(encoding="utf-8")
    rows = [json.loads(text)] if path.suffix == ".json" else [json.loads(line) for line in text.splitlines() if line.strip()]
    for row in rows:
        if "boundary_applicable_items" not in row or "if_else_if_condition_by_construct" not in row:
            raise ValueError("Expected analyzed profiles, not raw result rows")
        if not row.get("execution_passed") or row.get("run_id") in EXCLUDED_RUNS:
            continue
        group = (str(row.get("model_id")), str(row.get("prompt_template")))
        for item in row.get("boundary_applicable_items") or []:
            totals[(*group, "BVA", str(item["category"]))][1] += 1
        for item in row.get("boundary_matched_items") or []:
            totals[(*group, "BVA", str(item["category"]))][0] += 1
        if row.get("extreme_condition_mutation_status") == "passed":
            for kind, stats in (row.get("if_else_if_condition_by_construct") or {}).items():
                denominator = stats.get("scorable")
                if denominator is None:
                    denominator = stats.get("represented", 0)
                totals[(*group, "CFA", kind)][0] += float(stats.get("score_sum") or 0)
                totals[(*group, "CFA", kind)][1] += int(denominator or 0)
    return dict(totals)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--treatment", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output already exists")
    treatment, control = collect(args.treatment), collect(args.control)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["model", "strategy", "metric", "construct", "treatment_numerator",
                         "treatment_denominator", "treatment_score", "control_numerator",
                         "control_denominator", "control_score", "treatment_minus_control"])
        for key in sorted(treatment.keys() | control.keys()):
            t_num, t_den = treatment.get(key, (0, 0))
            c_num, c_den = control.get(key, (0, 0))
            t_score, c_score = (t_num / t_den if t_den else None), (c_num / c_den if c_den else None)
            delta = t_score - c_score if t_score is not None and c_score is not None else None
            writer.writerow([*key, t_num, t_den, t_score, c_num, c_den, c_score, delta])
    print(f"Wrote evidence-derived comparison to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
