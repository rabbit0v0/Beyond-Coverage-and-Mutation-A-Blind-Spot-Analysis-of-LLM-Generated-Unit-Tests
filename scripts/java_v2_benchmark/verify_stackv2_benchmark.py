#!/usr/bin/env python3
"""Verify the current packaged Java v2 benchmark task structure."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_METADATA = [
    "task_id",
    "language",
    "source_dataset",
    "path",
    "symbol_name",
    "cyclomatic_complexity",
    "cognitive_complexity",
    "self_containment",
    "testability_gate",
]

REQUIRED_FILES = {
    "go": ["task.json", "go.mod", "source.go", "source_test.go"],
    "csharp": ["task.json", "BenchmarkTask.csproj", "Source.cs", "Program.cs"],
    "java": ["task.json", "pom.xml", "src/main/java/benchmark/Subject.java", "src/test/java/benchmark/GeneratedSmokeTest.java"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--language", choices=["java"], default="java")
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    errors: list[str] = []
    for row in rows:
        task_id = row.get("task_id", "<missing>")
        if row.get("language") != args.language:
            errors.append(f"{task_id}: expected language {args.language}")
        for key in REQUIRED_METADATA:
            if key not in row:
                errors.append(f"{task_id}: missing metadata field `{key}`")
        if not row.get("complexity_bucket") and not row.get("v2_primary_split"):
            errors.append(f"{task_id}: missing complexity bucket or v2 split")
        task_dir = Path(row.get("task_dir", ""))
        if not task_dir.exists():
            errors.append(f"{task_id}: missing task_dir {task_dir}")
            continue
        for relative in REQUIRED_FILES[args.language]:
            if not (task_dir / relative).exists():
                errors.append(f"{task_id}: missing {relative}")
        task_json = task_dir / "task.json"
        if task_json.exists():
            metadata = json.loads(task_json.read_text(encoding="utf-8"))
            if metadata.get("task_id") != task_id:
                errors.append(f"{task_id}: task.json mismatch")
    if errors:
        print("benchmark verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"verified {len(rows)} {args.language} benchmark tasks from {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
