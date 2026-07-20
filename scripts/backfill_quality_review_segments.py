#!/usr/bin/env python3
"""Backfill structured transcript review segments into stored quality reports.

The API can derive review segment locations at read time, but older rows may
still have vague warnings persisted in quality_report_json. This maintenance
script stores the derived review_segments and a located warning so external
tools and future reads do not depend on recomputing the legacy Markdown.
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


def _read_markdown(path_value: Any) -> str:
    path = Path(str(path_value or ""))
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _segment_index(segment: dict[str, Any]) -> int | None:
    try:
        index = int(segment.get("index"))
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _merge_review_segments(
    existing_segments: Any,
    derived_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}

    def add(segment: Any, *, prefer_existing: bool) -> None:
        if not isinstance(segment, dict):
            return
        index = _segment_index(segment)
        if index is None:
            return
        item = merged.setdefault(
            index,
            {
                "index": index,
                "label": segment.get("label") or database.review_segment_label(index),
                "issues": [],
            },
        )
        for key in ("label", "start_seconds", "end_seconds", "status"):
            if prefer_existing and segment.get(key) not in (None, ""):
                item[key] = segment.get(key)
            elif item.get(key) in (None, "") and segment.get(key) not in (None, ""):
                item[key] = segment.get(key)
        issues = [
            str(issue).strip()
            for issue in segment.get("issues") or []
            if str(issue or "").strip()
        ]
        for issue in issues:
            if issue not in item["issues"]:
                item["issues"].append(issue)

    for segment in existing_segments or []:
        add(segment, prefer_existing=True)
    for segment in derived_segments:
        add(segment, prefer_existing=False)

    return [merged[index] for index in sorted(merged)]


def _warning_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        source = value
    else:
        source = str(value or "").splitlines()
    return [str(item).strip() for item in source if str(item or "").strip()]


def _has_located_review_warning(warnings: list[str]) -> bool:
    return any(
        "逐字稿品質警示" in warning
        and ("問題位置" in warning or "需複核分段" in warning)
        for warning in warnings
    )


def _located_review_warning(summary: str) -> str:
    return (
        f"逐字稿品質警示：問題位置：{summary}。"
        "建議重跑上述分段或複核相關內容。"
    )


def _quality_report_score(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_updated_report(row: sqlite3.Row) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    record = dict(row)
    quality_report = _load_json(record.get("quality_report_json"))
    full_content = _read_markdown(record.get("output_path"))
    derived = database.apply_quality_preview_fields(
        {
            **record,
            "quality_report": quality_report if quality_report else None,
            "full_content": full_content,
        },
        quality_report=quality_report if quality_report else None,
    )
    derived_segments = [
        segment
        for segment in derived.get("quality_review_segment_details") or []
        if isinstance(segment, dict) and _segment_index(segment) is not None
    ]
    if not derived_segments:
        return None, derived

    updated_report = deepcopy(quality_report)
    existing_segments = updated_report.get("review_segments") if isinstance(updated_report, dict) else []
    merged_segments = _merge_review_segments(existing_segments, derived_segments)
    if not merged_segments:
        return None, derived

    changed = merged_segments != (existing_segments or [])
    updated_report["review_segments"] = merged_segments

    warnings = _warning_lines(updated_report.get("warnings"))
    derived_summary = str(derived.get("quality_review_segment_summary") or "").strip()
    if derived_summary and not _has_located_review_warning(warnings):
        warnings.insert(0, _located_review_warning(derived_summary))
        changed = True
    if warnings != _warning_lines(updated_report.get("warnings")):
        updated_report["warnings"] = list(dict.fromkeys(warnings))

    if not updated_report.get("label"):
        updated_report["label"] = str(derived.get("quality_effective_label") or "需複核逐字稿")
        changed = True
    if updated_report.get("score") is None and derived.get("quality_effective_score") is not None:
        updated_report["score"] = derived.get("quality_effective_score")
        changed = True

    return (updated_report if changed else None), derived


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


def backfill_quality_review_segments(*, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    rows = _iter_meeting_rows(limit)
    records: list[dict[str, Any]] = []
    updates: list[tuple[str, int | None, str | None, int]] = []

    for row in rows:
        updated_report, derived = _build_updated_report(row)
        if updated_report is None:
            continue

        review_segments = updated_report.get("review_segments") or []
        labels = [
            str(segment.get("label") or database.review_segment_label(int(segment["index"])))
            for segment in review_segments
            if isinstance(segment, dict) and _segment_index(segment) is not None
        ]
        rerunnable_segments = [
            int(index)
            for index in derived.get("quality_review_rerunnable_segments") or []
            if isinstance(index, int)
        ]
        score = _quality_report_score(updated_report.get("score"))
        label = str(updated_report.get("label") or "").strip() or None
        records.append(
            {
                "id": int(row["id"]),
                "title": row["title"],
                "review_segments": labels,
                "rerunnable_segments": rerunnable_segments,
                "warning_summary": derived.get("quality_review_segment_summary"),
            }
        )
        updates.append((json.dumps(updated_report, ensure_ascii=False), score, label, int(row["id"])))

    if apply and updates:
        with database.get_db() as conn:
            conn.executemany(
                """UPDATE meetings
                      SET quality_report_json=?,
                          quality_score=?,
                          quality_label=?
                    WHERE id=?""",
                updates,
            )

    return {
        "database": str(database.DB_PATH),
        "dry_run": not apply,
        "scanned": len(rows),
        "would_update": len(records),
        "updated": len(records) if apply else 0,
        "records": records,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill structured transcript review segment metadata into quality_report_json."
    )
    parser.add_argument("--apply", action="store_true", help="Write changes to the SQLite database.")
    parser.add_argument("--limit", type=int, default=None, help="Limit scanned meetings.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    database.init_db()
    payload = backfill_quality_review_segments(apply=args.apply, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
