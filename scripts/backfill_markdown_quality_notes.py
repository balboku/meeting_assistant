#!/usr/bin/env python3
"""Backfill actionable transcript-quality notes into stored Markdown files.

The API and DOCX export can inject transcript review locations at read time.
Older Markdown files on disk may still only contain a vague warning, so direct
file reads or copied Markdown can miss the exact segment that should be checked.
This script updates those Markdown files only when a structured segment summary
can be derived from the database row and transcript.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database  # noqa: E402
from backend.exporter import content_with_quality_review_note  # noqa: E402


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _markdown_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text)


def _read_markdown(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _write_text_atomic(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _iter_meeting_rows(limit: int | None = None) -> list[sqlite3.Row]:
    sql = """SELECT id, title, date, source_audio, output_path, summary,
                    job_id, quality_score, quality_label, quality_report_json,
                    created_at
               FROM meetings
              ORDER BY created_at DESC, id DESC"""
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with database.get_db() as conn:
        return conn.execute(sql, params).fetchall()


def _meeting_record_with_quality_note(row: sqlite3.Row) -> tuple[dict[str, Any] | None, str, str]:
    record = dict(row)
    path = _markdown_path(record.get("output_path"))
    original = _read_markdown(path)
    if not original:
        return None, original, ""

    quality_report = _load_json(record.get("quality_report_json"))
    quality_fields = database.apply_quality_preview_fields(
        {
            **record,
            "quality_report": quality_report if quality_report else None,
            "full_content": original,
        },
        quality_report=quality_report if quality_report else None,
    )
    summary = str(quality_fields.get("quality_review_segment_summary") or "").strip()
    if not summary:
        return None, original, ""

    meeting_record = {
        **record,
        **quality_fields,
        "quality_report": quality_report if quality_report else None,
        "full_content": original,
    }
    updated = content_with_quality_review_note(meeting_record)
    if updated == original:
        return None, original, summary
    return meeting_record, original, updated


def backfill_markdown_quality_notes(*, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    rows = _iter_meeting_rows(limit)
    records: list[dict[str, Any]] = []
    updates: list[tuple[sqlite3.Row, dict[str, Any], str]] = []

    for row in rows:
        meeting_record, original, updated = _meeting_record_with_quality_note(row)
        if meeting_record is None:
            continue
        output_path = Path(str(meeting_record.get("output_path") or ""))
        review_summary = str(meeting_record.get("quality_review_segment_summary") or "").strip()
        records.append(
            {
                "id": int(row["id"]),
                "title": row["title"],
                "output_path": str(output_path),
                "review_summary": review_summary,
                "original_bytes": len(original.encode("utf-8")),
                "updated_bytes": len(updated.encode("utf-8")),
            }
        )
        updates.append((row, meeting_record, updated))

    updated_count = 0
    if apply and updates:
        for _row, meeting_record, updated in updates:
            output_path = Path(str(meeting_record.get("output_path") or ""))
            _write_text_atomic(output_path, updated)
            updated_count += 1
        with database.get_db() as conn:
            for row, meeting_record, updated in updates:
                database._upsert_meeting_fts_row(
                    conn,
                    int(row["id"]),
                    str(row["title"]),
                    str(row["source_audio"]),
                    row["summary"],
                    str(row["output_path"]),
                    content=updated,
                )

    return {
        "database": str(database.DB_PATH),
        "dry_run": not apply,
        "scanned": len(rows),
        "would_update": len(records),
        "updated": updated_count,
        "records": records,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill actionable transcript-quality notes into stored Markdown meeting files."
    )
    parser.add_argument("--apply", action="store_true", help="Write changes to Markdown files.")
    parser.add_argument("--limit", type=int, default=None, help="Limit scanned meetings.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    database.init_db()
    payload = backfill_markdown_quality_notes(apply=args.apply, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
