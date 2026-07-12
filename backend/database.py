"""
=============================================================================
backend/database.py — SQLite 資料庫初始化與 CRUD 操作
=============================================================================
使用 Python 標準庫 sqlite3，不需要 ORM 框架，保持輕量。
資料庫檔案 meetings.db 建立在專案根目錄。
=============================================================================
"""

import json
import sqlite3
import logging
import os
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from contextlib import contextmanager

logger = logging.getLogger("MeetingAssistant.DB")

DEFAULT_JOB_MAX_ATTEMPTS = int(os.getenv("JOB_QUEUE_MAX_ATTEMPTS", "5"))
TRANSIENT_RETRY_MARKERS = (
    "503",
    "429",
    "unavailable",
    "serviceunavailable",
    "overloaded",
    "temporarily",
    "timeout",
    "deadline exceeded",
    "resource exhausted",
    "rate limit",
)

# 資料庫檔案位置（預設放在專案根目錄，可用 DB_PATH 覆寫）
DB_PATH = Path(os.getenv("DB_PATH") or Path(__file__).parent.parent / "meetings.db")


def _now() -> str:
    """Return a local timestamp in the same format SQLite uses in this project."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _transient_retry_delay_seconds() -> int:
    try:
        return max(0, int(os.getenv("JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS", "30")))
    except ValueError:
        return 30


def _is_transient_error(error_detail: str) -> bool:
    normalized = (error_detail or "").lower().replace("_", "")
    return any(marker in normalized for marker in TRANSIENT_RETRY_MARKERS)


def _retry_queued_at(error_detail: str) -> str:
    if not _is_transient_error(error_detail):
        return _now()

    delay_seconds = _transient_retry_delay_seconds()
    if delay_seconds <= 0:
        return _now()
    return (datetime.now() + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _serialize_payload(payload: Optional[dict[str, Any]]) -> Optional[str]:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _deserialize_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    payload_json = job.get("payload_json")
    if payload_json:
        try:
            job["payload"] = json.loads(payload_json)
        except json.JSONDecodeError:
            logger.warning("⚠️  任務 payload_json 格式錯誤：%s", job.get("job_id"))
            job["payload"] = {}
    else:
        job["payload"] = {}
    return job


def _has_recoverable_payload(row: sqlite3.Row) -> bool:
    payload_json = row["payload_json"]
    if not payload_json:
        return False

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return False

    task_type = row["task_type"]
    if task_type == "audio_processing":
        return bool(payload.get("audio_path") and payload.get("output_dir"))
    if task_type == "line_audio_processing":
        return bool(payload.get("message_id") and payload.get("user_id"))
    return False


def _load_job_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload_json = row["payload_json"]
    if not payload_json:
        return {}
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError:
        return {}


def _validate_manual_retry_payload(row: sqlite3.Row) -> None:
    """Validate a terminal job has enough still-existing state for manual retry."""
    if not _has_recoverable_payload(row):
        raise ValueError("任務缺少可恢復的 payload，無法重新排入佇列")

    if row["task_type"] != "audio_processing":
        return

    payload = _load_job_payload(row)
    audio_path = Path(payload.get("audio_path", ""))
    output_dir = Path(payload.get("output_dir", ""))
    if not audio_path.is_file():
        raise ValueError("找不到可重試的原始音檔，請重新上傳。")
    if not output_dir.is_dir():
        raise ValueError("找不到可寫入的輸出資料夾，請重新上傳。")


def _record_job_event(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
    message: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    _ensure_job_events_table(conn)
    conn.execute(
        """INSERT INTO job_events (job_id, event_type, message, detail, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (job_id, event_type, message, detail, _now()),
    )


def _escape_fts_token(token: str) -> str:
    return token.replace('"', '""')


def _build_fts_query(query: str) -> str:
    tokens = [token for token in query.strip().split() if token]
    return " ".join(f'"{_escape_fts_token(token)}"' for token in tokens)


def _build_like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _read_meeting_markdown(output_path: str) -> str:
    """Read the Markdown record used by the optional full-content index."""
    if not output_path:
        return ""
    output_file = Path(str(output_path))
    if not output_file.is_file():
        return ""
    try:
        return output_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.warning("⚠️  無法讀取會議全文索引來源 %s：%s", output_file, exc)
        return ""


def _create_meeting_fts_tables(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Create metadata and full-content FTS tables when SQLite FTS5 is available."""
    metadata_available = False
    content_available = False
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS meeting_fts
               USING fts5(title, source_audio, summary, output_path)"""
        )
        metadata_available = True
    except sqlite3.OperationalError as exc:
        logger.warning("⚠️  SQLite metadata FTS5 index unavailable: %s", exc)

    try:
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS meeting_content_fts
               USING fts5(content)"""
        )
        content_available = True
    except sqlite3.OperationalError as exc:
        logger.warning("⚠️  SQLite full-content FTS5 index unavailable: %s", exc)

    return metadata_available, content_available


def _meeting_ids(conn: sqlite3.Connection) -> set[int]:
    return {int(row[0]) for row in conn.execute("SELECT id FROM meetings").fetchall()}


def _fts_ids(conn: sqlite3.Connection, table: str) -> set[int]:
    # table names are internal constants, never user input.
    return {int(row[0]) for row in conn.execute(f"SELECT rowid FROM {table}").fetchall()}


def _rebuild_meeting_fts(conn: sqlite3.Connection, metadata: bool, content: bool) -> None:
    """One-time/backfill rebuild used only during startup or explicit repair."""
    rows = conn.execute(
        """SELECT id, title, source_audio, COALESCE(summary, '') AS summary, output_path
             FROM meetings
            ORDER BY id"""
    ).fetchall()

    if metadata:
        conn.execute("DELETE FROM meeting_fts")
        conn.executemany(
            """INSERT INTO meeting_fts(rowid, title, source_audio, summary, output_path)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (row["id"], row["title"], row["source_audio"], row["summary"], row["output_path"])
                for row in rows
            ],
        )

    if content:
        conn.execute("DELETE FROM meeting_content_fts")
        conn.executemany(
            """INSERT INTO meeting_content_fts(rowid, content)
               VALUES (?, ?)""",
            [(row["id"], _read_meeting_markdown(row["output_path"])) for row in rows],
        )


def _ensure_meeting_fts(conn: sqlite3.Connection) -> bool:
    """Create and backfill FTS5 indexes once; searches remain read-only."""
    metadata, content = _create_meeting_fts_tables(conn)
    if not metadata and not content:
        return False

    expected_ids = _meeting_ids(conn)
    if metadata and _fts_ids(conn, "meeting_fts") != expected_ids:
        _rebuild_meeting_fts(conn, metadata=True, content=False)
    if content and _fts_ids(conn, "meeting_content_fts") != expected_ids:
        _rebuild_meeting_fts(conn, metadata=False, content=True)
    return metadata


def _upsert_meeting_fts_row(
    conn: sqlite3.Connection,
    meeting_id: int,
    title: str,
    source_audio: str,
    summary: Optional[str],
    output_path: str,
    content: Optional[str] = None,
) -> None:
    """Incrementally update one meeting in both FTS indexes."""
    metadata, content_available = _create_meeting_fts_tables(conn)
    if metadata:
        conn.execute("DELETE FROM meeting_fts WHERE rowid=?", (meeting_id,))
        conn.execute(
            """INSERT INTO meeting_fts(rowid, title, source_audio, summary, output_path)
               VALUES (?, ?, ?, ?, ?)""",
            (meeting_id, title, source_audio, summary or "", str(output_path)),
        )
    if content_available:
        conn.execute("DELETE FROM meeting_content_fts WHERE rowid=?", (meeting_id,))
        conn.execute(
            """INSERT INTO meeting_content_fts(rowid, content)
               VALUES (?, ?)""",
            (meeting_id, content if content is not None else _read_meeting_markdown(output_path)),
        )


def _remove_meeting_fts_row(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Remove one meeting from available FTS indexes."""
    for table in ("meeting_fts", "meeting_content_fts"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE rowid=?", (meeting_id,))
        except sqlite3.OperationalError:
            pass


def _ensure_job_events_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT    NOT NULL,
            event_type  TEXT    NOT NULL,
            message     TEXT,
            detail      TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_events_job_id_created ON job_events(job_id, id)"
    )


def _ensure_meeting_revisions_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meeting_revisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id  INTEGER NOT NULL,
            source      TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meeting_revisions_meeting_id ON meeting_revisions(meeting_id, id DESC)"
    )


def _ensure_auth_tables(conn: sqlite3.Connection) -> None:
    """Create future account/role/audit tables without enabling enforcement."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT    NOT NULL UNIQUE,
            display_name  TEXT,
            role          TEXT    NOT NULL DEFAULT 'viewer',
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_users_role_active ON app_users(role, is_active)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id   INTEGER,
            actor_email     TEXT,
            action          TEXT    NOT NULL,
            resource_type   TEXT,
            resource_id     TEXT,
            request_method  TEXT,
            request_path    TEXT,
            client_host     TEXT,
            detail_json     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs(resource_type, resource_id)"
    )


def _ensure_meeting_quality_columns(conn: sqlite3.Connection) -> None:
    """Add local quality-report fields to existing databases in place."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)").fetchall()}
    columns = {
        "job_id": "TEXT",
        "quality_score": "INTEGER",
        "quality_label": "TEXT",
        "quality_report_json": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE meetings ADD COLUMN {name} {definition}")


# =============================================================================
# 資料庫連線管理
# =============================================================================

@contextmanager
def get_db():
    """
    Context manager：安全地取得 SQLite 連線，使用完畢後自動關閉。

    Yields:
        sqlite3.Connection: 資料庫連線（已設定 row_factory 為 dict）
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row  # 讓查詢結果可用欄位名稱存取
    conn.execute("PRAGMA journal_mode=WAL")  # 提升並發性能
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# 資料庫初始化
# =============================================================================

def _ensure_jobs_queue_columns(conn: sqlite3.Connection) -> None:
    """Add queue-related columns to older meetings.db files in-place."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    columns = {
        "task_type": "TEXT NOT NULL DEFAULT 'audio_processing'",
        "source": "TEXT NOT NULL DEFAULT 'upload'",
        "payload_json": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": f"INTEGER NOT NULL DEFAULT {DEFAULT_JOB_MAX_ATTEMPTS}",
        "queued_at": "TEXT",
        "started_at": "TEXT",
        "updated_at": "TEXT",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "progress_current": "INTEGER",
        "progress_total": "INTEGER",
    }

    for column_name, definition in columns.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {definition}")

    conn.execute("""
        UPDATE jobs
           SET task_type = COALESCE(task_type, 'audio_processing'),
               source = COALESCE(source, 'upload'),
               attempts = COALESCE(attempts, 0),
               max_attempts = COALESCE(max_attempts, ?),
               queued_at = COALESCE(queued_at, created_at),
               updated_at = COALESCE(updated_at, created_at),
               cancel_requested = COALESCE(cancel_requested, 0)
    """, (DEFAULT_JOB_MAX_ATTEMPTS,))
    conn.execute(
        """UPDATE jobs
              SET max_attempts=?
            WHERE max_attempts < ?
              AND status IN ('pending', 'processing', 'failed')""",
        (DEFAULT_JOB_MAX_ATTEMPTS, DEFAULT_JOB_MAX_ATTEMPTS),
    )

def init_db() -> None:
    """
    初始化資料庫：若資料表不存在則建立。
    應於 FastAPI 啟動時呼叫一次。
    """
    logger.info(f"🗄️  初始化 SQLite 資料庫：{DB_PATH}")

    with get_db() as conn:
        # 會議記錄主表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT    NOT NULL,
                date          TEXT    NOT NULL,
                source_audio  TEXT    NOT NULL,
                output_path   TEXT    NOT NULL,
                summary       TEXT,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        _ensure_meeting_quality_columns(conn)

        # 任務狀態追蹤表
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id        TEXT    PRIMARY KEY,
                status        TEXT    NOT NULL DEFAULT 'pending',
                message       TEXT,
                output_path   TEXT,
                error_detail  TEXT,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                completed_at  TEXT,
                task_type     TEXT    NOT NULL DEFAULT 'audio_processing',
                source        TEXT    NOT NULL DEFAULT 'upload',
                payload_json  TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                max_attempts  INTEGER NOT NULL DEFAULT {DEFAULT_JOB_MAX_ATTEMPTS},
                queued_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                started_at    TEXT,
                updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                progress_current INTEGER,
                progress_total   INTEGER
            )
        """)
        _ensure_jobs_queue_columns(conn)

        # 任務事件時間線：供維運頁面與 API 追蹤狀態變化。
        _ensure_job_events_table(conn)
        _ensure_meeting_revisions_table(conn)
        _ensure_auth_tables(conn)
        _ensure_meeting_fts(conn)

    logger.info("✅ 資料庫初始化完成")


# =============================================================================
# Jobs CRUD / Queue（任務狀態與持久化佇列）
# =============================================================================

def create_job(
    job_id: str,
    task_type: str = "audio_processing",
    source: str = "upload",
    payload: Optional[dict[str, Any]] = None,
    max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    message: Optional[str] = None,
) -> None:
    """建立新的任務記錄（初始狀態：pending）"""
    now = _now()
    with get_db() as conn:
        _ensure_jobs_queue_columns(conn)
        conn.execute(
            """INSERT INTO jobs (
                   job_id, status, message, task_type, source, payload_json,
                   max_attempts, queued_at, updated_at
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                "pending",
                message or "音檔已接收，等待處理...",
                task_type,
                source,
                _serialize_payload(payload),
                max_attempts,
                now,
                now,
            )
        )
        _record_job_event(conn, job_id, "created", message or "音檔已接收，等待處理...")
    logger.debug(f"📝 任務已建立：{job_id}")


def update_job_status(
    job_id: str,
    status: str,
    message: str,
    output_path: Optional[str] = None,
    error_detail: Optional[str] = None,
    progress_current: Optional[int] = None,
    progress_total: Optional[int] = None,
) -> None:
    """更新任務狀態"""
    completed_at = _now() if status in ("done", "failed", "cancelled") else None

    with get_db() as conn:
        _ensure_jobs_queue_columns(conn)
        conn.execute(
            """UPDATE jobs
               SET status=?,
                   message=?,
                   output_path=COALESCE(?, output_path),
                   error_detail=?,
                   completed_at=?,
                   updated_at=?,
                   progress_current=?,
                   progress_total=?
               WHERE job_id=?""",
            (
                status,
                message,
                output_path,
                error_detail,
                completed_at,
                _now(),
                progress_current,
                progress_total,
                job_id,
            )
        )
        _record_job_event(conn, job_id, f"status_{status}", message, error_detail)
    logger.debug(f"🔄 任務狀態更新：{job_id} → {status}")


def get_job(job_id: str) -> Optional[dict]:
    """查詢特定任務的狀態"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return _deserialize_job(row) if row else None


def find_line_job_by_message_id(message_id: str) -> Optional[dict]:
    """Return the existing LINE processing job for a LINE message ID, if any."""
    with get_db() as conn:
        _ensure_jobs_queue_columns(conn)
        rows = conn.execute(
            """SELECT *
                 FROM jobs
                WHERE task_type='line_audio_processing'
                ORDER BY created_at DESC, job_id DESC"""
        ).fetchall()

    for row in rows:
        payload = _load_job_payload(row)
        if payload.get("message_id") == message_id:
            return _deserialize_job(row)
    return None


def list_line_jobs_for_user(user_id: str, limit: int = 3) -> list[dict]:
    """Return recent LINE media jobs for one LINE user."""
    matches: list[dict] = []
    with get_db() as conn:
        _ensure_jobs_queue_columns(conn)
        rows = conn.execute(
            """SELECT *
                 FROM jobs
                WHERE task_type='line_audio_processing'
                ORDER BY updated_at DESC, created_at DESC, job_id DESC"""
        ).fetchall()

    for row in rows:
        payload = _load_job_payload(row)
        if payload.get("user_id") != user_id:
            continue
        matches.append(_deserialize_job(row))
        if len(matches) >= limit:
            break

    return matches


def list_jobs(limit: int = 20, offset: int = 0, status: Optional[str] = None) -> list[dict]:
    """List recent jobs, optionally filtered by status."""
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE status=?"
        params.append(status)

    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT *
                  FROM jobs
                  {where}
                 ORDER BY created_at DESC, updated_at DESC
                 LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [_deserialize_job(row) for row in rows]


def count_jobs(status: Optional[str] = None) -> int:
    """Count jobs, optionally filtered by status."""
    if status:
        with get_db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=?", (status,)
            ).fetchone()[0]

    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def count_jobs_by_status() -> dict[str, int]:
    """Return job counts grouped by status, including zeroes for known states."""
    counts = {
        "pending": 0,
        "processing": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
    }
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()

    for row in rows:
        counts[row["status"]] = int(row["count"])
    return counts


def average_completed_job_seconds() -> Optional[float]:
    """Return average completed job duration in seconds when timing data exists."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT AVG(
                       (julianday(completed_at) -
                        julianday(COALESCE(started_at, queued_at, created_at))) * 86400.0
                   ) AS average_seconds
                 FROM jobs
                WHERE status IN ('done', 'failed', 'cancelled')
                  AND completed_at IS NOT NULL"""
        ).fetchone()
    value = row["average_seconds"] if row else None
    return round(float(value), 2) if value is not None else None


def list_recent_failed_jobs(limit: int = 5) -> list[dict]:
    """Return recent failed jobs for operational dashboards."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT job_id, status, message, error_detail, updated_at, completed_at
                 FROM jobs
                WHERE status='failed'
                ORDER BY completed_at DESC, updated_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def delete_terminal_jobs_completed_before(cutoff: str) -> int:
    """Delete done/failed/cancelled jobs completed before a local timestamp string."""
    with get_db() as conn:
        conn.execute(
            """DELETE FROM job_events
                WHERE job_id IN (
                    SELECT job_id
                      FROM jobs
                     WHERE status IN ('done', 'failed', 'cancelled')
                       AND completed_at IS NOT NULL
                       AND completed_at < ?
                )""",
            (cutoff,),
        )
        cursor = conn.execute(
            """DELETE FROM jobs
                WHERE status IN ('done', 'failed', 'cancelled')
                  AND completed_at IS NOT NULL
                  AND completed_at < ?""",
            (cutoff,),
        )
        return cursor.rowcount


def claim_next_pending_job() -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending job for a local worker."""
    now = _now()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT *
                 FROM jobs
                WHERE status='pending'
                  AND cancel_requested=0
                  AND (queued_at IS NULL OR queued_at <= ?)
                ORDER BY queued_at ASC, created_at ASC
                LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None

        cursor = conn.execute(
            """UPDATE jobs
                  SET status='processing',
                      attempts=attempts + 1,
                      started_at=?,
                      updated_at=?,
                      message='⚙️ 任務已由本機 worker 取出，開始處理...'
                WHERE job_id=?
                  AND status='pending'
                  AND cancel_requested=0""",
            (now, now, row["job_id"])
        )
        if cursor.rowcount == 0:
            return None

        _record_job_event(conn, row["job_id"], "claimed", "任務已由 worker 取出，開始處理...")
        claimed = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
        ).fetchone()
        return _deserialize_job(claimed)


def retry_or_fail_job(job_id: str, error_detail: str) -> str:
    """
    Mark the current attempt as failed.

    If attempts remain, the job is returned to pending; otherwise it becomes
    terminally failed. Returns the resulting status.
    """
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到任務：{job_id}")

        if row["cancel_requested"]:
            conn.execute(
                """UPDATE jobs
                      SET status='cancelled',
                          message='任務已取消。',
                          error_detail=NULL,
                          completed_at=?,
                          updated_at=?
                    WHERE job_id=?""",
                (now, now, job_id),
            )
            _record_job_event(conn, job_id, "status_cancelled", "任務已取消。")
            return "cancelled"

        attempts = int(row["attempts"] or 0)
        max_attempts = int(row["max_attempts"] or 1)
        if attempts < max_attempts:
            retry_at = _retry_queued_at(error_detail)
            retry_message = f"處理失敗，已重新排入佇列（第 {attempts}/{max_attempts} 次嘗試）。"
            if retry_at > now:
                retry_message = (
                    f"處理失敗，服務暫時忙碌，已安排於 {retry_at} 後重試"
                    f"（第 {attempts}/{max_attempts} 次嘗試）。"
                )
            conn.execute(
                """UPDATE jobs
                      SET status='pending',
                          message=?,
                          error_detail=?,
                          queued_at=?,
                          started_at=NULL,
                          completed_at=NULL,
                          updated_at=?
                    WHERE job_id=?""",
                (
                    retry_message,
                    error_detail,
                    retry_at,
                    now,
                    job_id,
                ),
            )
            _record_job_event(conn, job_id, "retry_scheduled", retry_message, error_detail)
            return "pending"

        conn.execute(
            """UPDATE jobs
                  SET status='failed',
                      message='❌ 處理失敗，已達重試上限。',
                      error_detail=?,
                      completed_at=?,
                      updated_at=?
                WHERE job_id=?""",
            (error_detail, now, now, job_id),
        )
        _record_job_event(conn, job_id, "status_failed", "❌ 處理失敗，已達重試上限。", error_detail)
        return "failed"


def requeue_interrupted_jobs() -> int:
    """
    Move processing jobs left behind by a previous process back to the queue.

    Returns the number of jobs requeued to pending.
    """
    now = _now()
    requeued = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status='processing'"
        ).fetchall()
        for row in rows:
            job_id = row["job_id"]
            attempts = int(row["attempts"] or 0)
            max_attempts = int(row["max_attempts"] or 1)

            if row["cancel_requested"]:
                conn.execute(
                    """UPDATE jobs
                          SET status='cancelled',
                              message='任務已取消。',
                              completed_at=?,
                              updated_at=?
                        WHERE job_id=?""",
                    (now, now, job_id),
                )
                _record_job_event(conn, job_id, "status_cancelled", "任務已取消。")
                continue

            if not _has_recoverable_payload(row):
                conn.execute(
                    """UPDATE jobs
                          SET status='failed',
                              message='❌ 前次處理中斷，且缺少可恢復的任務資料。',
                              error_detail='缺少可恢復的任務 payload，無法自動重試舊式背景任務。',
                              completed_at=?,
                              updated_at=?
                        WHERE job_id=?""",
                    (now, now, job_id),
                )
                _record_job_event(
                    conn,
                    job_id,
                    "status_failed",
                    "❌ 前次處理中斷，且缺少可恢復的任務資料。",
                    "缺少可恢復的任務 payload，無法自動重試舊式背景任務。",
                )
                continue

            if attempts < max_attempts:
                conn.execute(
                    """UPDATE jobs
                          SET status='pending',
                              message='偵測到前次處理中斷，已重新排入佇列。',
                              queued_at=?,
                              started_at=NULL,
                              completed_at=NULL,
                              updated_at=?
                        WHERE job_id=?""",
                    (now, now, job_id),
                )
                _record_job_event(conn, job_id, "interrupted_requeued", "偵測到前次處理中斷，已重新排入佇列。")
                requeued += 1
                continue

            conn.execute(
                """UPDATE jobs
                      SET status='failed',
                          message='❌ 前次處理中斷且已達重試上限。',
                          error_detail=COALESCE(error_detail, '任務處理中斷'),
                          completed_at=?,
                          updated_at=?
                    WHERE job_id=?""",
                (now, now, job_id),
            )
            _record_job_event(conn, job_id, "status_failed", "❌ 前次處理中斷且已達重試上限。")
    return requeued


def request_job_cancel(job_id: str) -> bool:
    """Request cancellation. Pending jobs become terminal immediately."""
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return False

        status = row["status"]
        if status in {"done", "failed", "cancelled"}:
            return False

        if status == "pending":
            conn.execute(
                """UPDATE jobs
                      SET status='cancelled',
                          cancel_requested=1,
                          message='任務已取消。',
                          completed_at=?,
                          updated_at=?
                    WHERE job_id=?""",
                (now, now, job_id),
            )
            _record_job_event(conn, job_id, "status_cancelled", "任務已取消。")
            return True

        conn.execute(
            """UPDATE jobs
                  SET cancel_requested=1,
                      message='已收到取消要求，會在目前處理步驟結束後停止。',
                      updated_at=?
                WHERE job_id=?""",
            (now, job_id),
        )
        _record_job_event(conn, job_id, "cancel_requested", "已收到取消要求，會在目前處理步驟結束後停止。")
        return True


def requeue_failed_job(job_id: str) -> Optional[dict]:
    """Move a failed/cancelled job with recoverable payload back to pending."""
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None

        if row["status"] not in {"failed", "cancelled"}:
            raise ValueError("只有 failed/cancelled 任務可以重新排入佇列")
        _validate_manual_retry_payload(row)

        conn.execute(
            """UPDATE jobs
                  SET status='pending',
                      message='已重新排入佇列，等待處理...',
                      error_detail=NULL,
                      attempts=0,
                      cancel_requested=0,
                      queued_at=?,
                      started_at=NULL,
                      completed_at=NULL,
                      updated_at=?,
                      progress_current=NULL,
                      progress_total=NULL
                WHERE job_id=?""",
            (now, now, job_id),
        )
        _record_job_event(conn, job_id, "requeued", "已重新排入佇列，等待處理...")
        updated = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return _deserialize_job(updated)


def delete_job(job_id: str) -> Optional[bool]:
    """Delete a terminal job. Return None when missing, False when active."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] not in {"done", "failed", "cancelled"}:
            return False
        conn.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        return True


def list_job_events(job_id: str) -> list[dict[str, Any]]:
    """Return a chronological event timeline for one job."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, job_id, event_type, message, detail, created_at
                 FROM job_events
                WHERE job_id=?
                ORDER BY id ASC""",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def is_job_cancel_requested(job_id: str) -> bool:
    """Return whether a job has an outstanding cancellation request."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT cancel_requested, status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return False
        return bool(row["cancel_requested"]) or row["status"] == "cancelled"


# =============================================================================
# Meetings CRUD（會議記錄管理）
# =============================================================================

def save_meeting(
    title: str,
    date: str,
    source_audio: str,
    output_path: str,
    summary: Optional[str] = None,
    job_id: Optional[str] = None,
    quality_report: Optional[dict[str, Any]] = None,
) -> int:
    """
    將新會議記錄存入資料庫。

    Returns:
        int: 新插入記錄的 ID
    """
    with get_db() as conn:
        _ensure_meeting_quality_columns(conn)
        quality_score = quality_report.get("score") if quality_report else None
        quality_label = quality_report.get("label") if quality_report else None
        quality_report_json = json.dumps(quality_report, ensure_ascii=False) if quality_report else None
        cursor = conn.execute(
            """INSERT INTO meetings (
                   title, date, source_audio, output_path, summary,
                   job_id, quality_score, quality_label, quality_report_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                date,
                source_audio,
                str(output_path),
                summary,
                job_id,
                quality_score,
                quality_label,
                quality_report_json,
            )
        )
        meeting_id = cursor.lastrowid
        try:
            _upsert_meeting_fts_row(
                conn,
                int(meeting_id),
                title,
                source_audio,
                summary,
                str(output_path),
            )
        except sqlite3.OperationalError:
            logger.warning("⚠️  會議已寫入，但 FTS 索引更新失敗（ID: %s）", meeting_id)

    logger.info(f"💾 會議記錄已寫入 SQLite（ID: {meeting_id}）")
    return meeting_id


def _meeting_row_with_quality_preview(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    quality_report_json = record.pop("quality_report_json", None)
    warning_count = 0
    try:
        quality_report = json.loads(quality_report_json) if quality_report_json else None
    except json.JSONDecodeError:
        quality_report = None
    if isinstance(quality_report, dict):
        warnings = quality_report.get("warnings") or []
        if isinstance(warnings, list):
            warning_count = len(warnings)
    record["quality_warning_count"] = warning_count
    return record


def list_meetings(limit: int = 50, offset: int = 0) -> list[dict]:
    """列出所有會議記錄（依建立時間倒序）"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, title, date, source_audio, output_path,
                      substr(summary, 1, 200) as summary_preview,
                      job_id, quality_score, quality_label,
                      quality_report_json,
                      created_at
               FROM meetings
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        return [_meeting_row_with_quality_preview(r) for r in rows]


def count_meetings() -> int:
    """統計會議記錄總數"""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]


def get_meeting(meeting_id: int) -> Optional[dict]:
    """查詢特定會議的完整資訊"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id=?", (meeting_id,)
        ).fetchone()
        if not row:
            return None

        record = dict(row)
        quality_report_json = record.pop("quality_report_json", None)
        try:
            record["quality_report"] = json.loads(quality_report_json) if quality_report_json else None
        except json.JSONDecodeError:
            record["quality_report"] = None
        # 讀取完整的 Markdown 內容
        output_file = Path(record["output_path"])
        if output_file.exists():
            record["full_content"] = output_file.read_text(encoding="utf-8")
        else:
            record["full_content"] = "（找不到對應的 Markdown 檔案）"

        return record


def update_meeting_content_with_revision(
    meeting_id: int,
    full_content: str,
    summary: str,
    source: str = "manual_edit",
) -> int:
    """Replace meeting Markdown while preserving the previous full content."""
    record = get_meeting(meeting_id)
    if not record:
        raise ValueError(f"找不到會議記錄：ID={meeting_id}")

    output_file = Path(record["output_path"])
    if not output_file.is_file():
        raise FileNotFoundError(f"找不到會議 Markdown：{output_file}")

    temp_file = output_file.with_suffix(output_file.suffix + ".editing.tmp")
    temp_file.write_text(full_content, encoding="utf-8")
    try:
        with get_db() as conn:
            _ensure_meeting_revisions_table(conn)
            cursor = conn.execute(
                """INSERT INTO meeting_revisions (meeting_id, source, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (meeting_id, source, record["full_content"], _now()),
            )
            revision_id = int(cursor.lastrowid)
            conn.execute("UPDATE meetings SET summary=? WHERE id=?", (summary, meeting_id))
            try:
                _upsert_meeting_fts_row(
                    conn,
                    int(meeting_id),
                    record["title"],
                    record["source_audio"],
                    summary,
                    record["output_path"],
                    content=full_content,
                )
            except sqlite3.OperationalError:
                logger.warning("⚠️ 會議內容已更新，但 FTS 索引更新失敗（ID: %s）", meeting_id)
            temp_file.replace(output_file)
    finally:
        if temp_file.exists():
            temp_file.unlink()

    logger.info("✏️  會議記錄已人工修訂並保留版本（ID: %s，revision: %s）", meeting_id, revision_id)
    return revision_id


def list_meeting_revisions(meeting_id: int) -> list[dict[str, Any]]:
    with get_db() as conn:
        _ensure_meeting_revisions_table(conn)
        rows = conn.execute(
            """SELECT id, meeting_id, source, content, created_at
                 FROM meeting_revisions
                WHERE meeting_id=?
                ORDER BY id DESC""",
            (meeting_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_app_user(
    email: str,
    display_name: Optional[str] = None,
    role: str = "viewer",
    is_active: bool = True,
) -> dict[str, Any]:
    """Create or update a future RBAC user record."""
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        raise ValueError("email is required")

    with get_db() as conn:
        _ensure_auth_tables(conn)
        now = _now()
        conn.execute(
            """INSERT INTO app_users (email, display_name, role, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                   display_name=excluded.display_name,
                   role=excluded.role,
                   is_active=excluded.is_active,
                   updated_at=excluded.updated_at""",
            (normalized_email, display_name, role, 1 if is_active else 0, now, now),
        )
        row = conn.execute("SELECT * FROM app_users WHERE email=?", (normalized_email,)).fetchone()
        return dict(row)


def get_app_user_by_email(email: str) -> Optional[dict[str, Any]]:
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None
    with get_db() as conn:
        _ensure_auth_tables(conn)
        row = conn.execute("SELECT * FROM app_users WHERE email=?", (normalized_email,)).fetchone()
        return dict(row) if row else None


def list_app_users(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with get_db() as conn:
        _ensure_auth_tables(conn)
        rows = conn.execute(
            """SELECT id, email, display_name, role, is_active, created_at, updated_at
                 FROM app_users
                ORDER BY email ASC
                LIMIT ? OFFSET ?""",
            (min(max(int(limit), 1), 500), max(int(offset), 0)),
        ).fetchall()
        return [dict(row) for row in rows]


def record_audit_log(
    *,
    action: str,
    actor_email: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    request_method: Optional[str] = None,
    request_path: Optional[str] = None,
    client_host: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> int:
    """Record a future audit event. Callers decide when the feature is enabled."""
    if not str(action or "").strip():
        raise ValueError("action is required")
    with get_db() as conn:
        _ensure_auth_tables(conn)
        cursor = conn.execute(
            """INSERT INTO audit_logs (
                   actor_user_id, actor_email, action, resource_type, resource_id,
                   request_method, request_path, client_host, detail_json, created_at
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                actor_email,
                str(action).strip(),
                resource_type,
                resource_id,
                request_method,
                request_path,
                client_host,
                json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                _now(),
            ),
        )
        return int(cursor.lastrowid)


def list_audit_logs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with get_db() as conn:
        _ensure_auth_tables(conn)
        rows = conn.execute(
            """SELECT id, actor_user_id, actor_email, action, resource_type, resource_id,
                      request_method, request_path, client_host, detail_json, created_at
                 FROM audit_logs
                ORDER BY id DESC
                LIMIT ? OFFSET ?""",
            (min(max(int(limit), 1), 500), max(int(offset), 0)),
        ).fetchall()

    records = []
    for row in rows:
        record = dict(row)
        detail_json = record.pop("detail_json", None)
        try:
            record["detail"] = json.loads(detail_json) if detail_json else None
        except json.JSONDecodeError:
            record["detail"] = None
        records.append(record)
    return records


def search_meetings(query: str, limit: int = 50) -> list[dict]:
    """
    搜尋會議記錄的標題、音檔名稱、摘要與完整 Markdown 內容。

    FTS5 負責快速的完整詞組搜尋，LIKE 後備則補足中文連續字串的部分匹配。
    此函式只讀取索引，不會在每次搜尋時重建索引。

    Args:
        query: 搜尋關鍵字
        limit: 最多回傳筆數

    Returns:
        符合條件的會議記錄列表
    """
    query = unicodedata.normalize("NFKC", str(query or "")).strip()
    if not query:
        return []

    limit = min(max(int(limit), 1), 100)
    fts_query = _build_fts_query(query)
    pattern = _build_like_pattern(query)
    records_by_id: dict[int, dict] = {}

    def add_rows(rows: list[sqlite3.Row]) -> None:
        for row in rows:
            records_by_id.setdefault(int(row["id"]), _meeting_row_with_quality_preview(row))

    with get_db() as conn:
        try:
            rows = conn.execute(
                """SELECT m.id, m.title, m.date, m.source_audio, m.output_path,
                          substr(m.summary, 1, 200) as summary_preview,
                          m.job_id, m.quality_score, m.quality_label,
                          m.quality_report_json,
                          m.created_at
                     FROM meeting_fts
                     JOIN meetings AS m ON m.id = meeting_fts.rowid
                    WHERE meeting_fts MATCH ?
                    ORDER BY bm25(meeting_fts), m.created_at DESC
                    LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            add_rows(rows)
        except sqlite3.OperationalError as exc:
            logger.debug("metadata FTS search unavailable: %s", exc)

        try:
            rows = conn.execute(
                """SELECT m.id, m.title, m.date, m.source_audio, m.output_path,
                          substr(m.summary, 1, 200) as summary_preview,
                          m.job_id, m.quality_score, m.quality_label,
                          m.quality_report_json,
                          m.created_at
                     FROM meeting_content_fts
                     JOIN meetings AS m ON m.id = meeting_content_fts.rowid
                    WHERE meeting_content_fts MATCH ?
                    ORDER BY bm25(meeting_content_fts), m.created_at DESC
                    LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            add_rows(rows)
        except sqlite3.OperationalError as exc:
            logger.debug("full-content FTS search unavailable: %s", exc)

        try:
            rows = conn.execute(
                """SELECT m.id, m.title, m.date, m.source_audio, m.output_path,
                          substr(m.summary, 1, 200) as summary_preview,
                          m.job_id, m.quality_score, m.quality_label,
                          m.quality_report_json,
                          m.created_at
                     FROM meetings AS m
                     LEFT JOIN meeting_content_fts AS c ON c.rowid = m.id
                    WHERE m.title LIKE ? ESCAPE '\\'
                       OR COALESCE(m.summary, '') LIKE ? ESCAPE '\\'
                       OR m.source_audio LIKE ? ESCAPE '\\'
                       OR m.output_path LIKE ? ESCAPE '\\'
                       OR COALESCE(c.content, '') LIKE ? ESCAPE '\\'
                    ORDER BY m.created_at DESC
                    LIMIT ?""",
                (pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            add_rows(rows)
        except sqlite3.OperationalError as exc:
            logger.debug("full-content LIKE search unavailable, using metadata only: %s", exc)
            rows = conn.execute(
                """SELECT id, title, date, source_audio, output_path,
                          substr(summary, 1, 200) as summary_preview,
                          job_id, quality_score, quality_label,
                          quality_report_json,
                          created_at
                   FROM meetings
                   WHERE title LIKE ?
                      OR COALESCE(summary, '') LIKE ? ESCAPE '\\'
                      OR source_audio LIKE ? ESCAPE '\\'
                      OR output_path LIKE ? ESCAPE '\\'
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            add_rows(rows)

    return list(records_by_id.values())[:limit]


def delete_meeting(meeting_id: int) -> bool:
    """
    刪除特定會議記錄，並嘗試移除關聯的 Markdown 檔案。

    Returns:
        bool: 刪除是否成功（True 成功，False 找不到記錄）
    """
    record = get_meeting(meeting_id)
    if not record:
        return False

    # 刪除實體 Markdown 檔案
    output_file = Path(record["output_path"])
    if output_file.exists():
        try:
            output_file.unlink()
            logger.info(f"🗑️  已刪除會議記錄檔案：{output_file.name}")
        except Exception as e:
            logger.warning(f"⚠️  刪除會議記錄檔案失敗：{e}")

    # 刪除資料庫記錄
    with get_db() as conn:
        _ensure_meeting_revisions_table(conn)
        conn.execute("DELETE FROM meeting_revisions WHERE meeting_id=?", (meeting_id,))
        _remove_meeting_fts_row(conn, meeting_id)
        conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        logger.info(f"🗑️  已從資料庫移除會議記錄（ID: {meeting_id}）")

    return True
