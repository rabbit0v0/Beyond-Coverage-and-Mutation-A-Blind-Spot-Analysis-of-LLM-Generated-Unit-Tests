#!/usr/bin/env python3
"""Run PANTA on Java benchmark tasks.

This wrapper adapts our self-contained `benchmark.Subject` tasks to PANTA's
project-level config format. It keeps PANTA itself as an external checkout under
`external-tools/Panta` and writes one JSONL result row per task.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/java-complexity-v2/manifests/java_v2_mixed.jsonl"
DEFAULT_PANTA_DIR = ROOT / "external-tools/Panta"
WORK_ROOT = ROOT / "pilot-workdir/java"
RESULT_ROOT = ROOT / "pilot-results"

PANTA_TEST_CLASS = """package benchmark;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import org.mockito.*;
import java.util.*;

public class PantaGeneratedTest {

    @Test
    void testPlaceHolder() {
        assertTrue(true);
    }
}
"""

JUNIT_AND_MOCKITO_DEPS = """
<dependency>
  <groupId>org.junit.jupiter</groupId>
  <artifactId>junit-jupiter-api</artifactId>
  <version>5.10.2</version>
  <scope>test</scope>
</dependency>
<dependency>
  <groupId>org.junit.jupiter</groupId>
  <artifactId>junit-jupiter-engine</artifactId>
  <version>5.10.2</version>
  <scope>test</scope>
</dependency>
<dependency>
  <groupId>org.mockito</groupId>
  <artifactId>mockito-core</artifactId>
  <version>5.12.0</version>
  <scope>test</scope>
</dependency>
<dependency>
  <groupId>org.mockito</groupId>
  <artifactId>mockito-junit-jupiter</artifactId>
  <version>5.12.0</version>
  <scope>test</scope>
</dependency>
"""

SUREFIRE_PLUGIN = """
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-surefire-plugin</artifactId>
  <version>3.2.5</version>
</plugin>
"""

JACOCO_PLUGIN = """
<plugin>
  <groupId>org.jacoco</groupId>
  <artifactId>jacoco-maven-plugin</artifactId>
  <version>0.8.12</version>
  <configuration>
    <outputDirectory>${project.build.directory}/jacoco</outputDirectory>
  </configuration>
  <executions>
    <execution>
      <goals>
        <goal>prepare-agent</goal>
      </goals>
    </execution>
    <execution>
      <id>report</id>
      <phase>test</phase>
      <goals>
        <goal>report</goal>
      </goals>
    </execution>
  </executions>
</plugin>
"""

MINIMAL_JAVA17_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>benchmark</groupId>
  <artifactId>task</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
"""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_task_ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


def select_tasks(manifest: Path, limit: int, task_ids: set[str] | None) -> list[dict[str, Any]]:
    tasks = read_jsonl(manifest)
    if task_ids:
        tasks = [task for task in tasks if task["task_id"] in task_ids]
        found = {task["task_id"] for task in tasks}
        missing = sorted(task_ids - found)
        if missing:
            raise SystemExit(f"Task IDs not found: {', '.join(missing)}")
    return tasks[:limit]


def completed_task_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(row.get("task_id")) for row in read_jsonl(path) if row.get("task_id")}


def timeout_output(exc: subprocess.TimeoutExpired) -> str:
    output = exc.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output + f"\n[TIMEOUT] command timed out after {exc.timeout} seconds\n"


def run_command(args: list[str], cwd: Path, log_path: Path, timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(args=args, returncode=124, stdout=timeout_output(exc), stderr=None)
    log_path.write_text(result.stdout or "", encoding="utf-8", errors="replace")
    return result


def prepare_workdir(task: dict[str, Any], run_id: str, keep_existing: bool) -> Path:
    source_dir = Path(task["task_dir"])
    if not source_dir.is_absolute():
        source_dir = ROOT / source_dir
    workdir = WORK_ROOT / run_id / "panta" / task["task_id"]
    if keep_existing and workdir.exists():
        return workdir
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(source_dir, workdir)
    return workdir


def ensure_test_file(workdir: Path) -> Path:
    test_file = workdir / "src/test/java/benchmark/PantaGeneratedTest.java"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    if not test_file.exists() or test_file.stat().st_size == 0:
        test_file.write_text(PANTA_TEST_CLASS, encoding="utf-8")
    return test_file


def ensure_pom_dependencies(workdir: Path) -> bool:
    pom = workdir / "pom.xml"
    if not pom.exists():
        pom.write_text(MINIMAL_JAVA17_POM, encoding="utf-8")
    text = pom.read_text(encoding="utf-8")
    changed = False

    if "<dependencies>" not in text:
        text = text.replace("</project>", "<dependencies></dependencies></project>")
        changed = True
    if "org.junit.jupiter" not in text:
        text = text.replace("</dependencies>", JUNIT_AND_MOCKITO_DEPS + "\n</dependencies>")
        changed = True

    if "<build>" not in text:
        text = text.replace("</project>", "<build><plugins></plugins></build></project>")
        changed = True
    elif "<plugins>" not in text:
        text = text.replace("</build>", "<plugins></plugins></build>")
        changed = True
    if "maven-surefire-plugin" not in text:
        text = text.replace("</plugins>", SUREFIRE_PLUGIN + "\n</plugins>")
        changed = True
    if "jacoco-maven-plugin" not in text:
        text = text.replace("</plugins>", JACOCO_PLUGIN + "\n</plugins>")
        changed = True

    if changed:
        pom.write_text(text, encoding="utf-8")
    return changed


def write_panta_config(
    *,
    workdir: Path,
    test_file: Path,
    model: str,
    api_type: str,
    prompt_type: str,
    maximum_iterations: int,
    enable_fixing: int,
    target_coverage: int,
    report_name: str,
) -> Path:
    config_path = workdir / "panta-config.ini"
    conf = configparser.ConfigParser()
    conf["default"] = {
        "project_directory": str(workdir),
        "source_code_file": str(workdir / "src/main/java/benchmark/Subject.java"),
        "test_code_file": str(test_file),
        "test_file_output_path": "",
        "code_coverage_report_path": str(workdir / "target/jacoco/jacoco.csv"),
        "test_execution_command": "mvn -q test jacoco:report -Dtest=PantaGeneratedTest",
        "test_dependency_command": "mvn -q dependency:list -DincludeScope=test -DexcludeTransitive=true",
        "junit_version": "5",
        "test_code_command_dir": str(workdir),
        "model": model,
        "api_type": api_type,
        "coverage_type": "jacoco",
        "report_filepath": report_name,
        "target_coverage": str(target_coverage),
        "maximum_iterations": str(maximum_iterations),
        "no_coverage_increase_iterations": "1",
        "prompt_type": prompt_type,
        "pick_two_paths": "true",
        "enable_fixing": str(enable_fixing),
        "run_symprompt": "false",
        "included_files": "",
        "additional_instructions": (
            "Generate JUnit 5 tests in package benchmark for benchmark.Subject. "
            "Prefer public APIs and reflection only when necessary. "
            "Do not rely on external project classes outside this self-contained task."
        ),
    }
    with config_path.open("w", encoding="utf-8") as handle:
        conf.write(handle)
    return config_path


def collect_file_preview(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "bytes": path.stat().st_size, "preview": text[:4000]}


def safe_label(value: str) -> str:
    return str(value).replace("/", "_").replace(":", "_")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--panta-dir", type=Path, default=DEFAULT_PANTA_DIR)
    parser.add_argument("--python", default=str(DEFAULT_PANTA_DIR / ".venv/bin/python"))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--task-ids", default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--api",
        choices=["litellm", "openai-sdk", "gemini"],
        default=os.environ.get("PANTA_API", "litellm"),
        help="Use PANTA's default LiteLLM backend, OpenAI SDK directly, or direct Gemini generateContent REST.",
    )
    parser.add_argument("--api-base", default=os.environ.get("PANTA_API_BASE") or os.environ.get("OPENAI_API_BASE"))
    parser.add_argument("--api-key-env", default="AALTO_API_KEY")
    parser.add_argument("--service-tier", default=os.environ.get("PANTA_SERVICE_TIER") or os.environ.get("OPENAI_SERVICE_TIER"))
    parser.add_argument("--prompt-type", choices=["baseline", "control", "coverage"], default="control")
    parser.add_argument("--maximum-iterations", type=int, default=1)
    parser.add_argument("--enable-fixing", type=int, default=0)
    parser.add_argument("--target-coverage", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get(args.api_key_env):
        raise SystemExit(f"No API key found. Set {args.api_key_env}, or use --dry-run.")
    if not args.dry_run and not args.api_base:
        raise SystemExit("No API base found. Pass --api-base or set PANTA_API_BASE/OPENAI_API_BASE.")
    if not args.dry_run and args.api == "gemini" and ":generateContent" not in args.api_base:
        raise SystemExit("--api gemini requires --api-base to be the full Gemini :generateContent endpoint.")

    panta_dir = args.panta_dir
    if not (panta_dir / "src/panta/main.py").exists():
        raise SystemExit(f"PANTA checkout not found: {panta_dir}")

    tasks = select_tasks(args.manifest, args.limit, parse_task_ids(args.task_ids))
    run_id = args.run_id or f"java-panta-{uuid.uuid4().hex[:8]}"
    results_path = RESULT_ROOT / f"{run_id}.jsonl"
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    done = completed_task_ids(results_path) if args.resume else set()

    print(f"run_id={run_id}")
    print(f"results={results_path}")

    for task in tasks:
        task_id = task["task_id"]
        if task_id in done:
            print(f"task={task_id} skipped=already_completed")
            continue

        workdir = prepare_workdir(task, run_id, keep_existing=args.resume)
        pom_augmented = ensure_pom_dependencies(workdir)
        test_file = ensure_test_file(workdir)
        config_path = write_panta_config(
            workdir=workdir,
            test_file=test_file,
            model=args.model,
            api_type=args.api,
            prompt_type=args.prompt_type,
            maximum_iterations=args.maximum_iterations,
            enable_fixing=args.enable_fixing,
            target_coverage=args.target_coverage,
            report_name=f"{task_id}.html",
        )
        log_path = workdir / "panta.log"
        cmd = [args.python, "-m", "panta.main"]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(panta_dir / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["PANTA_CONFIG_PATH"] = str(config_path)
        if args.api_base:
            env["PANTA_API_BASE"] = args.api_base
            env["OPENAI_API_BASE"] = args.api_base
        if args.api in {"gemini", "openai-sdk"}:
            env["PANTA_API"] = args.api
        if args.service_tier:
            env["PANTA_SERVICE_TIER"] = args.service_tier
            env["OPENAI_SERVICE_TIER"] = args.service_tier
        api_key = env.get(args.api_key_env, "")
        if api_key:
            env["PANTA_API_KEY"] = api_key
            env["OPENAI_API_KEY"] = api_key

        print(f"task={task_id}")
        if args.dry_run:
            result = subprocess.CompletedProcess(cmd, returncode=None, stdout="dry-run: command not executed\n")
            log_path.write_text(" ".join(cmd) + "\n", encoding="utf-8")
        else:
            result = run_command(cmd, panta_dir, log_path, timeout=args.timeout, env=env)

        generated_test = collect_file_preview(test_file)
        placeholder_unchanged = bool(
            generated_test
            and generated_test.get("bytes") == len(PANTA_TEST_CLASS.encode("utf-8"))
            and "void testPlaceHolder()" in str(generated_test.get("preview") or "")
        )
        effective_exit_code = 2 if result.returncode == 0 and placeholder_unchanged and not args.dry_run else result.returncode
        report_root = panta_dir / "result-files" / f"{args.prompt_type}_{safe_label(args.model)}"
        report_candidates = sorted(report_root.glob(f"Subject_{args.prompt_type}_{task_id}.html*")) if report_root.exists() else []
        row = {
            "run_id": run_id,
            "task_id": task_id,
            "language": "java",
            "tool": "panta",
            "model_id": args.model,
            "api": args.api,
            "api_base": args.api_base,
            "service_tier": args.service_tier,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "workdir": str(workdir),
            "task": task,
            "pom_augmented_for_panta": pom_augmented,
            "config_path": str(config_path),
            "command": cmd,
            "exit_code": effective_exit_code,
            "panta_process_exit_code": result.returncode,
            "panta_generation_failed": placeholder_unchanged,
            "panta_failure_reason": "placeholder_test_unchanged" if placeholder_unchanged else None,
            "log_path": str(log_path),
            "test_file": generated_test,
            "report_candidates": [str(path) for path in report_candidates],
        }
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  exit={effective_exit_code} process_exit={result.returncode} test_bytes={(generated_test or {}).get('bytes', 0)}")

    print(f"wrote {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
