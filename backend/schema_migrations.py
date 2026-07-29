"""Transactional SQLite schema migrations with pre-migration backups."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


JOBS_COLUMNS = (
    "job_id", "status", "message", "output_path", "error_detail",
    "created_at", "completed_at", "task_type", "source", "payload_json",
    "attempts", "max_attempts", "queued_at", "started_at", "updated_at",
    "cancel_requested", "progress_current", "progress_total", "worker_id",
    "worker_generation", "heartbeat_at", "lease_expires_at",
)
MEETINGS_COLUMNS = (
    "id", "title", "date", "source_audio", "output_path", "summary",
    "created_at", "job_id", "quality_score", "quality_label",
    "quality_report_json", "structured_summary_json", "review_status",
    "reviewed_at", "reviewed_by", "review_note",
    "approved_content_sha256",
)
JOB_EVENTS_COLUMNS = (
    "id", "job_id", "event_type", "message", "detail", "created_at",
)
MEETING_REVISIONS_COLUMNS = (
    "id", "meeting_id", "source", "content", "created_at",
)
MEETING_ITEMS_COLUMNS = (
    "id", "meeting_id", "item_type", "item_key", "position",
    "payload_json", "evidence_json", "review_status", "reviewed_by",
    "reviewed_at", "review_note", "created_at", "updated_at",
)
MEETING_EVIDENCE_COLUMNS = (
    "id", "meeting_id", "original_filename", "stored_path", "sha256",
    "note", "analysis_markdown", "status", "revision_id", "created_at",
)
MEETING_EVIDENCE_ITEMS_COLUMNS = (
    "id", "evidence_id", "meeting_item_id", "relation_type", "created_at",
)
APP_USERS_COLUMNS = (
    "id", "email", "display_name", "role", "is_active", "created_at",
    "updated_at",
)
AUDIT_LOGS_COLUMNS = (
    "id", "actor_user_id", "actor_email", "action", "resource_type",
    "resource_id", "request_method", "request_path", "client_host",
    "detail_json", "created_at",
)


def _jobs_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            job_id             TEXT PRIMARY KEY,
            status             TEXT NOT NULL DEFAULT 'pending'
                               CHECK(status IN ('pending','processing','done','failed','cancelled')),
            message            TEXT,
            output_path        TEXT,
            error_detail       TEXT,
            created_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            completed_at       TEXT,
            task_type          TEXT NOT NULL DEFAULT 'audio_processing',
            source             TEXT NOT NULL DEFAULT 'upload',
            payload_json       TEXT,
            attempts           INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts       INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
            queued_at          TEXT,
            started_at         TEXT,
            updated_at         TEXT,
            cancel_requested   INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
            progress_current   INTEGER CHECK(progress_current IS NULL OR progress_current >= 0),
            progress_total     INTEGER CHECK(progress_total IS NULL OR progress_total >= 0),
            worker_id          TEXT,
            worker_generation  INTEGER,
            heartbeat_at       TEXT,
            lease_expires_at   TEXT
        )
    """


def _meetings_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            title                   TEXT NOT NULL,
            date                    TEXT NOT NULL,
            source_audio            TEXT NOT NULL,
            output_path             TEXT NOT NULL,
            summary                 TEXT,
            created_at              TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            job_id                  TEXT,
            quality_score           INTEGER,
            quality_label           TEXT,
            quality_report_json     TEXT,
            structured_summary_json TEXT,
            review_status           TEXT NOT NULL DEFAULT 'generated'
                                    CHECK(review_status IN ('generated','needs_review','reviewed','approved')),
            reviewed_at             TEXT,
            reviewed_by             TEXT,
            review_note             TEXT,
            approved_content_sha256 TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
    """


def _job_events_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            message     TEXT,
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        )
    """


def _meeting_revisions_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id  INTEGER NOT NULL,
            source      TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """


def _meeting_items_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id     INTEGER NOT NULL,
            item_type      TEXT NOT NULL
                           CHECK(item_type IN ('discussion','decision','action')),
            item_key       TEXT NOT NULL,
            position       INTEGER NOT NULL CHECK(position >= 1),
            payload_json   TEXT NOT NULL,
            evidence_json  TEXT,
            review_status  TEXT NOT NULL DEFAULT 'generated'
                           CHECK(review_status IN ('generated','needs_review','reviewed','approved')),
            reviewed_by    TEXT,
            reviewed_at    TEXT,
            review_note    TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(meeting_id, item_type, item_key),
            FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """


def _meeting_evidence_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id         INTEGER NOT NULL,
            original_filename  TEXT NOT NULL,
            stored_path        TEXT NOT NULL,
            sha256             TEXT NOT NULL,
            note               TEXT,
            analysis_markdown  TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'analyzed'
                               CHECK(status IN ('pending','analyzed','failed')),
            revision_id        INTEGER,
            created_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
            FOREIGN KEY(revision_id) REFERENCES meeting_revisions(id) ON DELETE SET NULL
        )
    """


def _meeting_evidence_items_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id      INTEGER NOT NULL,
            meeting_item_id  INTEGER NOT NULL,
            relation_type    TEXT NOT NULL DEFAULT 'supports'
                             CHECK(relation_type IN ('supports','contradicts','context')),
            created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(evidence_id, meeting_item_id, relation_type),
            FOREIGN KEY(evidence_id) REFERENCES meeting_evidence(id) ON DELETE CASCADE,
            FOREIGN KEY(meeting_item_id) REFERENCES meeting_items(id) ON DELETE CASCADE
        )
    """


def _app_users_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT NOT NULL UNIQUE,
            display_name  TEXT,
            role          TEXT NOT NULL DEFAULT 'viewer'
                          CHECK(role IN ('admin','editor','viewer')),
            is_active     INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """


def _audit_logs_sql(table: str) -> str:
    return f"""
        CREATE TABLE {table} (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id   INTEGER,
            actor_email     TEXT,
            action          TEXT NOT NULL,
            resource_type   TEXT,
            resource_id     TEXT,
            request_method  TEXT,
            request_path    TEXT,
            client_host     TEXT,
            detail_json     TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(actor_user_id) REFERENCES app_users(id) ON DELETE SET NULL
        )
    """


CONSTRAINED_TABLES = (
    ("jobs", JOBS_COLUMNS, _jobs_sql),
    ("meetings", MEETINGS_COLUMNS, _meetings_sql),
    ("job_events", JOB_EVENTS_COLUMNS, _job_events_sql),
    ("meeting_revisions", MEETING_REVISIONS_COLUMNS, _meeting_revisions_sql),
    ("meeting_items", MEETING_ITEMS_COLUMNS, _meeting_items_sql),
    ("meeting_evidence", MEETING_EVIDENCE_COLUMNS, _meeting_evidence_sql),
    (
        "meeting_evidence_items",
        MEETING_EVIDENCE_ITEMS_COLUMNS,
        _meeting_evidence_items_sql,
    ),
    ("app_users", APP_USERS_COLUMNS, _app_users_sql),
    ("audit_logs", AUDIT_LOGS_COLUMNS, _audit_logs_sql),
)


def _backup_root(db_path: Path) -> Path:
    configured = str(os.getenv("MEETING_BACKUP_DIR") or "").strip()
    root = Path(configured) if configured else db_path.parent / "backups"
    if not root.is_absolute():
        root = db_path.parent / root
    return root / "schema_migrations"


def _create_pre_migration_backup(
    conn: sqlite3.Connection,
    db_path: Path,
    from_version: int,
    to_version: int,
) -> Optional[Path]:
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        return None
    backup_dir = _backup_root(db_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / (
        f"schema_pre_v{from_version}_to_v{to_version}_{timestamp}.db"
    )
    destination = sqlite3.connect(str(backup_path))
    try:
        conn.backup(destination)
        check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
        if check.lower() != "ok":
            raise sqlite3.DatabaseError(
                f"schema migration 備份 quick_check 失敗：{check}"
            )
    finally:
        destination.close()
    return backup_path


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    columns: Iterable[str],
    ddl_factory,
) -> None:
    if not _table_exists(conn, table):
        return
    replacement = f"{table}__schema_v5"
    conn.execute(f"DROP TABLE IF EXISTS {replacement}")
    conn.execute(ddl_factory(replacement))
    column_sql = ", ".join(f'"{column}"' for column in columns)
    conn.execute(
        f"""INSERT INTO {replacement} ({column_sql})
            SELECT {column_sql} FROM {table}"""
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO {table}")


def _archive_orphan_job_events(conn: sqlite3.Connection) -> int:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_event_archive (
            archive_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id     INTEGER NOT NULL,
            job_id          TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            message         TEXT,
            detail          TEXT,
            original_created_at TEXT NOT NULL,
            archived_reason TEXT NOT NULL,
            archived_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(original_id, job_id)
        )
    """)
    cursor = conn.execute(
        """INSERT OR IGNORE INTO job_event_archive (
               original_id, job_id, event_type, message, detail,
               original_created_at, archived_reason
           )
           SELECT e.id, e.job_id, e.event_type, e.message, e.detail,
                  e.created_at, 'missing_parent_job_before_schema_v5'
             FROM job_events AS e
             LEFT JOIN jobs AS j ON j.job_id=e.job_id
            WHERE j.job_id IS NULL"""
    )
    conn.execute(
        """DELETE FROM job_events
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs WHERE jobs.job_id=job_events.job_id
            )"""
    )
    return int(cursor.rowcount)


def _assert_no_other_orphans(conn: sqlite3.Connection) -> None:
    checks = (
        (
            "meeting job",
            """SELECT COUNT(*) FROM meetings AS m
                LEFT JOIN jobs AS j ON j.job_id=m.job_id
               WHERE m.job_id IS NOT NULL AND j.job_id IS NULL""",
        ),
        (
            "meeting revision",
            """SELECT COUNT(*) FROM meeting_revisions AS r
                LEFT JOIN meetings AS m ON m.id=r.meeting_id
               WHERE m.id IS NULL""",
        ),
        (
            "meeting item",
            """SELECT COUNT(*) FROM meeting_items AS i
                LEFT JOIN meetings AS m ON m.id=i.meeting_id
               WHERE m.id IS NULL""",
        ),
        (
            "meeting evidence",
            """SELECT COUNT(*) FROM meeting_evidence AS e
                LEFT JOIN meetings AS m ON m.id=e.meeting_id
               WHERE m.id IS NULL""",
        ),
        (
            "evidence item",
            """SELECT COUNT(*) FROM meeting_evidence_items AS x
                LEFT JOIN meeting_evidence AS e ON e.id=x.evidence_id
                LEFT JOIN meeting_items AS i ON i.id=x.meeting_item_id
               WHERE e.id IS NULL OR i.id IS NULL""",
        ),
        (
            "audit actor",
            """SELECT COUNT(*) FROM audit_logs AS a
                LEFT JOIN app_users AS u ON u.id=a.actor_user_id
               WHERE a.actor_user_id IS NOT NULL AND u.id IS NULL""",
        ),
    )
    failures: list[str] = []
    for label, sql in checks:
        count = int(conn.execute(sql).fetchone()[0])
        if count > 0:
            failures.append(f"{label}={count}")
    if failures:
        raise sqlite3.IntegrityError(
            "schema migration 發現未處理孤兒資料：" + ", ".join(failures)
        )


def _migrate_to_v5(conn: sqlite3.Connection) -> dict[str, int]:
    archived_events = _archive_orphan_job_events(conn)
    _assert_no_other_orphans(conn)
    for table, columns, ddl_factory in CONSTRAINED_TABLES:
        _rebuild_table(conn, table, columns, ddl_factory)
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"schema v5 foreign_key_check 失敗：{violations[:5]}"
        )
    return {"archived_orphan_job_events": archived_events}


def apply_schema_migrations(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    target_version: int,
    database_existed: bool,
) -> dict[str, object]:
    """Apply all pending migrations atomically and return audit evidence."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version       INTEGER PRIMARY KEY,
            applied_at    TEXT NOT NULL,
            detail        TEXT
        )
    """)
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='schema_version'"
    ).fetchone()
    current_version = int(row[0]) if row else 1
    if current_version > target_version:
        raise sqlite3.DatabaseError(
            f"資料庫 schema={current_version} 高於程式支援版本 {target_version}"
        )
    if current_version == target_version:
        return {
            "from_version": current_version,
            "to_version": target_version,
            "backup_path": None,
            "details": {},
        }

    conn.commit()
    backup_path = (
        _create_pre_migration_backup(
            conn,
            Path(db_path),
            current_version,
            target_version,
        )
        if database_existed
        else None
    )
    conn.execute("BEGIN IMMEDIATE")
    details: dict[str, object] = {}
    for version in range(current_version + 1, target_version + 1):
        detail: dict[str, object] = {}
        if version == 5:
            detail = _migrate_to_v5(conn)
        conn.execute(
            """INSERT OR REPLACE INTO schema_migrations (
                   version, applied_at, detail
               ) VALUES (?, ?, ?)""",
            (
                version,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(detail) if detail else None,
            ),
        )
        details[str(version)] = detail
    conn.execute(
        """UPDATE app_meta
              SET value=?, updated_at=?
            WHERE key='schema_version'""",
        (
            str(target_version),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return {
        "from_version": current_version,
        "to_version": target_version,
        "backup_path": str(backup_path) if backup_path else None,
        "details": details,
    }
