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
import threading
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping


_SNAPSHOT_VERIFICATION_CACHE: dict[
    tuple[str, int, int],
    dict[str, object],
] = {}
_SNAPSHOT_VERIFICATION_CACHE_LOCK = threading.Lock()
_RECORD_STATE_TABLES = (
    "app_meta",
    "jobs",
    "job_events",
    "orphan_job_events_archive",
    "meetings",
    "meeting_revisions",
    "meeting_items",
    "meeting_evidence",
    "meeting_evidence_items",
    "meeting_confirmation_tasks",
    "app_users",
    "audit_logs",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sidecar_paths(database_path: Path) -> tuple[Path, Path]:
    path = Path(database_path)
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )


def _remove_database_backup(database_path: Path) -> None:
    path = Path(database_path)
    path.unlink(missing_ok=True)
    for sidecar in _sqlite_sidecar_paths(path):
        sidecar.unlink(missing_ok=True)


def _cleanup_backup_sidecars(backup_dir: Path) -> None:
    """Remove obsolete journals from immutable, maintenance-owned DB copies."""
    root = Path(backup_dir)
    for pattern in ("meetings_*.db-wal", "meetings_*.db-shm"):
        for sidecar in root.glob(pattern):
            sidecar.unlink(missing_ok=True)


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
        _remove_database_backup(backup_path)
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
        _remove_database_backup(stale)
    _cleanup_backup_sidecars(backup_dir)

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
            "record_state_sha256": None,
        }

    # immutable=1 keeps verification read-only even on Windows and prevents
    # SQLite from creating persistent -wal/-shm files next to backup copies.
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
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
            record_state_sha256 = _record_state_fingerprint_from_connection(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "path": str(path),
            "detail": str(exc),
            "meetings": None,
            "jobs": None,
            "record_state_sha256": None,
        }
    return {
        "ok": integrity.lower() == "ok",
        "path": str(path),
        "detail": integrity,
        "meetings": meetings,
        "jobs": jobs,
        "record_state_sha256": record_state_sha256,
    }


def _record_state_fingerprint_from_connection(conn: sqlite3.Connection) -> str:
    """Hash durable record state while excluding runtime leases and FTS caches."""
    digest = hashlib.sha256()
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in _RECORD_STATE_TABLES:
        if table not in tables:
            continue
        columns = [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        digest.update(
            json.dumps(
                [table, columns],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=lambda value: {
                        "__bytes__": bytes(value).hex()
                    },
                ).encode("utf-8")
            )
    return digest.hexdigest()


def record_state_fingerprint(
    database_path: Path,
    *,
    immutable: bool = True,
) -> str:
    """Hash durable record state; live reads include committed WAL content."""
    path = Path(database_path)
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{path.resolve().as_uri()}?{query}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return _record_state_fingerprint_from_connection(conn)
    finally:
        conn.close()


def directory_inventory_fingerprint(root: Path | None) -> str | None:
    """Hash relative names, sizes, and mtimes without rereading large media."""
    if root is None:
        return None
    directory = Path(root)
    digest = hashlib.sha256()
    if not directory.is_dir():
        return digest.hexdigest()
    for path in sorted(
        (candidate for candidate in directory.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(directory).as_posix(),
    ):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(
            json.dumps(
                [
                    path.relative_to(directory).as_posix(),
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return digest.hexdigest()


def current_backup_state(
    db_path: Path,
    source_media_dir: Path | None = None,
    previous_minutes_dir: Path | None = None,
) -> dict[str, str | None]:
    """Return the durable DB and file-inventory state used by backup policy."""
    return {
        "record_state_sha256": record_state_fingerprint(
            db_path,
            immutable=False,
        ),
        "source_media_inventory_sha256": directory_inventory_fingerprint(
            source_media_dir
        ),
        "previous_minutes_inventory_sha256": directory_inventory_fingerprint(
            previous_minutes_dir
        ),
    }


def latest_backup_health(
    backup_dir: Path,
    *,
    now: datetime | None = None,
    max_age_hours: int = 48,
    current_state: Mapping[str, object] | None = None,
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
            "fresh": False,
            "container_valid": False,
            "state_current": False,
            "detail": "尚無資料庫備份",
        }
    latest = backups[0]
    reference = now or datetime.now()
    modified = datetime.fromtimestamp(latest.stat().st_mtime)
    age_hours = max(0.0, (reference - modified).total_seconds() / 3600.0)
    verification = verify_database_backup(latest)
    fresh = age_hours <= max(1, max_age_hours)
    state_current = bool(
        current_state is not None
        and verification.get("record_state_sha256")
        == current_state.get("record_state_sha256")
    )
    return {
        **verification,
        "ok": bool(verification["ok"]) and (fresh or state_current),
        "container_valid": bool(verification["ok"]),
        "fresh": fresh,
        "state_current": state_current,
        "age_hours": round(age_hours, 2),
        "detail": (
            str(verification["detail"])
            if fresh
            else (
                "資料未變更，沿用已驗證的資料庫備份"
                if state_current
                else f"備份已超過 {max_age_hours} 小時"
            )
        ),
    }


def create_record_snapshot(
    backup_path: Path,
    backup_dir: Path,
    *,
    source_media_dir: Path | None = None,
    previous_minutes_dir: Path | None = None,
    now: datetime | None = None,
    keep: int = 5,
) -> Path:
    """Bundle DB, Markdown, attachments, prior DOCX, and referenced media."""
    backup_path = Path(backup_path)
    verification = verify_database_backup(backup_path)
    if not verification["ok"]:
        raise sqlite3.DatabaseError(f"無法建立記錄快照：{verification['detail']}")

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    snapshot_path = Path(backup_dir) / f"meeting_records_{timestamp}.zip"
    entries: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    archived_source_media: dict[str, str] = {}
    archived_previous_minutes: dict[str, str] = {}

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

            source_audio_value = str(row["source_audio"] or "").strip()
            source_path: Path | None = None
            if source_audio_value:
                raw_source_path = Path(source_audio_value)
                candidates = [raw_source_path]
                if source_media_dir is not None:
                    candidates.append(Path(source_media_dir) / raw_source_path.name)
                candidates.append(output_path.parent / "source_audio" / raw_source_path.name)
                source_path = next(
                    (candidate for candidate in candidates if candidate.is_file()),
                    None,
                )
            if source_path is not None:
                digest = _sha256_file(source_path)
                arcname = archived_source_media.get(digest)
                if arcname is None:
                    suffix = source_path.suffix.lower()
                    arcname = f"source_media/{digest[:2]}/{digest}{suffix}"
                    archive.write(source_path, arcname)
                    archived_source_media[digest] = arcname
                    entries.append({
                        "category": "source_media",
                        "archive_path": arcname,
                        "source_path": str(source_path),
                        "sha256": digest,
                        "bytes": source_path.stat().st_size,
                    })
                meeting_entry.update({
                    "source_media_included": True,
                    "source_media_archive_path": arcname,
                    "source_media_sha256": digest,
                    "source_media_original_name": source_path.name,
                })
            elif source_audio_value:
                missing.append({
                    "category": "source_media",
                    "meeting_id": int(row["id"]),
                    "source_path": source_audio_value,
                })

            try:
                quality_report = json.loads(str(row["quality_report_json"] or "{}"))
            except json.JSONDecodeError:
                quality_report = {}
            previous_metadata = (
                quality_report.get("previous_minutes")
                if isinstance(quality_report, dict)
                else None
            )
            if isinstance(previous_metadata, dict):
                stored_value = str(previous_metadata.get("stored_path") or "").strip()
                original_name = str(previous_metadata.get("filename") or "").strip()
                recorded_digest = str(previous_metadata.get("sha256") or "").strip().lower()
                candidates = [Path(stored_value)] if stored_value else []
                if previous_minutes_dir is not None and stored_value:
                    candidates.append(Path(previous_minutes_dir) / Path(stored_value).name)
                previous_path = next(
                    (candidate for candidate in candidates if candidate.is_file()),
                    None,
                )
                if previous_path is not None:
                    digest = _sha256_file(previous_path)
                    if recorded_digest and digest != recorded_digest:
                        missing.append({
                            "category": "previous_minutes",
                            "meeting_id": int(row["id"]),
                            "source_path": str(previous_path),
                            "detail": "SHA-256 不符",
                        })
                    else:
                        arcname = archived_previous_minutes.get(digest)
                        if arcname is None:
                            arcname = f"previous_minutes/{digest[:2]}/{digest}.docx"
                            archive.write(previous_path, arcname)
                            archived_previous_minutes[digest] = arcname
                            entries.append({
                                "category": "previous_minutes",
                                "archive_path": arcname,
                                "source_path": str(previous_path),
                                "sha256": digest,
                                "bytes": previous_path.stat().st_size,
                            })
                        meeting_entry.update({
                            "previous_minutes_archive_path": arcname,
                            "previous_minutes_original_name": original_name,
                            "previous_minutes_sha256": digest,
                        })
                else:
                    missing.append({
                        "category": "previous_minutes",
                        "meeting_id": int(row["id"]),
                        "source_path": stored_value,
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
            "format_version": 2,
            "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "database_verification": verification,
            "record_state_sha256": record_state_fingerprint(backup_path),
            "source_media_inventory_sha256": directory_inventory_fingerprint(
                source_media_dir
            ),
            "previous_minutes_inventory_sha256": directory_inventory_fingerprint(
                previous_minutes_dir
            ),
            "source_media_policy": "已連結會議的原始媒體以 SHA-256 內容定址去重納入快照。",
            "previous_minutes_policy": "已連結會議的前次 DOCX 以記錄的 SHA-256 驗證後納入快照。",
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
            if int(manifest.get("format_version") or 0) not in {1, 2}:
                return {
                    "ok": False,
                    "path": str(path),
                    "detail": "不支援的快照格式版本",
                }
            archive_names = set(archive.namelist())
            if "database/meetings.db" not in archive_names:
                return {
                    "ok": False,
                    "path": str(path),
                    "detail": "快照缺少 database/meetings.db",
                }
            for entry in manifest.get("entries") or []:
                archive_path = str(entry.get("archive_path") or "")
                if not archive_path or archive_path not in archive_names:
                    return {
                        "ok": False,
                        "path": str(path),
                        "detail": f"manifest 指向不存在的檔案：{archive_path}",
                    }
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
    missing_count = len(manifest.get("missing") or [])
    container_valid = bool(database_check["ok"])
    recoverability_complete = container_valid and missing_count == 0
    result = {
        # ``ok`` remains the container-integrity signal so an otherwise valid
        # historical snapshot can still be inspected and restored.  Health
        # reporting separately treats missing source records as degraded.
        "ok": container_valid,
        "container_valid": container_valid,
        "recoverability_complete": recoverability_complete,
        "status": "ok" if recoverability_complete else "degraded",
        "path": str(path),
        "detail": (
            "ok"
            if recoverability_complete
            else (
                f"快照可讀，但有 {missing_count} 個來源檔案缺漏"
                if container_valid
                else database_check["detail"]
            )
        ),
        "entries": len(manifest.get("entries") or []),
        "missing": missing_count,
        "source_media": sum(
            1
            for entry in manifest.get("entries") or []
            if entry.get("category") == "source_media"
        ),
        "previous_minutes": sum(
            1
            for entry in manifest.get("entries") or []
            if entry.get("category") == "previous_minutes"
        ),
        "meetings": database_check.get("meetings"),
        "jobs": database_check.get("jobs"),
        "record_state_sha256": manifest.get("record_state_sha256"),
        "source_media_inventory_sha256": manifest.get(
            "source_media_inventory_sha256"
        ),
        "previous_minutes_inventory_sha256": manifest.get(
            "previous_minutes_inventory_sha256"
        ),
    }
    _remember_record_snapshot_verification(path, result)
    return result


def _remember_record_snapshot_verification(
    snapshot_path: Path,
    verification: dict[str, object],
) -> None:
    """Cache a verified immutable snapshot by resolved path, mtime, and size."""
    path = Path(snapshot_path)
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return
    cached_result = dict(verification)
    cached_result["path"] = str(path)
    with _SNAPSHOT_VERIFICATION_CACHE_LOCK:
        for stale_key in tuple(_SNAPSHOT_VERIFICATION_CACHE):
            if stale_key[0] == cache_key[0] and stale_key != cache_key:
                _SNAPSHOT_VERIFICATION_CACHE.pop(stale_key, None)
        _SNAPSHOT_VERIFICATION_CACHE[cache_key] = cached_result
        while len(_SNAPSHOT_VERIFICATION_CACHE) > 16:
            _SNAPSHOT_VERIFICATION_CACHE.pop(
                next(iter(_SNAPSHOT_VERIFICATION_CACHE))
            )


def _cached_record_snapshot_verification(snapshot_path: Path) -> dict[str, object]:
    path = Path(snapshot_path)
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return verify_record_snapshot(path)
    with _SNAPSHOT_VERIFICATION_CACHE_LOCK:
        cached = _SNAPSHOT_VERIFICATION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    return verify_record_snapshot(path)


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

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    runtime_root = target / "runtime"
    runtime_output = runtime_root / "output"
    runtime_source_media = runtime_output / "source_audio"
    runtime_previous_minutes = runtime_output / "previous_minutes"
    runtime_evidence = runtime_root / "evidence"
    runtime_output.mkdir(parents=True, exist_ok=True)
    runtime_source_media.mkdir(parents=True, exist_ok=True)
    runtime_previous_minutes.mkdir(parents=True, exist_ok=True)
    runtime_evidence.mkdir(parents=True, exist_ok=True)
    runtime_db = runtime_root / "meetings.db"
    shutil.copy2(target / "database" / "meetings.db", runtime_db)

    restored_files = 1
    conn = sqlite3.connect(str(runtime_db))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for meeting in manifest.get("meetings") or []:
            meeting_id = int(meeting["id"])
            job_id = str(meeting.get("job_id") or "").strip()
            markdown_arcname = str(
                meeting.get("markdown_archive_path") or ""
            ).strip()
            markdown_target: Path | None = None
            if markdown_arcname:
                markdown_source = target / markdown_arcname
                markdown_target = (
                    runtime_output
                    / f"{meeting_id}_{markdown_source.name}"
                )
                shutil.copy2(markdown_source, markdown_target)
                restored_files += 1
                conn.execute(
                    "UPDATE meetings SET output_path=? WHERE id=?",
                    (str(markdown_target), meeting_id),
                )

            source_arcname = str(
                meeting.get("source_media_archive_path") or ""
            ).strip()
            source_target: Path | None = None
            if source_arcname:
                source_file = target / source_arcname
                source_target = runtime_source_media / source_file.name
                if not source_target.exists():
                    shutil.copy2(source_file, source_target)
                    restored_files += 1
                conn.execute(
                    "UPDATE meetings SET source_audio=? WHERE id=?",
                    (str(source_target), meeting_id),
                )

            previous_arcname = str(
                meeting.get("previous_minutes_archive_path") or ""
            ).strip()
            previous_target: Path | None = None
            if previous_arcname:
                previous_file = target / previous_arcname
                previous_target = runtime_previous_minutes / previous_file.name
                if not previous_target.exists():
                    shutil.copy2(previous_file, previous_target)
                    restored_files += 1
                report_row = conn.execute(
                    "SELECT quality_report_json FROM meetings WHERE id=?",
                    (meeting_id,),
                ).fetchone()
                try:
                    quality_report = json.loads(report_row[0] or "{}") if report_row else {}
                except json.JSONDecodeError:
                    quality_report = {}
                if not isinstance(quality_report, dict):
                    quality_report = {}
                previous_metadata = quality_report.get("previous_minutes")
                if not isinstance(previous_metadata, dict):
                    previous_metadata = {}
                previous_metadata["stored_path"] = str(previous_target)
                quality_report["previous_minutes"] = previous_metadata
                conn.execute(
                    "UPDATE meetings SET quality_report_json=? WHERE id=?",
                    (json.dumps(quality_report, ensure_ascii=False), meeting_id),
                )

            if job_id:
                job_row = conn.execute(
                    "SELECT payload_json FROM jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if job_row:
                    try:
                        payload = json.loads(job_row[0] or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                    if source_target is not None:
                        payload["audio_path"] = str(source_target)
                    if previous_target is not None:
                        payload["previous_minutes_path"] = str(previous_target)
                        payload["previous_minutes_filename"] = meeting.get(
                            "previous_minutes_original_name"
                        )
                        payload["previous_minutes_sha256"] = meeting.get(
                            "previous_minutes_sha256"
                        )
                    payload["output_dir"] = str(runtime_output)
                    conn.execute(
                        """UPDATE jobs
                              SET output_path=COALESCE(?, output_path),
                                  payload_json=?
                            WHERE job_id=?""",
                        (
                            str(markdown_target) if markdown_target else None,
                            json.dumps(payload, ensure_ascii=False),
                            job_id,
                        ),
                    )

        for entry in manifest.get("entries") or []:
            if entry.get("category") != "attachment":
                continue
            evidence_id = int(entry["evidence_id"])
            archived = target / str(entry["archive_path"])
            evidence_dir = runtime_evidence / str(entry.get("meeting_id") or "unknown")
            evidence_dir.mkdir(parents=True, exist_ok=True)
            destination = evidence_dir / archived.name
            shutil.copy2(archived, destination)
            restored_files += 1
            conn.execute(
                "UPDATE meeting_evidence SET stored_path=? WHERE id=?",
                (str(destination), evidence_id),
            )

        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if integrity.lower() != "ok" or foreign_key_violations:
            raise sqlite3.IntegrityError(
                "還原後資料庫驗證失敗："
                f"integrity={integrity}; foreign_keys={foreign_key_violations[:5]}"
            )
        conn.commit()
    finally:
        conn.close()

    report = {
        "snapshot": str(Path(snapshot_path)),
        "runtime_database": str(runtime_db),
        "runtime_output": str(runtime_output),
        "restored_files": restored_files,
        "missing": manifest.get("missing") or [],
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    (runtime_root / "restore_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "restored": True,
        "target": str(target),
        "runtime": report,
        "verification": verification,
    }


def replicate_record_snapshot(
    snapshot_path: Path,
    offsite_dir: Path,
    *,
    keep: int = 5,
) -> Path:
    """Atomically replicate one verified snapshot to a distinct backup root."""
    source = Path(snapshot_path)
    verification = _cached_record_snapshot_verification(source)
    if not verification["ok"]:
        raise ValueError(f"本機快照驗證失敗：{verification['detail']}")
    destination_root = Path(offsite_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    if source.parent.resolve() == destination_root.resolve():
        raise ValueError("異地備份目錄不可與本機備份目錄相同")

    destination = destination_root / source.name
    checksum_path = destination.with_suffix(f"{destination.suffix}.sha256")
    temp_destination = destination_root / (
        f".{source.name}.{uuid.uuid4().hex}.tmp"
    )
    source_digest = _sha256_file(source)
    try:
        destination_matches = False
        if destination.is_file() and destination.stat().st_size == source.stat().st_size:
            try:
                checksum_parts = checksum_path.read_text(
                    encoding="ascii"
                ).split(maxsplit=1)
                recorded_digest = (
                    checksum_parts[0].strip().lower()
                    if checksum_parts
                    else ""
                )
            except (OSError, UnicodeError):
                recorded_digest = ""
            if recorded_digest == source_digest:
                destination_matches = _sha256_file(destination) == source_digest

        if not destination_matches:
            shutil.copy2(source, temp_destination)
            copied_digest = _sha256_file(temp_destination)
            if copied_digest != source_digest:
                raise OSError("異地備份 SHA-256 驗證失敗")
            temp_destination.replace(destination)

        # Byte identity with the already fully verified source proves the
        # replicated archive has the same manifest, members, and SQLite DB.
        _remember_record_snapshot_verification(destination, verification)
        checksum_path.write_text(
            f"{source_digest}  {destination.name}\n",
            encoding="ascii",
        )
    finally:
        temp_destination.unlink(missing_ok=True)

    snapshots = sorted(
        destination_root.glob("meeting_records_*.zip"),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in snapshots[max(keep, 1):]:
        stale.unlink()
        stale.with_suffix(f"{stale.suffix}.sha256").unlink(missing_ok=True)
    return destination


def latest_record_snapshot_health(
    snapshot_dir: Path | None,
    *,
    now: datetime | None = None,
    max_age_hours: int = 48,
    current_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Verify freshness and content of the newest record snapshot."""
    if snapshot_dir is None:
        return {
            "ok": False,
            "configured": False,
            "path": None,
            "age_hours": None,
            "fresh": False,
            "state_current": False,
            "detail": "未設定異地備份目錄",
        }
    root = Path(snapshot_dir)
    snapshots = sorted(
        root.glob("meeting_records_*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not snapshots:
        return {
            "ok": False,
            "configured": True,
            "path": None,
            "age_hours": None,
            "fresh": False,
            "state_current": False,
            "detail": "異地備份目錄尚無記錄快照",
        }
    latest = snapshots[0]
    reference = now or datetime.now()
    modified = datetime.fromtimestamp(latest.stat().st_mtime)
    age_hours = max(0.0, (reference - modified).total_seconds() / 3600.0)
    verification = _cached_record_snapshot_verification(latest)
    fresh = age_hours <= max(1, max_age_hours)
    complete = bool(verification.get("recoverability_complete"))
    state_current = bool(
        current_state is not None
        and verification.get("record_state_sha256")
        == current_state.get("record_state_sha256")
        and verification.get("source_media_inventory_sha256")
        == current_state.get("source_media_inventory_sha256")
        and verification.get("previous_minutes_inventory_sha256")
        == current_state.get("previous_minutes_inventory_sha256")
    )
    return {
        **verification,
        "configured": True,
        "ok": (
            bool(verification["ok"])
            and complete
            and (fresh or state_current)
        ),
        "fresh": fresh,
        "state_current": state_current,
        "age_hours": round(age_hours, 2),
        "detail": (
            str(verification["detail"])
            if fresh or not complete
            else (
                "資料未變更，沿用已驗證的完整記錄快照"
                if state_current
                else f"記錄快照已超過 {max_age_hours} 小時"
            )
        ),
    }


def runtime_backup_health(
    db_path: Path,
    backup_dir: Path,
    source_media_dir: Path | None,
    previous_minutes_dir: Path | None,
    offsite_backup_dir: Path | None,
    *,
    max_age_hours: int,
) -> tuple[dict[str, object], dict[str, object]]:
    """Check backup health and prove unchanged state only after age expiry."""
    database_health = latest_backup_health(
        backup_dir,
        max_age_hours=max_age_hours,
    )
    snapshot_health = latest_record_snapshot_health(
        offsite_backup_dir,
        max_age_hours=max_age_hours,
    )
    needs_state_check = any(
        health.get("path") and not health.get("fresh")
        for health in (database_health, snapshot_health)
    )
    if not needs_state_check:
        return database_health, snapshot_health
    state = current_backup_state(
        db_path,
        source_media_dir,
        previous_minutes_dir,
    )
    return (
        latest_backup_health(
            backup_dir,
            max_age_hours=max_age_hours,
            current_state=state,
        ),
        latest_record_snapshot_health(
            offsite_backup_dir,
            max_age_hours=max_age_hours,
            current_state=state,
        ),
    )


def maintain_database(
    db_path: Path,
    *,
    force_vacuum: bool = False,
    vacuum_min_free_pages: int = 5000,
    vacuum_free_ratio: float = 0.20,
) -> dict[str, object]:
    """Checkpoint WAL and vacuum only when reclaimable space justifies it."""
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        free_ratio = free_pages / max(page_count, 1)
        should_vacuum = bool(force_vacuum) or (
            free_pages >= max(1, int(vacuum_min_free_pages))
            and free_ratio >= max(0.0, float(vacuum_free_ratio))
        )
        if should_vacuum:
            conn.execute("VACUUM")
    finally:
        conn.close()

    return {
        "wal_checkpoint": True,
        "vacuum": should_vacuum,
        "page_count": page_count,
        "free_pages": free_pages,
        "free_ratio": round(free_ratio, 4),
    }


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
    source_media_dir: Path | None = None,
    previous_minutes_dir: Path | None = None,
    offsite_backup_dir: Path | None = None,
    backup_keep: int = 4,
    backup_min_interval_hours: int = 168,
    force_backup: bool = False,
) -> dict[str, object]:
    """Run weekly changed-only backup policy, replication, and DB maintenance."""
    interval_hours = max(1, int(backup_min_interval_hours))
    state = current_backup_state(
        db_path,
        source_media_dir,
        previous_minutes_dir,
    )
    existing_backup = latest_backup_health(
        backup_dir,
        max_age_hours=interval_hours,
        current_state=state,
    )
    existing_snapshot = latest_record_snapshot_health(
        backup_dir,
        max_age_hours=interval_hours,
        current_state=state,
    )
    backup_usable = bool(
        existing_backup.get("container_valid")
        and existing_backup.get("path")
    )
    snapshot_usable = bool(
        existing_snapshot.get("container_valid")
        and existing_snapshot.get("path")
    )
    data_changed = not bool(existing_snapshot.get("state_current"))
    snapshot_age = existing_snapshot.get("age_hours")
    interval_due = snapshot_age is None or float(snapshot_age) >= interval_hours
    backup_created = bool(
        force_backup
        or not backup_usable
        or not snapshot_usable
        or (data_changed and interval_due)
    )
    backup_path = (
        backup_database(db_path=db_path, backup_dir=backup_dir, keep=backup_keep)
        if backup_created
        else Path(str(existing_backup["path"]))
    )
    snapshot_reused = not bool(
        force_backup
        or not snapshot_usable
        or (data_changed and backup_created)
    )
    snapshot_path = (
        Path(str(existing_snapshot["path"]))
        if snapshot_reused
        else create_record_snapshot(
            backup_path=backup_path,
            backup_dir=backup_dir,
            source_media_dir=source_media_dir,
            previous_minutes_dir=previous_minutes_dir,
            keep=backup_keep,
        )
    )
    offsite_path = (
        replicate_record_snapshot(
            snapshot_path,
            offsite_backup_dir,
            keep=backup_keep,
        )
        if offsite_backup_dir is not None
        else None
    )
    maintenance = maintain_database(db_path=db_path)
    return {
        "backup_path": str(backup_path),
        "backup_created": backup_created,
        "snapshot_path": str(snapshot_path),
        "snapshot_reused": snapshot_reused,
        "data_changed": data_changed,
        "interval_due": interval_due,
        "force_backup": bool(force_backup),
        "snapshot_verification": _cached_record_snapshot_verification(snapshot_path),
        "offsite_snapshot_path": str(offsite_path) if offsite_path else None,
        "offsite_snapshot_verification": (
            _cached_record_snapshot_verification(offsite_path)
            if offsite_path
            else None
        ),
        "maintenance": maintenance,
    }
