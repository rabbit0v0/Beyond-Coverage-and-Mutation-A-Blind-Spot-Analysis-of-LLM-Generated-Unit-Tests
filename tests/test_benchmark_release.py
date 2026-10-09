import importlib.util
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest


MANIFESTS = Path(__file__).resolve().parents[1] / "benchmarks/java-complexity-v2/manifests"


def load_manifest(name):
    rows = [json.loads(line) for line in (MANIFESTS / name).read_text().splitlines() if line.strip()]
    indexed = {row["task_id"]: row for row in rows}
    assert len(indexed) == len(rows)
    return indexed


def test_current_release_is_exactly_treatment_and_control():
    treatment = load_manifest("java_v2_mixed_treatment.jsonl")
    control = load_manifest("java_v2_mixed_control.jsonl")
    combined = load_manifest("java_v2_mixed.jsonl")
    assert len(treatment) == 895
    assert len(control) == 700
    assert len(combined) == 1595
    assert not treatment.keys() & control.keys()
    assert combined == {**treatment, **control}
    assert all(not row["v2_category_labels"]["easy"] for row in treatment.values())
    assert all(row["v2_category_labels"]["easy"] for row in control.values())


def test_third_party_register_preserves_original_license_labels():
    path = MANIFESTS.parents[2] / "scripts/package_benchmark_release.py"
    spec = importlib.util.spec_from_file_location("package_benchmark_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = list(load_manifest("java_v2_mixed_control.jsonl").values())
    sources = [json.loads(line) for line in module.third_party_sources(rows).splitlines()]
    assert len(sources) == 700
    assert {row["task_id"] for row in sources} == {row["task_id"] for row in rows}
    original = {row["task_id"]: row for row in rows}
    for row in sources:
        for field in ("license", "repo_name", "revision", "path", "source_dataset"):
            assert row[field] == original[row["task_id"]].get(field)


@pytest.fixture
def notice_package(tmp_path):
    path = MANIFESTS.parents[2] / "scripts/package_benchmark_release.py"
    spec = importlib.util.spec_from_file_location("package_benchmark_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = {"task_id": "task", "repo_name": "owner/repo", "revision": "abc",
              "path": "/Subject.java", "license": ["Apache-2.0"]}
    data = b"Original source GPL notice\n"
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / "README.md").write_text("Upstream notices\n")
    (tmp_path / "notice.txt").write_bytes(data)
    mapping = {**source, "recorded_terms": "GPL-2.0-only", "license_documents": [
        {"archive_path": "third-party-notices/notice.txt", "sha256": sha}],
        "source_notices": "third-party-notices/notice.txt", "source_notices_sha256": sha}
    return module, tmp_path, source, mapping


def write_notice_mapping(directory, mapping):
    (directory / "task-sources.jsonl").write_text(json.dumps(mapping) + "\n")


def test_notice_package_preserves_conflicting_source_terms(notice_package):
    module, directory, source, mapping = notice_package
    write_notice_mapping(directory, mapping)
    (directory / "internal-review.md").write_text("Not for publication")
    files = module.upstream_notices(directory, [source])
    assert "third-party-notices/internal-review.md" not in files
    record = json.loads(files["third-party-notices/task-sources.jsonl"])
    assert record["license"] == ["Apache-2.0"]
    assert record["recorded_terms"] == "GPL-2.0-only"


@pytest.mark.parametrize("problem", ["hash", "provenance", "traversal", "missing-task", "duplicate-task"])
def test_notice_package_rejects_invalid_evidence(notice_package, problem):
    module, directory, source, mapping = notice_package
    if problem == "hash":
        mapping["license_documents"][0]["sha256"] = "0" * 64
    elif problem == "provenance":
        mapping["revision"] = "different"
    elif problem == "traversal":
        mapping["license_documents"][0]["archive_path"] = "third-party-notices/../notice.txt"
    elif problem == "missing-task":
        mapping["task_id"] = "other"
    write_notice_mapping(directory, mapping)
    if problem == "duplicate-task":
        with (directory / "task-sources.jsonl").open("a") as output:
            output.write(json.dumps(mapping) + "\n")
    with pytest.raises(ValueError):
        module.upstream_notices(directory, [source])


def test_source_exclusions_retain_complete_study_population(notice_package):
    module, _, _, _ = notice_package
    rows = list(load_manifest("java_v2_mixed.jsonl").values())
    exclusions = module.source_exclusions(rows)
    assert len(rows) == 1595
    assert len(exclusions) == 10
    assert len(exclusions.keys() & load_manifest("java_v2_mixed_treatment.jsonl").keys()) == 5
    assert len(exclusions.keys() & load_manifest("java_v2_mixed_control.jsonl").keys()) == 5
    assert all(entry["reason"] and entry["evidence_url"] for entry in exclusions.values())


@pytest.mark.parametrize("problem", ["unknown", "duplicate", "repo", "reason"])
def test_source_exclusions_validate_provenance(notice_package, problem):
    module, directory, source, _ = notice_package
    entry = {"task_id": source["task_id"], "repo_name": source["repo_name"], "reason": "Unresolved source terms"}
    if problem == "unknown":
        entry["task_id"] = "other"
    elif problem == "repo":
        entry["repo_name"] = "different/repo"
    elif problem == "reason":
        entry["reason"] = " "
    entries = [entry, entry] if problem == "duplicate" else [entry]
    path = directory / "exclusions.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": entries}))
    with pytest.raises(ValueError):
        module.source_exclusions([source], path)


def test_benchmark_packaging_withholds_source_not_metadata(notice_package, monkeypatch):
    module, directory, _, _ = notice_package
    monkeypatch.setattr(module, "ROOT", directory)
    registry = directory / module.BASE / "source-exclusions.json"
    monkeypatch.setattr(module, "SOURCE_EXCLUSIONS", registry)
    rows = [{"task_id": f"task-{index}", "repo_name": "owner/repo",
             "task_dir": str(module.BASE / "tasks" / f"task-{index}")} for index in range(1595)]
    exclusions = [{"task_id": row["task_id"], "repo_name": row["repo_name"], "reason": "Unresolved"} for row in rows[1:]]
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"schema_version": 1, "tasks": exclusions}))
    manifests = registry.parent / "manifests"
    manifests.mkdir()
    manifest_data = "\n".join(json.dumps(row) for row in rows) + "\n"
    for name in module.MANIFEST_NAMES:
        (manifests / (name + ".jsonl")).write_text(manifest_data)
    for path in (directory / "LICENSE", directory / "THIRD_PARTY_NOTICES.md", directory / "SOURCE_DISTRIBUTION.md", registry.parent / "README.md"):
        path.write_text("Test fixture\n")
    task = directory / rows[0]["task_dir"]
    source = task / "src/main/java/benchmark/Subject.java"
    source.parent.mkdir(parents=True)
    source.write_text("package benchmark; public class Subject {}\n")
    (task / "pom.xml").write_text("<project/>\n")
    notices = directory / "notices"
    notices.mkdir()
    (notices / "README.md").write_text("Test notices\n")
    notice = b"Test source notice\n"
    (notices / "notice.txt").write_bytes(notice)
    sha = hashlib.sha256(notice).hexdigest()
    notice_rows = [{**row, "license_documents": [], "source_notices": "third-party-notices/notice.txt",
                    "source_notices_sha256": sha, "review_status": "hold" if index else "documented-grant"} for index, row in enumerate(rows)]
    (notices / "task-sources.jsonl").write_text("\n".join(json.dumps(row) for row in notice_rows) + "\n")
    destination = directory / "release.zip"
    monkeypatch.setattr(sys, "argv", ["packager", "--out", str(destination), "--notices-dir", str(notices)])
    assert module.main() == 0
    with zipfile.ZipFile(destination) as archive:
        assert archive.read(str(source.relative_to(directory))) == source.read_bytes()
        assert len([name for name in archive.namelist() if name.endswith("/task.json")]) == 1595
        assert not any("/task-1/src/" in name for name in archive.namelist())
        assert archive.read(str(module.BASE / "manifests/java_v2_mixed.jsonl")).decode() == manifest_data
    inventory = json.loads(destination.with_suffix(".inventory.json").read_text())
    assert inventory["tasks"] == 1595
    assert inventory["distributed_source_tasks"] == 1
    notice_rows[0]["review_status"] = "hold"
    (notices / "task-sources.jsonl").write_text("\n".join(json.dumps(row) for row in notice_rows) + "\n")
    other_destination = directory / "unresolved.zip"
    monkeypatch.setattr(sys, "argv", ["packager", "--out", str(other_destination), "--notices-dir", str(notices)])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert not other_destination.exists()
