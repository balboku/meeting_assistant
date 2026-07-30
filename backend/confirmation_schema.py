"""SQLite schema owned by the structured-minutes confirmation subsystem."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any


_MISSING_VALUES = {"", "未提及", "待確認", "不確定", "unknown", "n/a", "none"}


def confirmation_tasks_sql(
    table: str = "meeting_confirmation_tasks",
    *,
    if_not_exists: bool = False,
) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"unsafe table name: {table}")
    guard = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {guard}{table} (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id        INTEGER NOT NULL,
            meeting_item_id   INTEGER NOT NULL,
            field_name        TEXT NOT NULL,
            source_value      TEXT,
            status            TEXT NOT NULL DEFAULT 'pending'
                              CHECK(status IN ('pending','resolved','waived')),
            resolution_value  TEXT,
            resolution_note   TEXT,
            resolved_by       TEXT,
            resolved_at       TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(meeting_item_id, field_name),
            FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
            FOREIGN KEY(meeting_item_id) REFERENCES meeting_items(id) ON DELETE CASCADE
        )
    """


def ensure_confirmation_tasks_table(conn: sqlite3.Connection) -> None:
    conn.execute(confirmation_tasks_sql(if_not_exists=True))
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_confirmation_tasks_status
             ON meeting_confirmation_tasks(status, meeting_id, id)"""
    )


def required_confirmation_fields(
    item_type: str,
    payload: dict[str, Any],
    evidence: list[Any],
) -> list[str]:
    """Derive confirmation work for both imported and newly generated minutes."""
    required = [
        str(value).strip()
        for value in payload.get("confirmation_required") or []
        if str(value).strip()
    ]
    if item_type == "action":
        for field_name in ("owner", "due"):
            value = str(payload.get(field_name) or "").strip().lower()
            if value in _MISSING_VALUES and field_name not in required:
                required.append(field_name)
        evidence_field = "source_timecodes"
    else:
        evidence_field = "evidence_timecodes"
    if not evidence and evidence_field not in required:
        required.append(evidence_field)
    return required


def enqueue_confirmation_tasks(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    meeting_item_id: int,
    payload: dict[str, Any],
    field_names: list[str],
    now: str,
) -> int:
    """Persist idempotent pending tasks for one structured meeting item."""
    created = 0
    for field_name in field_names:
        value = payload.get(field_name)
        source_value = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (list, dict))
            else str(value or "")
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO meeting_confirmation_tasks (
                   meeting_id, meeting_item_id, field_name,
                   source_value, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (
                int(meeting_id),
                int(meeting_item_id),
                str(field_name),
                source_value,
                now,
                now,
            ),
        )
        created += max(0, int(cursor.rowcount))
    return created
