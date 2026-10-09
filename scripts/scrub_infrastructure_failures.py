#!/usr/bin/env python3
"""Scrub infrastructure failures from JSONL result files and make rerun manifests.

The intended use is for interrupted/unstable LLM gateway runs. Rows that failed
before a usable model output should not count as model/test-generation failures,
and they should not cause `--resume` to skip those tasks forever.

By default this script is a dry-run. Pass `--apply` to write cleaned files.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INFRA_PATTERNS = [
    "401",
    "unauthorized",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "503",
    "model not available",
    "service unavailable",
    "connection refused",
    "couldn't connect",
    "could not connect",
    "connecterror",
    "readtimeout",
    "timeout",
    "timed out",
    "proxy",
    "upstream",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def text_blob(row: dict[str, Any]) -> str:
    parts = []
    for key in [
        "error_stage",
        "model_error_type",
        "model_error_status_code",
        "model_error_message",
        "coverage_error_stage",
        "mutation_error_stage",
        "exit_code",
    ]:
        if row.get(key) is not None:
            parts.append(str(row.get(key)))
    log_path = row.get("log_path")
    if log_path:
        path = Path(log_path)
        if path.exists():
            parts.append(path.read_text(encoding="utf-8", errors="replace")[-16000:])
    return "\n".join(parts).lower()


def is_placeholder_test(row: dict[str, Any]) -> bool:
    test_file = row.get("test_file")
    preview = ""
    if isinstance(test_file, dict):
        preview = str(test_file.get("preview") or "")
    if not preview:
        return False
    stripped = re.sub(r"\s+", " ", preview)
    return "testPlaceHolder" in preview or "assertTrue(true)" in stripped


def is_infrastructure_failure(row: dict[str, Any], tool: str) -> tuple[bool, str]:
    stage = str(row.get("pipeline_stage") or "")
    error_stage = str(row.get("error_stage") or "")
    if stage == "model_error":
        return True, error_stage or "model_error"
    if error_stage.startswith("model_error"):
        return True, error_stage

    blob = text_blob(row)
    for pattern in INFRA_PATTERNS:
        if pattern in blob:
            return True, f"matched:{pattern}"

    if tool == "panta":
        exit_code = row.get("exit_code")
        if exit_code not in (0, None) and is_placeholder_test(row):
            return True, f"panta_exit_{exit_code}_placeholder"
        if exit_code == 124:
            return True, "panta_timeout"

    if tool == "chatunitest":
        exit_code = row.get("exit_code")
        generated_count = row.get("generated_file_count")
        if exit_code == 124:
            return True, "chatunitest_timeout"
        if exit_code == 0 and generated_count == 0 and (
            "response is null" in blob
            or "failed to get response" in blob
            or "askgpt" in blob
            or "unexpected code response" in blob
        ):
            return True, "chatunitest_no_output_after_model_failure"

    return False, ""


def key_for(row: dict[str, Any], tool: str) -> tuple[str, str]:
    task_id = str(row.get("task_id"))
    if tool == "sota":
        return task_id, str(row.get("prompt_template"))
    if tool == "chatunitest":
        return task_id, str(row.get("phase") or "chatunitest")
    return task_id, "panta"


def expected_keys(manifest_rows: list[dict[str, Any]], limit: int, tool: str, pipeline: str) -> list[tuple[str, str]]:
    selected = manifest_rows[:limit]
    if tool == "sota":
        prompts = {
            "p3": ["p3-intention-planning"],
            "p5": ["p5-mutation-feedback"],
            "both": ["p3-intention-planning", "p5-mutation-feedback"],
        }[pipeline]
        return [(str(task["task_id"]), prompt) for task in selected for prompt in prompts]
    if tool == "chatunitest":
        phase = {"p3": "chattester", "p5": "mutap", "both": "chatunitest"}[pipeline]
        return [(str(task["task_id"]), phase) for task in selected]
    return [(str(task["task_id"]), "panta") for task in selected]


def load_manifest_lookup(path: Path) -> dict[str, dict[str, Any]]:
    return {str(row["task_id"]): row for row in read_jsonl(path)}


def unique_ordered(keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set()
    out = []
    for key in keys:
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tool", choices=["panta", "sota", "chatunitest"], required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument(
        "--pipeline",
        choices=["p3", "p5", "both"],
        default="both",
        help="For --tool sota: p3/p5/both. For --tool chatunitest: p3 means chattester; p5 means mutap.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=Path("pilot-results/scrub-backups"))
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    manifest_rows = read_jsonl(args.manifest)
    manifest_lookup = load_manifest_lookup(args.manifest)
    expected = expected_keys(manifest_rows, args.limit, args.tool, args.pipeline)
    expected_set = set(expected)

    kept = []
    scrubbed = []
    scrubbed_reasons = Counter()
    seen_kept_keys = set()
    for row in rows:
        key = key_for(row, args.tool)
        scrub, reason = is_infrastructure_failure(row, args.tool)
        if scrub:
            row = {**row, "scrub_reason": reason, "scrubbed_utc": datetime.now(timezone.utc).isoformat()}
            scrubbed.append(row)
            scrubbed_reasons[reason] += 1
        else:
            kept.append(row)
            seen_kept_keys.add(key)

    scrubbed_keys = [key_for(row, args.tool) for row in scrubbed]
    missing_keys = [key for key in expected if key not in seen_kept_keys and key not in set(scrubbed_keys)]
    rerun_keys = unique_ordered(scrubbed_keys + missing_keys)

    rerun_task_ids = unique_ordered([(task_id, "") for task_id, _ in rerun_keys])
    rerun_manifest_rows = [manifest_lookup[task_id] for task_id, _ in rerun_task_ids if task_id in manifest_lookup]

    print(f"result={args.result}")
    print(f"rows={len(rows)} kept={len(kept)} scrubbed={len(scrubbed)}")
    print(f"expected_keys={len(expected_set)} kept_expected_keys={len(seen_kept_keys & expected_set)}")
    print(f"scrubbed_keys={len(set(scrubbed_keys))} missing_keys={len(missing_keys)} rerun_keys={len(rerun_keys)}")
    if scrubbed_reasons:
        print("scrub_reasons:")
        for reason, count in scrubbed_reasons.most_common():
            print(f"  {reason}: {count}")
    print(f"rerun_task_ids={len(rerun_manifest_rows)}")

    sidecar_base = args.result.with_suffix("")
    scrubbed_path = sidecar_base.with_name(sidecar_base.name + "-scrubbed-infra.jsonl")
    rerun_keys_path = sidecar_base.with_name(sidecar_base.name + "-rerun-keys.jsonl")
    rerun_manifest_path = sidecar_base.with_name(sidecar_base.name + "-rerun-manifest.jsonl")
    cleaned_path = args.result

    if not args.apply:
        print("dry_run=true; pass --apply to write cleaned result and rerun artifacts")
        print(f"would_write_cleaned={cleaned_path}")
        print(f"would_write_scrubbed={scrubbed_path}")
        print(f"would_write_rerun_keys={rerun_keys_path}")
        print(f"would_write_rerun_manifest={rerun_manifest_path}")
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = args.backup_dir / f"{args.result.name}.{timestamp}.bak"
    shutil.copy2(args.result, backup_path)

    write_jsonl(cleaned_path, kept)
    write_jsonl(scrubbed_path, scrubbed)
    write_jsonl(
        rerun_keys_path,
        [
            {
                "task_id": task_id,
                "prompt_template": prompt if args.tool == "sota" else None,
                "phase": prompt if args.tool == "chatunitest" else None,
            }
            for task_id, prompt in rerun_keys
        ],
    )
    write_jsonl(rerun_manifest_path, rerun_manifest_rows)
    print(f"backup={backup_path}")
    print(f"wrote_cleaned={cleaned_path}")
    print(f"wrote_scrubbed={scrubbed_path}")
    print(f"wrote_rerun_keys={rerun_keys_path}")
    print(f"wrote_rerun_manifest={rerun_manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
