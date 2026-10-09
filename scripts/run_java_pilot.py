#!/usr/bin/env python3
"""Run Java LLM test-generation pilot tasks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "java-complexity-v2" / "manifests" / "java_v2_mixed.jsonl"
WORK_ROOT = ROOT / "pilot-workdir" / "java"
RESULT_ROOT = ROOT / "pilot-results"
JAVA_BIN = Path(os.environ.get("JAVA_BIN") or (str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.environ.get("JAVA_HOME") else shutil.which("java") or "java"))
JACOCO_AGENT = Path(
    os.environ.get(
        "JACOCO_AGENT",
        str(
            Path.home()
            / ".m2/repository/org/jacoco/org.jacoco.agent/0.8.14/org.jacoco.agent-0.8.14-runtime.jar"
        ),
    )
)
PROMPTS = {
    "zero-shot": ROOT / "prompts" / "java_zero_shot_plain_main.txt",
    "structured-intent": ROOT / "prompts" / "java_structured_intent_plain_main.txt",
    "access-aware": ROOT / "prompts" / "java_access_aware_plain_main.txt",
}
MINIMAL_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
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


class ModelCallError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_task_ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


def extract_java_code(response_text: str) -> str:
    fenced = re.search(r"```(?:java)?\s*(.*?)```", response_text, re.DOTALL | re.I)
    if fenced:
        response_text = fenced.group(1)
    return response_text.strip() + "\n"


def timeout_output(exc: subprocess.TimeoutExpired) -> str:
    output = exc.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output + f"\n[TIMEOUT] command timed out after {exc.timeout} seconds\n"


def run_command(args: list[str], cwd: Path, log_path: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(args=args, returncode=124, stdout=timeout_output(exc), stderr=None)
    log_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    return result


def prepare_workdir(task: dict[str, Any], run_id: str, prompt_name: str, keep_existing: bool = False) -> Path:
    source_dir = Path(task["task_dir"])
    if not source_dir.is_absolute():
        source_dir = ROOT / source_dir
    workdir = WORK_ROOT / run_id / prompt_name / task["task_id"]
    if keep_existing and workdir.exists():
        return workdir
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(source_dir, workdir)
    # Remove the benchmark-provided placeholder so --resume only reuses files
    # written by a previous LLM call in this run, not the benchmark scaffold.
    placeholder = workdir / "src/test/java/benchmark/GeneratedSmokeTest.java"
    if placeholder.exists():
        placeholder.unlink()
    pom_path = workdir / "pom.xml"
    if not pom_path.exists():
        pom_path.write_text(MINIMAL_POM, encoding="utf-8")
    return workdir


def placeholder_test(task: dict[str, Any]) -> str:
    return (Path(task["task_dir"]) / "src/test/java/benchmark/GeneratedSmokeTest.java").read_text(encoding="utf-8")


def build_prompt(task: dict[str, Any], source_code: str, prompt_name: str) -> str:
    template = PROMPTS[prompt_name].read_text(encoding="utf-8")
    metadata = {
        "task_id": task["task_id"],
        "symbol_name": task["symbol_name"],
        "signature": task.get("signature"),
        "complexity_bucket": task.get("complexity_bucket"),
        "cyclomatic_complexity": task.get("cyclomatic_complexity"),
        "cognitive_complexity": task.get("cognitive_complexity"),
        "self_containment": task.get("self_containment"),
    }
    return template.format(
        symbol_name=task["symbol_name"],
        metadata_json=json.dumps(metadata, indent=2, ensure_ascii=False),
        source_code=source_code,
    )


class ModelClient:
    def __init__(
        self,
        *,
        model: str,
        api: str,
        base_url: str | None,
        api_key: str | None,
        temperature: float,
        max_output_tokens: int,
        model_timeout: int,
        model_retries: int,
    ) -> None:
        self.model = model
        self.api = api
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.model_timeout = model_timeout
        self.model_retries = model_retries
        if self.api == "gemini":
            self.client = None
            return
        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        elif base_url:
            kwargs["api_key"] = "local"
        self.client = OpenAI(**kwargs)

    def call(self, prompt: str) -> tuple[str, dict[str, Any]]:
        system = "You generate concise, executable Java tests. Return only Java code."
        if self.api == "gemini":
            if not self.base_url:
                raise ValueError("--base-url must be the Gemini generateContent endpoint when --api gemini is used")
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": self.max_output_tokens,
                },
            }
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Ocp-Apim-Subscription-Key"] = self.api_key
            request = urllib.request.Request(
                self.base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            last_error: Exception | None = None
            for attempt in range(self.model_retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=self.model_timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    if exc.code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < self.model_retries:
                        last_error = exc
                        time.sleep(min(2 ** attempt, 10))
                        continue
                    raise ModelCallError(
                        f"Gemini generateContent failed with HTTP {exc.code}: {detail}",
                        status_code=exc.code,
                    ) from exc
                except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                    last_error = exc
                    if attempt < self.model_retries:
                        time.sleep(min(2 ** attempt, 10))
                        continue
                    raise ModelCallError(
                        f"Gemini generateContent request failed after {self.model_retries + 1} attempt(s): {exc}"
                    ) from exc
            else:
                raise ModelCallError(f"Gemini generateContent request failed: {last_error}")
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            text = "".join(part.get("text", "") for part in parts)
            usage = data.get("usageMetadata") or {}
            return text, usage
        if self.api == "responses":
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
            )
            usage = response.usage.model_dump() if response.usage else {}
            return response.output_text, usage

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
        )
        message = response.choices[0].message.content or ""
        usage = response.usage.model_dump() if response.usage else {}
        return message, usage


def model_error_record(error: Exception, prompt_name: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
    status_code = getattr(error, "status_code", None)
    error_stage = f"model_error_{status_code}" if status_code else "model_error"
    return {
        "run_id": None,
        "task_id": task.get("task_id") if task else None,
        "language": "java",
        "model_id": None,
        "prompt_template": prompt_name,
        "temperature": None,
        "dry_run": False,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "workdir": None,
        "task": task or {},
        "prompt": None,
        "raw_response": "",
        "extracted_test_code": "",
        "usage": {},
        "compile_passed": False,
        "execution_passed": False,
        "error_stage": error_stage,
        "coverage_status": "skipped",
        "coverage_error_stage": error_stage,
        "coverage": {},
        "model_error_type": type(error).__name__,
        "model_error_status_code": status_code,
        "model_error_message": str(error),
    }


def select_tasks(manifest: Path, limit: int, task_ids: set[str] | None) -> list[dict[str, Any]]:
    tasks = read_jsonl(manifest)
    if task_ids:
        tasks = [task for task in tasks if task["task_id"] in task_ids]
        found = {task["task_id"] for task in tasks}
        missing = sorted(task_ids - found)
        if missing:
            raise SystemExit(f"Task IDs not found: {', '.join(missing)}")
    return tasks[:limit]


def run_generated_test(workdir: Path, prompt_name: str) -> dict[str, Any]:
    compile_result = run_command(["mvn", "-q", "test"], workdir, workdir / f"mvn-test-{prompt_name}.log", timeout=120)
    if compile_result.returncode == 124:
        return {"compile_passed": False, "execution_passed": False, "error_stage": "compile_timeout"}
    if compile_result.returncode != 0:
        return {"compile_passed": False, "execution_passed": False, "error_stage": "compile"}
    exec_result = run_command(
        [
            "mvn",
            "-q",
            "-Dexec.mainClass=benchmark.GeneratedSmokeTest",
            "-Dexec.classpathScope=test",
            "org.codehaus.mojo:exec-maven-plugin:3.5.0:java",
        ],
        workdir,
        workdir / f"execute-{prompt_name}.log",
        timeout=120,
    )
    if exec_result.returncode == 124:
        return {
            "compile_passed": True,
            "execution_passed": False,
            "error_stage": "execute_timeout",
        }
    return {
        "compile_passed": True,
        "execution_passed": exec_result.returncode == 0,
        "error_stage": None if exec_result.returncode == 0 else "execute",
    }


def parse_jacoco_xml(path: Path) -> dict[str, Any]:
    empty = {
        "line_rate": None,
        "lines_covered": None,
        "lines_missed": None,
        "branch_rate": None,
        "branches_covered": None,
        "branches_missed": None,
    }
    if not path.exists():
        return empty
    root = ET.parse(path).getroot()
    sourcefile = None
    for candidate in root.findall(".//sourcefile"):
        if candidate.attrib.get("name") == "Subject.java":
            sourcefile = candidate
            break
    if sourcefile is None:
        return empty

    counters = {counter.attrib["type"]: counter for counter in sourcefile.findall("counter")}

    def values(counter_type: str) -> tuple[int | None, int | None, float | None]:
        counter = counters.get(counter_type)
        if counter is None:
            return None, None, None
        missed = int(counter.attrib.get("missed", 0))
        covered = int(counter.attrib.get("covered", 0))
        total = missed + covered
        return missed, covered, None if total == 0 else covered / total

    line_missed, line_covered, line_rate = values("LINE")
    branch_missed, branch_covered, branch_rate = values("BRANCH")
    return {
        "line_rate": line_rate,
        "lines_covered": line_covered,
        "lines_missed": line_missed,
        "branch_rate": branch_rate,
        "branches_covered": branch_covered,
        "branches_missed": branch_missed,
    }


def collect_coverage(workdir: Path, prompt_name: str) -> dict[str, Any]:
    if not JAVA_BIN.exists():
        return {"coverage_status": "failed", "coverage_error_stage": "java_bin_missing", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}
    if not JACOCO_AGENT.exists():
        return {"coverage_status": "failed", "coverage_error_stage": "jacoco_agent_missing", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}

    compile_result = run_command(["mvn", "-q", "test-compile"], workdir, workdir / f"coverage-test-compile-{prompt_name}.log", timeout=120)
    if compile_result.returncode == 124:
        return {"coverage_status": "failed", "coverage_error_stage": "test-compile-timeout", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}
    if compile_result.returncode != 0:
        return {"coverage_status": "failed", "coverage_error_stage": "test-compile", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}
    exec_result = run_command(
        [
            str(JAVA_BIN),
            f"-javaagent:{JACOCO_AGENT}=destfile=target/jacoco.exec",
            "-cp",
            "target/classes:target/test-classes",
            "benchmark.GeneratedSmokeTest",
        ],
        workdir,
        workdir / f"coverage-execute-{prompt_name}.log",
        timeout=120,
    )
    if exec_result.returncode == 124:
        return {"coverage_status": "failed", "coverage_error_stage": "execute-timeout", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}
    if exec_result.returncode != 0:
        return {"coverage_status": "failed", "coverage_error_stage": "execute", "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml")}
    report_result = run_command(
        ["mvn", "-q", "org.jacoco:jacoco-maven-plugin:0.8.14:report"],
        workdir,
        workdir / f"jacoco-report-{prompt_name}.log",
        timeout=120,
    )
    status = "passed" if report_result.returncode == 0 else "failed"
    return {
        "coverage_status": status,
        "coverage_error_stage": None if status == "passed" else ("report-timeout" if report_result.returncode == 124 else "report"),
        "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml"),
    }


def read_completed_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    keys = set()
    for row in read_jsonl(path):
        task_id = row.get("task_id")
        prompt_template = row.get("prompt_template")
        if task_id and prompt_template:
            keys.add((task_id, prompt_template))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--task-ids", default=None)
    parser.add_argument("--prompt", choices=["zero-shot", "structured-intent", "access-aware", "both", "all"], default="both")
    parser.add_argument("--model", default=os.environ.get("PILOT_MODEL", "gpt-5.4-mini"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--model-timeout", type=int, default=300, help="Per-request model API timeout in seconds.")
    parser.add_argument("--model-retries", type=int, default=2, help="Retry count for transient Gemini API failures.")
    parser.add_argument(
        "--api",
        choices=["responses", "chat", "gemini"],
        default=os.environ.get("PILOT_API", "responses"),
        help="Use OpenAI Responses API, OpenAI-compatible chat completions, or Gemini generateContent REST.",
    )
    parser.add_argument("--base-url", default=os.environ.get("PILOT_BASE_URL"))
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable containing the API key. For local OpenAI-compatible servers, omit it or set any value.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Use existing smoke tests instead of calling an LLM.")
    parser.add_argument("--run-id", default=None, help="Use a fixed run id, useful for resuming an interrupted run.")
    parser.add_argument("--resume", action="store_true", help="Skip rows already present in the result file.")
    args = parser.parse_args()

    if args.prompt == "both":
        prompt_names = ["zero-shot", "structured-intent"]
    elif args.prompt == "all":
        prompt_names = ["zero-shot", "structured-intent", "access-aware"]
    else:
        prompt_names = [args.prompt]
    tasks = select_tasks(args.manifest, args.limit, parse_task_ids(args.task_ids))
    if not tasks:
        raise SystemExit("No tasks selected.")

    api_key = os.environ.get(args.api_key_env)
    client = None
    if not args.dry_run:
        client = ModelClient(
            model=args.model,
            api=args.api,
            base_url=args.base_url,
            api_key=api_key,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            model_timeout=args.model_timeout,
            model_retries=args.model_retries,
        )
    run_id = args.run_id or f"java-{args.model.replace('/', '_')}-{uuid.uuid4().hex[:8]}"
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    results_path = RESULT_ROOT / f"{run_id}.jsonl"
    completed = read_completed_keys(results_path) if args.resume else set()
    print(f"run_id={run_id}")
    if args.resume:
        print(f"resume=true completed_rows={len(completed)} results={results_path}")

    for task in tasks:
        for prompt_name in prompt_names:
            if (task["task_id"], prompt_name) in completed:
                print(f"task={task['task_id']} prompt={prompt_name} skipped=already_completed")
                continue
            print(f"task={task['task_id']} prompt={prompt_name}")
            workdir = prepare_workdir(task, run_id, prompt_name, keep_existing=args.resume)
            source_code = (workdir / "src/main/java/benchmark/Subject.java").read_text(encoding="utf-8")
            prompt = build_prompt(task, source_code, prompt_name)
            test_path = workdir / "src/test/java/benchmark/GeneratedSmokeTest.java"

            try:
                if args.resume and test_path.exists():
                    raw_response = test_path.read_text(encoding="utf-8")
                    usage = {"resume_reused_existing_test": True}
                    test_code = raw_response
                elif args.dry_run:
                    raw_response = placeholder_test(task)
                    usage: dict[str, Any] = {}
                    test_code = raw_response
                else:
                    raw_response, usage = client.call(prompt)  # type: ignore[union-attr]
                    test_code = extract_java_code(raw_response)
            except ModelCallError as exc:
                record = model_error_record(exc, prompt_name, task)
                record.update(
                    {
                        "run_id": run_id,
                        "task_id": task["task_id"],
                        "model_id": args.model,
                        "temperature": args.temperature,
                        "dry_run": args.dry_run,
                        "prompt": prompt,
                    }
                )
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"  compile=False execute=False stage={record['error_stage']}")
                continue

            test_path.write_text(test_code, encoding="utf-8")
            outcome = run_generated_test(workdir, prompt_name)
            coverage_outcome = collect_coverage(workdir, prompt_name) if outcome["execution_passed"] else {
                "coverage_status": "skipped",
                "coverage_error_stage": "test_not_executed",
                "coverage": parse_jacoco_xml(workdir / "target/site/jacoco/jacoco.xml"),
            }
            record = {
                "run_id": run_id,
                "task_id": task["task_id"],
                "language": "java",
                "model_id": args.model,
                "prompt_template": prompt_name,
                "temperature": args.temperature,
                "dry_run": args.dry_run,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "workdir": str(workdir),
                "task": task,
                "prompt": prompt,
                "raw_response": raw_response,
                "extracted_test_code": test_code,
                "usage": usage,
                **outcome,
                **coverage_outcome,
            }
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"  compile={outcome['compile_passed']} "
                f"execute={outcome['execution_passed']} stage={outcome['error_stage']}"
            )

    print(f"results={results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
