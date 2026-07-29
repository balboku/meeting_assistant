"""
Operational maintenance helpers for the local SQLite-backed service.

These helpers keep the API layer small: startup can run database maintenance,
health checks can describe local prerequisites, and tests can exercise both
without starting Uvicorn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(
    db_path: Path,
    backup_dir: Path,
    now: datetime | None = None,
    keep: int = 5,
) -> Path:
    """Create a transactionally consistent SQLite backup and prune old copies.

    ``shutil.copy2`` can produce an incomplete backup while the source database
    is using WAL mode.  SQLite's online backup API copies the database from one
    consistent read snapshot and therefore includes committed WAL content.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"meetings_{timestamp}.db"
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
        integrity_row = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity_row or str(integrity_row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(
                f"備份完整性檢查失敗：{integrity_row[0] if integrity_row else 'no result'}"
            )
    except Exception:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()

    backups = sorted(
        backup_dir.glob("meetings_*.db"),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in backups[max(keep, 1):]:
        stale.unlink()

    return backup_path


def verify_database_backup(backup_path: Path) -> dict[str, object]:
    """Open a backup read-only and report SQLite integrity plus basic counts."""
    path = Path(backup_path)
    if not path.is_file():
        return {
            "ok": False,
            "path": str(path),
            "detail": "找不到備份檔案",
            "meetings": None,
            "jobs": None,
        }

    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0]) if integrity_row else "no result"
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            meetings = (
                int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0])
                if "meetings" in tables
                else 0
            )
            jobs = (
                int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                if "jobs" in tables
                else 0
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "path": str(path),
            "detail": str(exc),
            "meetings": None,
            "jobs": None,
        }
    return {
        "ok": integrity.lower() == "ok",
        "path": str(path),
        "detail": integrity,
        "meetings": meetings,
        "jobs": jobs,
    }


def latest_backup_health(
    backup_dir: Path,
    *,
    now: datetime | None = None,
    max_age_hours: int = 48,
) -> dict[str, object]:
    """Verify the newest database backup and report whether it is fresh."""
    backups = sorted(
        Path(backup_dir).glob("meetings_*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return {
            "ok": False,
            "path": None,
            "age_hours": None,
            "detail": "尚無資料庫備份",
        }
    latest = backups[0]
    reference = now or datetime.now()
    modified = datetime.fromtimestamp(latest.stat().st_mtime)
    age_hours = max(0.0, (reference - modified).total_seconds() / 3600.0)
    verification = verify_database_backup(latest)
    fresh = age_hours <= max(1, max_age_hours)
    return {
        **verification,
        "ok": bool(verification["ok"]) and fresh,
        "age_hours": round(age_hours, 2),
        "detail": (
            str(verification["detail"])
            if fresh
            else f"備份已超過 {max_age_hours} 小時"
        ),
    }


def create_record_snapshot(
    backup_path: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
    keep: int = 5,
) -> Path:
    """Bundle a consistent DB backup with referenced Markdown and attachments.

    Retained source audio/video is intentionally inventoried in the database
    quality metadata rather than duplicated into every startup snapshot.
    """
    backup_path = Path(backup_path)
    verification = verify_database_backup(backup_path)
    if not verification["ok"]:
        raise sqlite3.DatabaseError(f"無法建立記錄快照：{verification['detail']}")

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    snapshot_path = Path(backup_dir) / f"meeting_records_{timestamp}.zip"
    entries: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []

    conn = sqlite3.connect(str(backup_path))
    conn.row_factory = sqlite3.Row
    try:
        meetings = conn.execute(
            """SELECT id, title, job_id, source_audio, output_path,
                      quality_report_json
                 FROM meetings
                ORDER BY id"""
        ).fetchall()
        evidence = (
            conn.execute(
                """SELECT id, meeting_id, original_filename, stored_path, sha256
                     FROM meeting_evidence
                    ORDER BY meeting_id, id"""
            ).fetchall()
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_evidence'"
            ).fetchone()
            else []
        )
    finally:
        conn.close()

    with zipfile.ZipFile(
        snapshot_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        database_arcname = "database/meetings.db"
        archive.write(backup_path, database_arcname)
        entries.append({
            "category": "database",
            "archive_path": database_arcname,
            "sha256": _sha256_file(backup_path),
            "bytes": backup_path.stat().st_size,
        })

        meeting_manifest: list[dict[str, object]] = []
        for row in meetings:
            output_path = Path(str(row["output_path"] or ""))
            meeting_entry: dict[str, object] = {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "job_id": row["job_id"],
                "source_audio": str(row["source_audio"] or ""),
                "source_media_included": False,
            }
            if output_path.is_file():
                arcname = f"meetings/{row['id']}/{output_path.name}"
                archive.write(output_path, arcname)
                entries.append({
                    "category": "markdown",
                    "meeting_id": int(row["id"]),
                    "archive_path": arcname,
                    "source_path": str(output_path),
                    "sha256": _sha256_file(output_path),
                    "bytes": output_path.stat().st_size,
                })
                meeting_entry["markdown_archive_path"] = arcname
            else:
                missing.append({
                    "category": "markdown",
                    "meeting_id": int(row["id"]),
                    "source_path": str(output_path),
                })
            meeting_manifest.append(meeting_entry)

        for row in evidence:
            stored_path = Path(str(row["stored_path"] or ""))
            if stored_path.is_file():
                arcname = f"attachments/{row['meeting_id']}/{row['id']}_{stored_path.name}"
                archive.write(stored_path, arcname)
                entries.append({
                    "category": "attachment",
                    "meeting_id": int(row["meeting_id"]),
                    "evidence_id": int(row["id"]),
                    "archive_path": arcname,
                    "source_path": str(stored_path),
                    "sha256": _sha256_file(stored_path),
                    "recorded_sha256": str(row["sha256"] or ""),
                    "bytes": stored_path.stat().st_size,
                })
            else:
                missing.append({
                    "category": "attachment",
                    "meeting_id": int(row["meeting_id"]),
                    "evidence_id": int(row["id"]),
                    "source_path": str(stored_path),
                })

        manifest = {
            "format_version": 1,
            "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "database_verification": verification,
            "source_media_policy": (
                "原始媒體不重複納入啟動快照；依 output/source_audio 保留政策另行管理。"
            ),
            "meetings": meeting_manifest,
            "entries": entries,
            "missing": missing,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    snapshots = sorted(
        Path(backup_dir).glob("meeting_records_*.zip"),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in snapshots[max(keep, 1):]:
        stale.unlink()
    return snapshot_path


def verify_record_snapshot(snapshot_path: Path) -> dict[str, object]:
    """Verify the manifest, file digests, and embedded SQLite database."""
    path = Path(snapshot_path)
    if not path.is_file():
        return {"ok": False, "path": str(path), "detail": "找不到記錄快照"}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            bad_member = archive.testzip()
            if bad_member:
                return {
                    "ok": False,
                    "path": str(path),
                    "detail": f"ZIP CRC 失敗：{bad_member}",
                }
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            for entry in manifest.get("entries") or []:
                archive_path = str(entry.get("archive_path") or "")
                expected = str(entry.get("sha256") or "")
                actual = hashlib.sha256(archive.read(archive_path)).hexdigest()
                if not expected or actual != expected:
                    return {
                        "ok": False,
                        "path": str(path),
                        "detail": f"SHA-256 不一致：{archive_path}",
                    }
            with tempfile.TemporaryDirectory() as temp_dir:
                embedded_db = Path(temp_dir) / "meetings.db"
                embedded_db.write_bytes(archive.read("database/meetings.db"))
                database_check = verify_database_backup(embedded_db)
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        return {"ok": False, "path": str(path), "detail": str(exc)}
    return {
        "ok": bool(database_check["ok"]),
        "path": str(path),
        "detail": "ok" if database_check["ok"] else database_check["detail"],
        "entries": len(manifest.get("entries") or []),
        "missing": len(manifest.get("missing") or []),
        "meetings": database_check.get("meetings"),
        "jobs": database_check.get("jobs"),
    }


def restore_record_snapshot(snapshot_path: Path, target_dir: Path) -> dict[str, object]:
    """Restore a verified snapshot to a new empty directory.

    This function deliberately refuses to overwrite an existing non-empty
    target, making restore drills safe to automate without touching production.
    """
    verification = verify_record_snapshot(snapshot_path)
    if not verification["ok"]:
        raise ValueError(f"快照驗證失敗：{verification['detail']}")
    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"還原目標不是空目錄：{target}")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(snapshot_path, "r") as archive:
        target_root = target.resolve()
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if target_root not in destination.parents and destination != target_root:
                raise ValueError(f"快照包含不安全路徑：{member.filename}")
        archive.extractall(target)
    return {
        "restored": True,
        "target": str(target),
        "verification": verification,
    }


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
        if db_path.is_file():
            try:
                conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
                try:
                    integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                finally:
                    conn.close()
            except (OSError, sqlite3.Error) as exc:
                integrity = str(exc)
            checks.append({
                "name": "database_integrity",
                "status": "ok" if integrity.lower() == "ok" else "failed",
                "detail": integrity,
            })

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    checks.append({
        "name": "media_tools",
        "status": "ok" if ffmpeg_path and ffprobe_path else "failed",
        "detail": (
            f"ffmpeg={ffmpeg_path}; ffprobe={ffprobe_path}"
            if ffmpeg_path and ffprobe_path
            else "缺少 ffmpeg 或 ffprobe"
        ),
    })

    return checks


def run_startup_maintenance(
    db_path: Path,
    backup_dir: Path,
    backup_keep: int = 5,
) -> dict[str, object]:
    """Back up and maintain the SQLite DB after init_db has ensured it exists."""
    backup_path = backup_database(db_path=db_path, backup_dir=backup_dir, keep=backup_keep)
    snapshot_path = create_record_snapshot(
        backup_path=backup_path,
        backup_dir=backup_dir,
        keep=backup_keep,
    )
    maintenance = maintain_database(db_path=db_path)
    return {
        "backup_path": str(backup_path),
        "snapshot_path": str(snapshot_path),
        "snapshot_verification": verify_record_snapshot(snapshot_path),
        "maintenance": maintenance,
    }
