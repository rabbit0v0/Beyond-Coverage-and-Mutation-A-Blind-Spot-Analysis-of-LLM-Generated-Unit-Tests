#!/usr/bin/env python3
"""Package current Java v2 fixtures with portable metadata and SHA-256 inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("benchmarks/java-complexity-v2")
MANIFEST_NAMES = ("java_v2_mixed", "java_v2_mixed_treatment", "java_v2_mixed_control")
SOURCE_EXCLUSIONS = ROOT / BASE / "source-exclusions.json"


def source_exclusions(rows: list[dict], path: Path = SOURCE_EXCLUSIONS) -> dict[str, dict]:
    """Withhold fixtures, never historical task metadata or evaluation results."""
    document = json.loads(path.read_text())
    if document.get("schema_version") != 1:
        raise ValueError("unsupported source exclusion schema")
    sources = {row["task_id"]: row for row in rows}
    exclusions = {}
    for entry in document["tasks"]:
        task_id = entry["task_id"]
        if task_id not in sources or task_id in exclusions:
            raise ValueError(f"unknown or duplicate source exclusion: {task_id}")
        if entry["repo_name"] != sources[task_id]["repo_name"] or not entry.get("reason", "").strip():
            raise ValueError(f"invalid source exclusion provenance or reason: {task_id}")
        exclusions[task_id] = entry
    return exclusions


def third_party_sources(rows: list[dict]) -> bytes:
    fields = (
        "task_id", "source_dataset", "repo_name", "revision", "path",
        "start_line", "end_line", "license",
    )
    records = (
        json.dumps({field: row.get(field) for field in fields}, sort_keys=True)
        for row in sorted(rows, key=lambda row: row["task_id"])
    )
    return ("\n".join(records) + "\n").encode()


def upstream_notices(directory: Path, rows: list[dict]) -> dict[str, bytes]:
    """Validate task provenance and include only referenced notice documents."""
    root = directory.resolve()
    files = {}

    def include(name: str, expected_hash: str | None = None) -> None:
        path = Path(name)
        if not path.parts or path.is_absolute() or ".." in path.parts or path.parts[0] != "third-party-notices":
            raise ValueError(f"invalid notice archive path: {name}")
        source = root.joinpath(*path.parts[1:])
        if not source.resolve().is_relative_to(root) or source.is_symlink():
            raise ValueError(f"notice path escapes directory: {name}")
        data = source.read_bytes()
        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError(f"notice hash mismatch: {name}")
        files[name] = data

    include("third-party-notices/README.md")
    include("third-party-notices/task-sources.jsonl")
    records = [json.loads(line) for line in files["third-party-notices/task-sources.jsonl"].split(b"\n") if line]
    sources = {row["task_id"]: row for row in rows}
    if len(records) != len(sources) or {row["task_id"] for row in records} != sources.keys():
        raise ValueError("notice mapping must contain each benchmark task exactly once")
    for record in records:
        for field in ("repo_name", "revision", "path", "license"):
            if record.get(field) != sources[record["task_id"]].get(field):
                raise ValueError(f"notice provenance mismatch: {record['task_id']} {field}")
        for document in record["license_documents"]:
            if not document["sha256"]:
                raise ValueError("license document hash is required")
            include(document["archive_path"], document["sha256"])
        if not record["source_notices_sha256"]:
            raise ValueError("source notice hash is required")
        include(record["source_notices"], record["source_notices_sha256"])
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--notices-dir", type=Path, required=True, help="Consolidated notices directory containing README.md and task-sources.jsonl")
    args = parser.parse_args()
    if args.out.exists() or args.out.with_suffix(".inventory.json").exists():
        parser.error("output archive and inventory must not already exist")
    manifests = {}
    fixtures = []
    inventory = {}
    for name in MANIFEST_NAMES:
        path = ROOT / BASE / "manifests" / f"{name}.jsonl"
        manifests[path.relative_to(ROOT)] = path.read_bytes()
    rows = [json.loads(line) for line in manifests[BASE / "manifests/java_v2_mixed.jsonl"].decode().splitlines()]
    if len(rows) != 1595 or len({row["task_id"] for row in rows}) != 1595:
        parser.error("expected the current 1595-task benchmark")
    try:
        exclusions = source_exclusions(rows, SOURCE_EXCLUSIONS)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    notices = {}
    if args.notices_dir:
        try:
            notices = upstream_notices(args.notices_dir, rows)
        except (ValueError, OSError, KeyError) as error:
            parser.error(str(error))
        records = [json.loads(line) for line in notices["third-party-notices/task-sources.jsonl"].split(b"\n") if line]
        unresolved = [row["task_id"] for row in records if row.get("review_status") == "hold" and row["task_id"] not in exclusions]
        if unresolved:
            parser.error(f"unresolved notice cases are not in the exclusion register: {', '.join(unresolved)}")
    for row in rows:
        directory = ROOT / row["task_dir"]
        if not directory.resolve().is_relative_to((ROOT / BASE / "tasks").resolve()):
            parser.error(f"task directory is outside current benchmark: {row['task_id']}")
        if row["task_id"] in exclusions:
            continue
        if not (directory / "pom.xml").is_file() or not (directory / "src/main/java/benchmark/Subject.java").is_file():
            parser.error(f"missing fixture: {row['task_id']}")
        files = [directory / "pom.xml", *sorted((directory / "src").rglob("*.java"))]
        for path in files:
            if path.is_symlink():
                parser.error(f"unexpected symlink: {path.relative_to(ROOT)}")
            fixtures.append(path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("LICENSE",):
            data = (ROOT / name).read_bytes()
            archive.writestr(name, data)
            inventory[name] = hashlib.sha256(data).hexdigest()
        data = third_party_sources(rows)
        archive.writestr("THIRD_PARTY_SOURCES.jsonl", data)
        inventory["THIRD_PARTY_SOURCES.jsonl"] = hashlib.sha256(data).hexdigest()
        name = str(BASE / "source-exclusions.json")
        data = SOURCE_EXCLUSIONS.read_bytes()
        archive.writestr(name, data)
        inventory[name] = hashlib.sha256(data).hexdigest()
        for path in (ROOT / BASE / "README.md", ROOT / "SOURCE_DISTRIBUTION.md"):
            name = str(path.relative_to(ROOT))
            data = path.read_bytes()
            archive.writestr(name, data)
            inventory[name] = hashlib.sha256(data).hexdigest()
        for name, data in sorted(notices.items()):
            archive.writestr(name, data)
            inventory[name] = hashlib.sha256(data).hexdigest()
        for name, data in manifests.items():
            archive.writestr(str(name), data)
            inventory[str(name)] = hashlib.sha256(data).hexdigest()
        for path in fixtures:
            name = str(path.relative_to(ROOT))
            data = path.read_bytes()
            archive.writestr(name, data)
            inventory[name] = hashlib.sha256(data).hexdigest()
        for row in rows:
            name = str(Path(row["task_dir"]) / "task.json")
            data = (json.dumps(row, sort_keys=True) + "\n").encode()
            archive.writestr(name, data)
            inventory[name] = hashlib.sha256(data).hexdigest()
    metadata = {
        "benchmark": "ObligBench-java-v2", "tasks": len(rows),
        "distributed_source_tasks": len(rows) - len(exclusions),
        "withheld_source_tasks": sorted(exclusions),
        "archive_sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
        "files": inventory,
        "original_material_license": "MIT",
        "third_party_license_scope": "Upstream terms retained; see SOURCE_DISTRIBUTION.md",
        "upstream_notices_included": bool(notices),
        "redistribution_status": "Notice inclusion does not override upstream conditions or resolve recorded restrictions",
    }
    args.out.with_suffix(".inventory.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Packaged {len(rows)} tasks; SHA-256 {metadata['archive_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
