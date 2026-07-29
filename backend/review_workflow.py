"""Human-review domain service for meeting documents and D/R/A items.

This module owns review transitions and roll-up rules.  Database storage stays
in ``backend.database``; HTTP concerns stay in ``backend.main``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from backend.database import (
    MEETING_REVIEW_STATUSES,
    _ensure_meeting_quality_columns,
    _ensure_meeting_workflow_tables,
    _meeting_file_sha256,
    _now,
    _quality_report_blockers,
    get_db,
    get_meeting,
)


def update_meeting_review_status(
    meeting_id: int,
    status: str,
    *,
    reviewed_by: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Move one meeting through the explicit human-review workflow."""
    normalized = str(status or "").strip().lower()
    if normalized not in MEETING_REVIEW_STATUSES:
        raise ValueError(f"不支援的會議審查狀態：{status}")

    with get_db() as conn:
        _ensure_meeting_quality_columns(conn)
        row = conn.execute(
            """SELECT id, output_path, quality_report_json, review_status
                 FROM meetings
                WHERE id=?""",
            (meeting_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"找不到會議記錄：ID={meeting_id}")

        try:
            quality_report = json.loads(row["quality_report_json"] or "{}")
        except json.JSONDecodeError:
            quality_report = {}
        blockers = _quality_report_blockers(quality_report)
        current_status = str(row["review_status"] or "generated")
        if normalized == "approved" and current_status != "reviewed":
            raise ValueError("會議必須先完成人工複核，才能核准。")
        if normalized == "approved" and blockers:
            labels = "、".join(str(index + 1) for index in blockers)
            raise ValueError(f"第 {labels} 段仍有阻擋交付的品質問題，不能核准。")
        if normalized == "approved":
            unresolved_items = conn.execute(
                """SELECT item_key
                     FROM meeting_items
                    WHERE meeting_id=?
                      AND review_status NOT IN ('reviewed', 'approved')
                    ORDER BY item_type, position, id""",
                (meeting_id,),
            ).fetchall()
            if unresolved_items:
                labels = "、".join(str(item["item_key"]) for item in unresolved_items)
                raise ValueError(f"{labels} 尚未完成逐項複核，不能核准整份會議。")

        approved_hash = None
        if normalized == "approved":
            output_file = Path(row["output_path"])
            if not output_file.is_file():
                raise FileNotFoundError(f"找不到會議 Markdown：{output_file}")
            approved_hash = _meeting_file_sha256(output_file)

        timestamp = _now() if normalized in {"reviewed", "approved"} else None
        actor = (
            (reviewed_by or "").strip() or None
            if normalized in {"reviewed", "approved"}
            else None
        )
        conn.execute(
            """UPDATE meetings
                  SET review_status=?,
                      reviewed_at=?,
                      reviewed_by=?,
                      review_note=?,
                      approved_content_sha256=?
                WHERE id=?""",
            (
                normalized,
                timestamp,
                actor,
                (note or "").strip() or None,
                approved_hash,
                meeting_id,
            ),
        )

    updated = get_meeting(meeting_id)
    if not updated:
        raise KeyError(f"找不到會議記錄：ID={meeting_id}")
    return updated


def update_meeting_item_review_status(
    meeting_id: int,
    item_key: str,
    status: str,
    *,
    reviewed_by: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Update one D/R/A item and roll its result up to meeting review state."""
    normalized_status = str(status or "").strip().lower()
    normalized_key = str(item_key or "").strip()
    if normalized_status not in MEETING_REVIEW_STATUSES:
        raise ValueError(f"不支援的逐項審查狀態：{status}")
    if not normalized_key:
        raise ValueError("缺少逐項識別碼。")

    with get_db() as conn:
        _ensure_meeting_workflow_tables(conn)
        meeting = conn.execute(
            "SELECT id, review_status FROM meetings WHERE id=?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            raise KeyError(f"找不到會議記錄：ID={meeting_id}")
        item = conn.execute(
            """SELECT id, review_status
                 FROM meeting_items
                WHERE meeting_id=? AND item_key=?""",
            (meeting_id, normalized_key),
        ).fetchone()
        if item is None:
            raise KeyError(f"找不到結構化項目：{normalized_key}")
        if normalized_status == "approved" and str(item["review_status"]) != "reviewed":
            raise ValueError("逐項內容必須先標記已複核，才能核准。")

        timestamp = _now() if normalized_status in {"reviewed", "approved"} else None
        actor = (
            (reviewed_by or "").strip() or None
            if normalized_status in {"reviewed", "approved"}
            else None
        )
        conn.execute(
            """UPDATE meeting_items
                  SET review_status=?,
                      reviewed_by=?,
                      reviewed_at=?,
                      review_note=?,
                      updated_at=?
                WHERE id=?""",
            (
                normalized_status,
                actor,
                timestamp,
                (note or "").strip() or None,
                _now(),
                item["id"],
            ),
        )

        statuses = [
            str(row["review_status"] or "generated")
            for row in conn.execute(
                "SELECT review_status FROM meeting_items WHERE meeting_id=?",
                (meeting_id,),
            ).fetchall()
        ]
        if any(value in {"generated", "needs_review"} for value in statuses):
            rolled_up_status = "needs_review"
        elif statuses:
            rolled_up_status = "reviewed"
        else:
            rolled_up_status = str(meeting["review_status"] or "generated")
        rolled_up_timestamp = _now() if rolled_up_status == "reviewed" else None
        rolled_up_actor = actor if rolled_up_status == "reviewed" else None
        conn.execute(
            """UPDATE meetings
                  SET review_status=?,
                      reviewed_at=?,
                      reviewed_by=?,
                      approved_content_sha256=NULL
                WHERE id=?""",
            (rolled_up_status, rolled_up_timestamp, rolled_up_actor, meeting_id),
        )
        updated = conn.execute(
            """SELECT id, item_type, item_key, position, payload_json,
                      evidence_json, review_status, reviewed_by, reviewed_at,
                      review_note, created_at, updated_at
                 FROM meeting_items
                WHERE id=?""",
            (item["id"],),
        ).fetchone()
    result = dict(updated)
    result["meeting_review_status"] = rolled_up_status
    try:
        result["payload"] = json.loads(result.pop("payload_json"))
    except (TypeError, json.JSONDecodeError):
        result["payload"] = {}
    try:
        result["evidence"] = json.loads(result.pop("evidence_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["evidence"] = []
    return result
