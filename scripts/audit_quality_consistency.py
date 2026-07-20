#!/usr/bin/env python3
"""Audit quality-warning fields across list, search, and detail APIs.

The default mode imports the FastAPI app in-process, so the backend does not
need to be running. Use --base-url to audit a running service instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


QUALITY_FIELDS = (
    "quality_warning_preview",
    "quality_warning_text",
    "quality_warning_count",
    "quality_review_segments",
    "quality_review_segment_details",
    "quality_review_segment_summary",
    "quality_review_segment_count",
    "quality_review_rerunnable_segments",
    "quality_effective_score",
    "quality_effective_label",
)


TRANSCRIPT_REVIEW_SIGNAL_PATTERN = (
    "疑似連續重複轉錄",
    "同一句連續重複",
    "重複轉錄",
    "曾觸發轉錄補救",
    "重複時間",
    "非最後分段",
    "分段含",
)


def _quality_warning_text(record: dict[str, Any]) -> str:
    return "\n".join(
        str(record.get(field) or "")
        for field in ("quality_warning_preview", "quality_warning_text")
    )


@dataclass
class ConsistencyProblem:
    meeting_id: int | None
    meeting_title: str | None
    surface: str
    field: str
    expected: Any
    actual: Any


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("records", "meetings", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _meeting_query(record: dict[str, Any]) -> str:
    return str(
        record.get("title")
        or record.get("source_audio")
        or record.get("id")
        or ""
    )


def _compare_fields(
    meeting_id: int | None,
    meeting_title: str | None,
    surface: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[ConsistencyProblem]:
    problems: list[ConsistencyProblem] = []
    for field in QUALITY_FIELDS:
        if expected.get(field) != actual.get(field):
            problems.append(
                ConsistencyProblem(
                    meeting_id=meeting_id,
                    meeting_title=meeting_title,
                    surface=surface,
                    field=field,
                    expected=expected.get(field),
                    actual=actual.get(field),
                )
            )
    return problems


def _has_transcript_review_signal(record: dict[str, Any]) -> bool:
    return any(token in _quality_warning_text(record) for token in TRANSCRIPT_REVIEW_SIGNAL_PATTERN)


def _has_specific_repeat_warning(record: dict[str, Any]) -> bool:
    warning_text = _quality_warning_text(record)
    return (
        "疑似連續重複轉錄" in warning_text
        and (
            "同一句連續重複" in warning_text
            or "重複時間" in warning_text
        )
    )


def _has_structured_review_location(record: dict[str, Any]) -> bool:
    try:
        if int(record.get("quality_review_segment_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return any(
        isinstance(record.get(field), list) and bool(record.get(field))
        for field in (
            "quality_review_segments",
            "quality_review_segment_details",
            "quality_review_rerunnable_segments",
        )
    )


def _has_actionable_review_location(record: dict[str, Any]) -> bool:
    review_segment_count = int(record.get("quality_review_segment_count") or 0)
    if review_segment_count > 0:
        return True
    for field in (
        "quality_review_segment_summary",
        "quality_review_segments",
        "quality_review_segment_details",
        "quality_review_rerunnable_segments",
    ):
        value = record.get(field)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
    warning_text = _quality_warning_text(record)
    has_location_marker = "問題位置" in warning_text or "需複核分段" in warning_text
    return has_location_marker and "第" in warning_text and "段" in warning_text


def _actionability_problems(record: dict[str, Any]) -> list[ConsistencyProblem]:
    if not _has_transcript_review_signal(record):
        return []
    if _has_actionable_review_location(record):
        if _has_specific_repeat_warning(record) and not _has_structured_review_location(record):
            meeting_id = record.get("id")
            try:
                normalized_id = int(meeting_id)
            except (TypeError, ValueError):
                normalized_id = None
            return [
                ConsistencyProblem(
                    meeting_id=normalized_id,
                    meeting_title=str(record.get("title") or "").strip() or None,
                    surface="detail-actionability",
                    field="quality_repeat_location",
                    expected="連續重複轉錄警示需有結構化 quality_review_segment_details",
                    actual=record.get("quality_warning_text") or record.get("quality_warning_preview"),
                )
            ]
        return []
    meeting_id = record.get("id")
    try:
        normalized_id = int(meeting_id)
    except (TypeError, ValueError):
        normalized_id = None
    return [
        ConsistencyProblem(
            meeting_id=normalized_id,
            meeting_title=str(record.get("title") or "").strip() or None,
            surface="detail-actionability",
            field="quality_actionability",
            expected="逐字稿品質警示需包含問題位置或 quality_review_segment_details",
            actual=record.get("quality_warning_text") or record.get("quality_warning_preview"),
        )
    ]


def _review_location_labels(record: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for detail in record.get("quality_review_segment_details") or []:
        if not isinstance(detail, dict):
            continue
        label = str(detail.get("label") or "").strip()
        if not label:
            try:
                index = int(detail.get("index"))
            except (TypeError, ValueError):
                index = -1
            if index >= 0:
                label = f"第 {index + 1} 段"
        if label and label not in labels:
            labels.append(label)
    for label in record.get("quality_review_segments") or []:
        label_text = str(label or "").strip()
        if label_text and label_text not in labels:
            labels.append(label_text)
    for index_value in record.get("quality_review_rerunnable_segments") or []:
        try:
            index = int(index_value)
        except (TypeError, ValueError):
            continue
        if index < 0:
            continue
        label = f"第 {index + 1} 段"
        if label not in labels:
            labels.append(label)
    return labels


def _markdown_export_problems(record: dict[str, Any], markdown_text: str) -> list[ConsistencyProblem]:
    labels = _review_location_labels(record)
    if not labels:
        return []
    markdown = str(markdown_text or "")
    meeting_id = record.get("id")
    try:
        normalized_id = int(meeting_id)
    except (TypeError, ValueError):
        normalized_id = None
    meeting_title = str(record.get("title") or "").strip() or None
    problems: list[ConsistencyProblem] = []
    has_quality_note = (
        "逐字稿品質複核提示" in markdown
        or "逐字稿品質警示：問題位置" in markdown
    )
    if not has_quality_note:
        problems.append(
            ConsistencyProblem(
                meeting_id=normalized_id,
                meeting_title=meeting_title,
                surface="markdown-export",
                field="quality_review_note",
                expected="Markdown 下載內容需包含逐字稿品質複核提示或逐字稿品質警示問題位置",
                actual=markdown[:240],
            )
        )
        return problems
    present_labels = [label for label in labels if label in markdown]
    if not present_labels:
        problems.append(
            ConsistencyProblem(
                meeting_id=normalized_id,
                meeting_title=meeting_title,
                surface="markdown-export",
                field="quality_review_segments",
                expected=labels,
                actual="Markdown 品質複核提示未列出任何問題分段",
            )
        )
    return problems


async def _audit(client: Any, limit: int) -> dict[str, Any]:
    list_response = await client.get("/meetings", params={"limit": limit})
    list_response.raise_for_status()
    listed = _records(list_response.json())
    problems: list[ConsistencyProblem] = []
    search_checked = 0
    markdown_checked = 0

    for record in listed:
        meeting_id = record.get("id")
        meeting_title = str(record.get("title") or "").strip() or None
        detail_response = await client.get(f"/meetings/{meeting_id}")
        detail_response.raise_for_status()
        detail = detail_response.json()
        problems.extend(_actionability_problems(detail))
        problems.extend(
            _compare_fields(int(meeting_id), meeting_title, "list-detail", record, detail)
        )
        if _review_location_labels(detail):
            markdown_response = await client.get(f"/meetings/{meeting_id}/markdown")
            markdown_response.raise_for_status()
            markdown_checked += 1
            problems.extend(_markdown_export_problems(detail, markdown_response.text))

        query = _meeting_query(record)
        search_response = await client.get(
            "/meetings/search",
            params={"q": query, "limit": limit},
        )
        search_response.raise_for_status()
        search_records = _records(search_response.json())
        matched = next(
            (item for item in search_records if item.get("id") == meeting_id),
            None,
        )
        if matched is None:
            problems.append(
                ConsistencyProblem(
                    meeting_id=int(meeting_id) if meeting_id is not None else None,
                    meeting_title=meeting_title,
                    surface="search-detail",
                    field="missing",
                    expected=query,
                    actual=None,
                )
            )
            continue
        search_checked += 1
        problems.extend(
            _compare_fields(int(meeting_id), meeting_title, "search-detail", matched, detail)
        )

    return {
        "passed": not problems,
        "records": len(listed),
        "search_checked": search_checked,
        "markdown_checked": markdown_checked,
        "problem_count": len(problems),
        "problems": [asdict(problem) for problem in problems],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    headers = {"X-API-Key": args.api_key} if args.api_key else None
    if args.base_url:
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"),
            headers=headers,
            timeout=args.timeout,
        ) as client:
            return await _audit(client, args.limit)

    from backend import database

    database.init_db()

    import backend.main as main

    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 0))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=headers,
        timeout=args.timeout,
    ) as client:
        return await _audit(client, args.limit)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that quality-warning fields match across list, search, and detail APIs.",
    )
    parser.add_argument(
        "--base-url",
        help="Audit a running backend instead of the in-process ASGI app, e.g. http://127.0.0.1:8001.",
    )
    parser.add_argument("--api-key", help="Optional X-API-Key value for protected remote checks.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum meetings to audit.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
