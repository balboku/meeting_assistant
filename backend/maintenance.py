"""
Operational maintenance helpers for the local SQLite-backed service.

These helpers keep the API layer small: startup can run database maintenance,
health checks can describe local prerequisites, and tests can exercise both
without starting Uvicorn.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping


def backup_database(
    db_path: Path,
    backup_dir: Path,
    now: datetime | None = None,
    keep: int = 5,
) -> Path:
    """Copy the SQLite DB to a timestamped backup and prune older backups."""
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"meetings_{timestamp}.db"
    shutil.copy2(db_path, backup_path)

    backups = sorted(
        backup_dir.glob("meetings_*.db"),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in backups[max(keep, 1):]:
        stale.unlink()

    return backup_path


def maintain_database(db_path: Path) -> dict[str, bool]:
    """Run lightweight SQLite maintenance commands."""
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()

    return {"wal_checkpoint": True, "vacuum": True}


def cleanup_source_media_archives(
    archive_root: Path,
    retention_days: int = 90,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Delete date-bucketed removed source-media backups older than retention."""
    result: dict[str, int | bool] = {
        "enabled": retention_days > 0,
        "deleted_dirs": 0,
        "deleted_files": 0,
        "deleted_bytes": 0,
    }
    if retention_days <= 0 or not archive_root.exists():
        return result

    cutoff_date = (now or datetime.now()).date() - timedelta(days=retention_days)
    try:
        date_dirs = list(archive_root.iterdir())
    except OSError:
        return result

    for date_dir in date_dirs:
        if date_dir.is_symlink() or not date_dir.is_dir() or not re.fullmatch(r"\d{8}", date_dir.name):
            continue
        try:
            archive_date = datetime.strptime(date_dir.name, "%Y%m%d").date()
        except ValueError:
            continue
        if archive_date >= cutoff_date:
            continue

        deleted_files = 0
        deleted_bytes = 0
        try:
            entries = list(date_dir.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                deleted_files += 1
                deleted_bytes += int(entry.stat().st_size)
            except OSError:
                continue
        try:
            shutil.rmtree(date_dir)
        except OSError:
            continue
        result["deleted_dirs"] = int(result["deleted_dirs"]) + 1
        result["deleted_files"] = int(result["deleted_files"]) + deleted_files
        result["deleted_bytes"] = int(result["deleted_bytes"]) + deleted_bytes

    return result


def _path_check(name: str, path: Path, must_contain: tuple[str, ...] = ()) -> dict[str, str]:
    if not path.exists():
        return {"name": name, "status": "failed", "detail": f"路徑不存在：{path}"}
    if not os.access(path, os.R_OK):
        return {"name": name, "status": "failed", "detail": f"路徑不可讀：{path}"}
    if path.is_dir() and not os.access(path, os.W_OK):
        return {"name": name, "status": "failed", "detail": f"路徑不可寫：{path}"}

    missing = [filename for filename in must_contain if not (path / filename).is_file()]
    if missing:
        return {"name": name, "status": "failed", "detail": f"缺少檔案：{', '.join(missing)}"}

    return {"name": name, "status": "ok", "detail": str(path)}


def run_startup_health_checks(
    temp_dir: Path,
    output_dir: Path,
    static_vendor_dir: Path,
    env: Mapping[str, str] | None = None,
    db_path: Path | None = None,
    source_audio_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Return startup prerequisite checks without mutating the filesystem."""
    environment = os.environ if env is None else env
    checks: list[dict[str, str]] = []

    checks.append({
        "name": "gemini_api_key",
        "status": "ok" if environment.get("GEMINI_API_KEY") else "failed",
        "detail": "已設定" if environment.get("GEMINI_API_KEY") else "缺少 GEMINI_API_KEY",
    })
    checks.append(_path_check("temp_dir", temp_dir))
    checks.append(_path_check("output_dir", output_dir))
    if source_audio_dir is not None:
        checks.append(_path_check("source_audio_dir", source_audio_dir))
    checks.append(_path_check(
        "static_vendor",
        static_vendor_dir,
        must_contain=("marked.min.js", "purify.min.js"),
    ))

    if db_path is not None:
        database_ok = db_path.exists() or os.access(db_path.parent, os.W_OK)
        checks.append({
            "name": "database",
            "status": "ok" if database_ok else "failed",
            "detail": str(db_path),
        })

    return checks


def run_startup_maintenance(
    db_path: Path,
    backup_dir: Path,
    backup_keep: int = 5,
) -> dict[str, object]:
    """Back up and maintain the SQLite DB after init_db has ensured it exists."""
    backup_path = backup_database(db_path=db_path, backup_dir=backup_dir, keep=backup_keep)
    maintenance = maintain_database(db_path=db_path)
    return {
        "backup_path": str(backup_path),
        "maintenance": maintenance,
    }
