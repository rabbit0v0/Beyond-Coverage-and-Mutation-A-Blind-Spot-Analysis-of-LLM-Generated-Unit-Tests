#!/usr/bin/env python3
"""Export compact final-study profiles without changing recorded scores."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from collections import Counter
from pathlib import Path

from compare_complexity import EXCLUDED_RUNS

ROOT = Path(__file__).resolve().parents[1]
LINEAGE = "pit-mutation-v11c-object-observability-broad"
STRATEGIES = {"zero-shot", "p3-intention-planning", "p5-mutation-feedback", "panta"}
CONTROL_MODELS = {"gpt-5.4", "RedHatAI/gemma-4-31B-it-FP8-Dynamic"}
TREATMENT_MODELS = CONTROL_MODELS | {"gemini-3.6-flash", "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"}
FIELDS = {
    "run_id", "task_id", "language", "model_id", "prompt_template", "temperature",
    "complexity_bucket", "target_is_private", "compile_passed", "execution_passed",
    "error_stage", "failure_family", "failure_category", "mutation_score",
    "coverage_line_rate", "coverage_branch_rate",
    "boundary_strength_score", "boundary_applicable_count", "boundary_covered_count",
    "extreme_condition_mutation_status", "if_else_if_condition_total",
    "if_else_if_condition_scorable", "if_else_if_condition_checked_sum",
    "if_else_if_condition_avg_item_score", "if_else_if_condition_checked_items",
    "if_else_if_condition_represented", "if_else_if_condition_by_construct",
    "if_else_if_condition_pit_covered_by_construct", "if_else_if_condition_pit_total_by_construct",
    "assertion_mutation_status", "assertion_mutation_raw_status",
    "assertion_strength_score", "assertion_strength_pessimistic_score",
    "assertion_strength_total_items", "assertion_strength_reached_items",
    "assertion_strength_scorable_items", "assertion_strength_non_representable_items",
    "assertion_strength_checked_items", "assertion_strength_oracle_killed_items",
    "assertion_strength_incidentally_killed_items", "assertion_strength_survived_items",
    "assertion_strength_unmeasurable_by_type_substitution_items",
    "assertion_strength_represented_items", "assertion_strength_path_changed_items",
    "assertion_strength_coverage_unverified_items", "assertion_strength_by_construct",
    "assertion_strength_return_subcategories", "exception_behavior_excluding_broad_original_scope",
}


def compact_profile(row):
    compact = {key: value for key, value in row.items() if key in FIELDS}
    for field in ("boundary_applicable_items", "boundary_matched_items"):
        compact[field] = [{"category": item["category"]} for item in row.get(field) or []]
    compact["assertion_strength_by_construct"] = {
        key: value for key, value in (row.get("assertion_strength_by_construct") or {}).items()
        if key in {"return_behavior", "exception_behavior"}
    }
    return compact


def export_split(source, destination, split):
    models = TREATMENT_MODELS if split == "treatment" else CONTROL_MODELS
    manifest = ROOT / f"benchmarks/java-complexity-v2/manifests/java_v2_mixed_{split}.jsonl"
    task_ids = {json.loads(line)["task_id"] for line in manifest.read_text().splitlines() if line.strip()}
    seen = set()
    counts = Counter()
    skipped = 0
    source_hash = hashlib.sha256()
    with source.open("rb") as incoming, destination.open("wb") as raw_output, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed, \
            io.TextIOWrapper(compressed, encoding="utf-8") as outgoing:
        for line in incoming:
            source_hash.update(line)
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("run_id") in EXCLUDED_RUNS or row.get("model_id") not in models:
                skipped += 1
                continue
            key = (row["model_id"], row["prompt_template"], row["task_id"])
            if key in seen or key[1] not in STRATEGIES or key[2] not in task_ids:
                raise ValueError(f"Unexpected or duplicate {split} configuration/task: {key}")
            seen.add(key)
            counts[key[:2]] += 1
            outgoing.write(json.dumps(compact_profile(row), separators=(",", ":"), ensure_ascii=True) + "\n")
    expected = {(model, strategy, task) for model in models for strategy in STRATEGIES for task in task_ids}
    if seen != expected:
        raise ValueError(f"Incomplete {split} release: missing {len(expected - seen)} task/configuration rows")
    if destination.stat().st_size > 50 * 1024 * 1024:
        raise ValueError(f"{destination.name} exceeds the Git release size limit")
    return {
        "rows": len(seen), "skipped_rows": skipped,
        "source": f"analysis/final-mixed-runs/assertion-strength/{LINEAGE}/{split}/blindspot_profiles.jsonl",
        "source_sha256": source_hash.hexdigest(),
        "profile_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "configurations": [
            {"model": model, "strategy": strategy, "rows": count}
            for (model, strategy), count in sorted(counts.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / f"analysis/final-mixed-runs/assertion-strength/{LINEAGE}")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        parser.error("output directory must be empty")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    inventory = {"schema_version": 1, "analysis_lineage": LINEAGE, "groups": {}}
    for split in ("treatment", "control"):
        directory = args.out_dir / split
        directory.mkdir()
        inventory["groups"][split] = export_split(
            args.source_dir / split / "blindspot_profiles.jsonl",
            directory / "blindspot_profiles.jsonl.gz", split,
        )
    (args.out_dir / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    print(f"Exported compact final-study profiles to {args.out_dir}")


if __name__ == "__main__":
    main()
