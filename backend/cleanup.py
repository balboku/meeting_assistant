"""
Temporary file cleanup helpers.

The durable queue keeps uploaded source audio on disk until a job succeeds or
terminally fails. Startup cleanup must therefore protect source files referenced
by pending/processing jobs while still removing orphaned leftovers.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from backend.database import delete_terminal_jobs_completed_before, get_db

logger = logging.getLogger("MeetingAssistant.Cleanup")


def _resolved_paths(paths: Iterable[Path]) -> set[Path]:
    resolved: set[Path] = set()
    for path in paths:
        try:
            resolved.add(Path(path).resolve())
        except OSError:
            resolved.add(Path(path).absolute())
    return resolved


def cleanup_stale_temp_files(
    temp_dir: Path,
    active_paths: Iterable[Path],
    max_age_seconds: int,
    now: float | None = None,
) -> list[Path]:
    """Delete stale files under temp_dir unless an active job still references them."""
    if not temp_dir.exists():
        return []

    cutoff_now = time.time() if now is None else now
    protected = _resolved_paths(active_paths)
    deleted: list[Path] = []

    for path in temp_dir.rglob("*"):
        if not path.is_file():
            continue

        try:
            resolved = path.resolve()
            if resolved in protected:
                continue

            age_seconds = cutoff_now - path.stat().st_mtime
            if age_seconds < max_age_seconds:
                continue

            path.unlink()
            deleted.append(path)
        except OSError as exc:
            logger.warning("⚠️  暫存檔清理失敗：%s (%s)", path, exc)

    if deleted:
        logger.info("🧹 已清理 %s 個過期暫存檔", len(deleted))
    return deleted


def _active_audio_paths_from_jobs() -> set[Path]:
    active: set[Path] = set()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT payload_json
                 FROM jobs
                WHERE status IN ('pending', 'processing')
                  AND cancel_requested=0
                  AND payload_json IS NOT NULL"""
        ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        audio_path = payload.get("audio_path")
        if audio_path:
            active.add(Path(audio_path))

    return active


def cleanup_stale_temp_files_for_jobs(
    temp_dir: Path,
    max_age_seconds: int = 24 * 60 * 60,
) -> list[Path]:
    """Clean temp files while preserving audio files needed by active queue jobs."""
    return cleanup_stale_temp_files(
        temp_dir=temp_dir,
        active_paths=_active_audio_paths_from_jobs(),
        max_age_seconds=max_age_seconds,
    )


def cleanup_terminal_jobs(max_age_days: int = 30, now: datetime | None = None) -> int:
    """Delete terminal queue records older than the retention window."""
    if max_age_days <= 0:
        return 0

    cutoff = (now or datetime.now()) - timedelta(days=max_age_days)
    deleted = delete_terminal_jobs_completed_before(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    if deleted:
        logger.info("🧹 已清理 %s 筆過期任務狀態記錄", deleted)
    return deleted
