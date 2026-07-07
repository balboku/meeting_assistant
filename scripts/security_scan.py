#!/usr/bin/env python3
"""Fail CI when likely secrets or runtime artifacts are tracked in the repo."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "temp",
    "output",
    "backups",
    "logs",
}
SKIP_FILES = {
    ".env",
    "4-QA-005 V01 會議紀錄.docx",
}
SECRET_PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    "ngrok_token_or_key": re.compile(r"\b3G[0-9A-Za-z_-]{20,}\b"),
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
}
RUNTIME_ARTIFACT_PATTERNS = (
    re.compile(r"(^|/)meetings\.db(?:-|$)"),
    re.compile(r"(^|/)(temp|output|backups|logs)/"),
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if path.name in SKIP_FILES:
        return True
    return any(part in SKIP_DIRS for part in rel.parts)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="ignore")


def main() -> int:
    findings: list[str] = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if _should_skip(path):
            continue
        if any(pattern.search(rel) for pattern in RUNTIME_ARTIFACT_PATTERNS):
            findings.append(f"{rel}: tracked runtime artifact")
            continue
        text = _read_text(path)
        if text is None:
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{rel}: possible secret ({name})")

    if findings:
        print("Security scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Security scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
