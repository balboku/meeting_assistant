#!/usr/bin/env python3
"""Create an immediate verified database and full record backup."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.maintenance import run_startup_maintenance, verify_database_backup


def _env_path(name: str, default: str) -> Path:
    value = str(os.getenv(name) or default).strip()
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _active_job_count(db_path: Path) -> int:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        jobs_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not jobs_table:
            return 0
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def main() -> int:
    db_path = _env_path("DB_PATH", "meetings.db")
    if not db_path.is_file():
        print(f"找不到資料庫，未建立備份：{db_path}", file=sys.stderr)
        return 2
    active_jobs = _active_job_count(db_path)
    if active_jobs:
        print(
            f"仍有 {active_jobs} 筆 pending/processing 任務，未強制備份。",
            file=sys.stderr,
        )
        return 2

    offsite_value = str(os.getenv("MEETING_OFFSITE_BACKUP_DIR") or "").strip()
    offsite_dir = _env_path("MEETING_OFFSITE_BACKUP_DIR", "") if offsite_value else None
    legacy_interval = _positive_int("FULL_SNAPSHOT_MIN_INTERVAL_HOURS", 168)
    result = run_startup_maintenance(
        db_path,
        _env_path("MEETING_BACKUP_DIR", "backups"),
        source_media_dir=_env_path(
            "MEETING_SOURCE_AUDIO_DIR",
            "output/source_audio",
        ),
        previous_minutes_dir=_env_path(
            "MEETING_PREVIOUS_MINUTES_DIR",
            "output/previous_minutes",
        ),
        offsite_backup_dir=offsite_dir,
        backup_keep=_positive_int("DB_BACKUP_KEEP", 4),
        backup_min_interval_hours=_positive_int(
            "BACKUP_MIN_INTERVAL_HOURS",
            legacy_interval,
        ),
        force_backup=True,
    )
    database_verification = verify_database_backup(Path(result["backup_path"]))
    payload = {**result, "database_verification": database_verification}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    snapshot_ok = bool(
        result["snapshot_verification"].get("recoverability_complete")
    )
    offsite = result.get("offsite_snapshot_verification")
    offsite_ok = offsite is None or bool(offsite.get("recoverability_complete"))
    return 0 if database_verification["ok"] and snapshot_ok and offsite_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
