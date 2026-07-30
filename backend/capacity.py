"""Storage-capacity health checks with explicit, configurable thresholds."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping


def _env_bytes(
    environment: Mapping[str, str],
    name: str,
    default_gib: float,
) -> int:
    raw = str(environment.get(name) or "").strip()
    if not raw:
        return int(default_gib * 1024 ** 3)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(default_gib * 1024 ** 3)


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_file():
        try:
            return int(root.stat().st_size)
        except OSError:
            return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += int(path.stat().st_size)
        except OSError:
            continue
    return total


def _existing_disk_anchor(path: Path) -> Path:
    candidate = Path(path).resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def capacity_health_checks(
    *,
    db_path: Path,
    source_media_dir: Path,
    backup_dir: Path,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    environment = os.environ if env is None else env
    database_bytes = _tree_bytes(Path(db_path))
    source_bytes = _tree_bytes(Path(source_media_dir))
    backup_bytes = _tree_bytes(Path(backup_dir))
    disk = shutil.disk_usage(_existing_disk_anchor(Path(db_path).parent))
    values = (
        (
            "database_capacity",
            database_bytes,
            _env_bytes(environment, "MEETING_DATABASE_MAX_BYTES", 2),
        ),
        (
            "source_media_capacity",
            source_bytes,
            _env_bytes(environment, "MEETING_SOURCE_MEDIA_MAX_BYTES", 20),
        ),
        (
            "backup_capacity",
            backup_bytes,
            _env_bytes(environment, "MEETING_BACKUP_MAX_BYTES", 20),
        ),
    )
    checks = [
        {
            "name": name,
            "status": "ok" if actual <= maximum else "failed",
            "detail": f"bytes={actual}; threshold={maximum}",
        }
        for name, actual, maximum in values
    ]
    minimum_free = _env_bytes(
        environment,
        "MEETING_MIN_FREE_DISK_BYTES",
        5,
    )
    checks.append({
        "name": "local_disk_free",
        "status": "ok" if int(disk.free) >= minimum_free else "failed",
        "detail": f"free_bytes={int(disk.free)}; threshold={minimum_free}",
    })
    return checks
