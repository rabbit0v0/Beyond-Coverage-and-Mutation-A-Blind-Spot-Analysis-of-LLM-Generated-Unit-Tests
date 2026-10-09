#!/usr/bin/env python3
"""Collect JaCoCo coverage for existing Java pilot result rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from run_java_pilot import ROOT, collect_coverage, parse_jacoco_xml


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--include-failed", action="store_true")
    args = parser.parse_args()

    rows = read_jsonl(args.result)
    out = args.out or args.result.with_name(args.result.stem + "-with-coverage.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    updated = []
    for row in rows:
        workdir = Path(row["workdir"])
        if not workdir.is_absolute():
            workdir = ROOT / workdir
        prompt_name = row["prompt_template"]
        print(f"{row['task_id']} {prompt_name}")
        if row.get("execution_passed") or args.include_failed:
            coverage_outcome = collect_coverage(workdir, prompt_name)
        else:
            coverage_outcome = {
                "coverage_status": "skipped",
                "coverage_error_stage": "test_not_executed",
                "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml"),
            }
        row = {**row, **coverage_outcome}
        updated.append(row)
        print(f"  {row['coverage_status']} {row['coverage_error_stage']} {row['coverage']}")

    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in updated), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
