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


@dataclass
class ConsistencyProblem:
    meeting_id: int | None
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
                    surface=surface,
                    field=field,
                    expected=expected.get(field),
                    actual=actual.get(field),
                )
            )
    return problems


async def _audit(client: Any, limit: int) -> dict[str, Any]:
    list_response = await client.get("/meetings", params={"limit": limit})
    list_response.raise_for_status()
    listed = _records(list_response.json())
    problems: list[ConsistencyProblem] = []
    search_checked = 0

    for record in listed:
        meeting_id = record.get("id")
        detail_response = await client.get(f"/meetings/{meeting_id}")
        detail_response.raise_for_status()
        detail = detail_response.json()
        problems.extend(
            _compare_fields(int(meeting_id), "list-detail", record, detail)
        )

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
                    surface="search-detail",
                    field="missing",
                    expected=query,
                    actual=None,
                )
            )
            continue
        search_checked += 1
        problems.extend(
            _compare_fields(int(meeting_id), "search-detail", matched, detail)
        )

    return {
        "passed": not problems,
        "records": len(listed),
        "search_checked": search_checked,
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
