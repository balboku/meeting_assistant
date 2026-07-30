"""Persistent confirmation queue for incomplete structured meeting fields."""

from __future__ import annotations

import json
from typing import Any, Iterable

from backend import database


_STRUCTURED_FIELDS = {
    "discussion": "discussion_summary",
    "decision": "final_decisions",
    "action": "action_items",
}
_EVIDENCE_FIELDS = {"evidence_timecodes", "source_timecodes"}


def apply_structured_minutes_backfill(
    entries: Iterable[tuple[int, dict[str, Any]]],
    *,
    actor_email: str,
) -> dict[str, int]:
    """Apply already-validated parser results in one database transaction."""
    applied = 0
    items_created = 0
    confirmation_tasks_created = 0
    skipped = 0
    with database.get_db() as conn:
        for meeting_id, structured_summary in entries:
            row = conn.execute(
                """SELECT structured_summary_json, approved_content_sha256
                     FROM meetings WHERE id=?""",
                (int(meeting_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到會議：{meeting_id}")
            if row["structured_summary_json"] or row["approved_content_sha256"]:
                skipped += 1
                continue

            conn.execute(
                """UPDATE meetings
                      SET structured_summary_json=?,
                          review_status='needs_review',
                          reviewed_at=NULL,
                          reviewed_by=NULL,
                          review_note='歷史 D/R/A 已由規則式解析回填，待人工確認。'
                    WHERE id=?""",
                (
                    json.dumps(structured_summary, ensure_ascii=False),
                    int(meeting_id),
                ),
            )
            pending_before = int(
                conn.execute(
                    """SELECT COUNT(*) FROM meeting_confirmation_tasks
                        WHERE meeting_id=? AND status='pending'""",
                    (int(meeting_id),),
                ).fetchone()[0]
            )
            database._replace_meeting_items(  # noqa: SLF001
                conn,
                int(meeting_id),
                structured_summary,
            )
            conn.execute(
                """UPDATE meeting_items
                      SET review_status='needs_review',
                          updated_at=?
                    WHERE meeting_id=?""",
                (database._now(), int(meeting_id)),  # noqa: SLF001
            )
            pending_after = int(
                conn.execute(
                    """SELECT COUNT(*) FROM meeting_confirmation_tasks
                        WHERE meeting_id=? AND status='pending'""",
                    (int(meeting_id),),
                ).fetchone()[0]
            )
            confirmation_tasks_created += max(0, pending_after - pending_before)
            created = int(
                conn.execute(
                    "SELECT COUNT(*) FROM meeting_items WHERE meeting_id=?",
                    (int(meeting_id),),
                ).fetchone()[0]
            )
            items_created += created
            applied += 1

        actor = conn.execute(
            "SELECT id FROM app_users WHERE email=?",
            (str(actor_email or "").strip().lower(),),
        ).fetchone()
        conn.execute(
            """INSERT INTO audit_logs (
                   actor_user_id, actor_email, action, resource_type,
                   resource_id, detail_json, created_at
               ) VALUES (?, ?, 'meeting.structured_backfill',
                         'meeting_collection', 'all', ?, ?)""",
            (
                int(actor["id"]) if actor else None,
                str(actor_email or "").strip().lower() or None,
                json.dumps(
                    {
                        "applied_meetings": applied,
                        "items_created": items_created,
                        "confirmation_tasks_created": confirmation_tasks_created,
                        "skipped": skipped,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                database._now(),  # noqa: SLF001
            ),
        )
    return {
        "applied_meetings": applied,
        "items_created": items_created,
        "confirmation_tasks_created": confirmation_tasks_created,
        "skipped": skipped,
    }


def list_confirmation_tasks(
    *,
    status: str = "pending",
    meeting_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    normalized_status = str(status or "pending").strip().lower()
    if normalized_status not in {"pending", "resolved", "waived", "all"}:
        raise ValueError(f"不支援的確認狀態：{status}")
    clauses: list[str] = []
    params: list[Any] = []
    if normalized_status != "all":
        clauses.append("t.status=?")
        params.append(normalized_status)
    if meeting_id is not None:
        clauses.append("t.meeting_id=?")
        params.append(int(meeting_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend((min(max(int(limit), 1), 500), max(int(offset), 0)))
    with database.get_db() as conn:
        rows = conn.execute(
            f"""SELECT t.*, m.title AS meeting_title, i.item_type,
                       i.item_key, i.payload_json
                  FROM meeting_confirmation_tasks AS t
                  JOIN meetings AS m ON m.id=t.meeting_id
                  JOIN meeting_items AS i ON i.id=t.meeting_item_id
                  {where}
                 ORDER BY CASE t.status WHEN 'pending' THEN 0 ELSE 1 END,
                          t.meeting_id, i.position, t.id
                 LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        payload_json = record.pop("payload_json", None)
        record["item_payload"] = json.loads(payload_json or "{}")
        records.append(record)
    return records


def update_confirmation_task(
    task_id: int,
    *,
    status: str,
    resolution_value: str | None,
    resolution_note: str | None,
    actor_email: str,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"resolved", "waived"}:
        raise ValueError("確認狀態必須是 resolved 或 waived")
    normalized_value = str(resolution_value or "").strip()
    if normalized_status == "resolved" and not normalized_value:
        raise ValueError("resolved 必須提供 resolution_value")

    with database.get_db() as conn:
        row = conn.execute(
            """SELECT t.*, i.item_type, i.item_key, i.payload_json,
                      i.evidence_json, m.structured_summary_json
                 FROM meeting_confirmation_tasks AS t
                 JOIN meeting_items AS i ON i.id=t.meeting_item_id
                 JOIN meetings AS m ON m.id=t.meeting_id
                WHERE t.id=?""",
            (int(task_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到確認任務：{task_id}")
        if str(row["status"]) != "pending":
            raise ValueError("此確認任務已處理")

        now = database._now()  # noqa: SLF001
        field_name = str(row["field_name"])
        if normalized_status == "resolved":
            payload = json.loads(row["payload_json"] or "{}")
            value: Any = normalized_value
            if field_name in _EVIDENCE_FIELDS:
                value = [
                    part.strip()
                    for part in normalized_value.replace("、", ",").split(",")
                    if part.strip()
                ]
            payload[field_name] = value
            payload["confirmation_required"] = [
                item
                for item in payload.get("confirmation_required") or []
                if item != field_name
            ]
            evidence_field = (
                "source_timecodes"
                if str(row["item_type"]) == "action"
                else "evidence_timecodes"
            )
            evidence = payload.get(evidence_field) or []
            conn.execute(
                """UPDATE meeting_items
                      SET payload_json=?, evidence_json=?, updated_at=?
                    WHERE id=?""",
                (
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                    int(row["meeting_item_id"]),
                ),
            )

            structured = json.loads(row["structured_summary_json"] or "{}")
            field = _STRUCTURED_FIELDS[str(row["item_type"])]
            for item in structured.get(field) or []:
                if str(item.get("id")) == str(row["item_key"]):
                    item.update(payload)
                    break
            conn.execute(
                """UPDATE meetings
                      SET structured_summary_json=?, review_status='needs_review'
                    WHERE id=?""",
                (
                    json.dumps(structured, ensure_ascii=False),
                    int(row["meeting_id"]),
                ),
            )

        conn.execute(
            """UPDATE meeting_confirmation_tasks
                  SET status=?, resolution_value=?, resolution_note=?,
                      resolved_by=?, resolved_at=?, updated_at=?
                WHERE id=?""",
            (
                normalized_status,
                normalized_value or None,
                str(resolution_note or "").strip() or None,
                str(actor_email or "").strip().lower() or None,
                now,
                now,
                int(task_id),
            ),
        )
        updated = conn.execute(
            "SELECT * FROM meeting_confirmation_tasks WHERE id=?",
            (int(task_id),),
        ).fetchone()
    return dict(updated)
