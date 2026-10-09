#!/usr/bin/env python3
"""Audit the repository for common public-release hazards."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_GIT_FILE_BYTES = 50 * 1024 * 1024
MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

LOCAL_PATH_PATTERNS = [
    re.compile(r"/Users/[^,\s\"')]+"),
    re.compile(r"(?<![\w/])/home/[^,\s\"')]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^,\s\"')]+"),
]

DISALLOWED_NAMES = {
    ".DS_Store",
}

DISALLOWED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".log",
    ".bak",
    ".tmp",
)


def git_files() -> set[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return {ROOT / line for line in result.stdout.splitlines() if line}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def is_probably_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\0" not in chunk


def scan_text(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_bytes()[:MAX_TEXT_SCAN_BYTES].decode("utf-8", errors="ignore")
    except OSError:
        return findings

    rel = path.relative_to(ROOT)
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"{rel}: possible secret pattern: {pattern.pattern}")
            break
    for pattern in LOCAL_PATH_PATTERNS:
        if rel in {
            Path("scripts/audit_public_release.py"),
            Path("scripts/scrub_public_paths.py"),
        }:
            break
        if pattern.search(text):
            findings.append(f"{rel}: contains local absolute path")
            break
    return findings


def main() -> int:
    tracked = git_files()
    files = candidate_files()
    findings: list[str] = []

    for path in files:
        rel = path.relative_to(ROOT)
        stat = path.stat()

        if path.name in DISALLOWED_NAMES or path.suffix in DISALLOWED_SUFFIXES:
            findings.append(f"{rel}: generated/local file should not be published")

        if stat.st_size > MAX_GIT_FILE_BYTES:
            findings.append(f"{rel}: larger than 50 MiB; archive outside normal Git")

        if path in tracked and (
            path.name in DISALLOWED_NAMES or path.suffix in DISALLOWED_SUFFIXES
        ):
            findings.append(f"{rel}: already tracked but should be removed from Git")

        if is_probably_text(path):
            findings.extend(scan_text(path))

    if findings:
        print("Public-release audit found issues:\n")
        for item in sorted(set(findings)):
            print(f"- {item}")
        return 1

    print("Public-release audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
