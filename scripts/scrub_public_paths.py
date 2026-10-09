#!/usr/bin/env python3
"""Replace machine-local absolute paths in text artifacts.

By default this is a dry run. Pass `--write` to update files in place.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_FILE_BYTES = 100 * 1024 * 1024


LOCAL_PATH_PATTERNS = [
    re.compile(r"/Users/[^,\s\"')]+"),
    re.compile(r"/home/[^,\s\"')]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^,\s\"')]+"),
]


def candidate_files(paths: list[str]) -> list[Path]:
    if paths:
        return [ROOT / path for path in paths]
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def is_text(path: Path) -> bool:
    try:
        return b"\0" not in path.read_bytes()[:4096]
    except OSError:
        return False


def scrub_text(text: str) -> str:
    root_text = str(ROOT)
    text = text.replace(root_text + "/", "")
    text = text.replace(root_text, ".")
    for pattern in LOCAL_PATH_PATTERNS:
        text = pattern.sub("<LOCAL_PATH>", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", help="Optional repo-relative files to scrub")
    parser.add_argument("--write", action="store_true", help="Update files in place")
    args = parser.parse_args()

    changed: list[Path] = []
    skipped: list[Path] = []
    for path in candidate_files(args.paths):
        rel = path.relative_to(ROOT)
        if rel in {
            Path("scripts/audit_public_release.py"),
            Path("scripts/scrub_public_paths.py"),
        }:
            continue
        if not path.exists() or not path.is_file() or not is_text(path):
            continue
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            skipped.append(path)
            continue
        original = path.read_text(encoding="utf-8", errors="ignore")
        scrubbed = scrub_text(original)
        if scrubbed == original:
            continue
        changed.append(path)
        if args.write:
            path.write_text(scrubbed, encoding="utf-8")

    action = "Updated" if args.write else "Would update"
    for path in changed:
        print(f"{action}: {path.relative_to(ROOT)}")
    for path in skipped:
        print(f"Skipped large file: {path.relative_to(ROOT)}")

    if not changed and not skipped:
        print("No local paths found in candidate files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
