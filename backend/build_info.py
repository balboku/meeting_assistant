"""Runtime build fingerprint for diagnosing stale local service processes."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_VERSION = "2.7.0"
SOURCE_SUFFIXES = {".py", ".html", ".js", ".css", ".ps1"}


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _source_files() -> list[Path]:
    candidates: list[Path] = []
    for directory_name in ("backend", "scripts", "static"):
        directory = ROOT_DIR / directory_name
        if directory.is_dir():
            candidates.extend(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and path.suffix.lower() in SOURCE_SUFFIXES
                and "__pycache__" not in path.parts
            )
    for relative in ("start.py", "requirements.txt"):
        path = ROOT_DIR / relative
        if path.is_file():
            candidates.append(path)
    return sorted(set(candidates), key=lambda path: path.as_posix().lower())


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in _source_files():
        relative = path.relative_to(ROOT_DIR).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def _workspace_state() -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    status = _git(
        "status",
        "--porcelain",
        "--untracked-files=normal",
        "--",
        "backend",
        "scripts",
        "static/index.html",
        "start.py",
        "requirements.txt",
    )
    return {
        "commit": commit,
        "dirty": bool(status) if status is not None else None,
        "source_fingerprint": _source_fingerprint(),
    }


LOADED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
LOADED_STATE = _workspace_state()


def build_info_payload() -> dict[str, Any]:
    current = _workspace_state()
    return {
        "loaded_at": LOADED_AT,
        "loaded_commit": LOADED_STATE["commit"],
        "loaded_dirty": LOADED_STATE["dirty"],
        "loaded_source_fingerprint": LOADED_STATE["source_fingerprint"],
        "workspace_commit": current["commit"],
        "workspace_dirty": current["dirty"],
        "workspace_source_fingerprint": current["source_fingerprint"],
        "matches_workspace": (
            LOADED_STATE["commit"] == current["commit"]
            and LOADED_STATE["source_fingerprint"] == current["source_fingerprint"]
        ),
    }
