"""Deterministic parser for the standardized meeting-minutes Markdown format."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_TIMECODE_RE = re.compile(r"(?<!\d)\d{1,3}:[0-5]\d(?::[0-5]\d)?(?!\d)")
_ITEM_ID_RE = re.compile(r"\b([DRA]\d+)\b", flags=re.IGNORECASE)
_DISCUSSION_HEADING_RE = re.compile(
    r"^###\s+(D\d+)\.\s*(.+?)\s*$",
    flags=re.MULTILINE | re.IGNORECASE,
)
_BULLET_FIELD_RE = re.compile(r"^-\s*([^：:\n]+)[：:]\s*(.*)$")


def _section(markdown: str, start_terms: tuple[str, ...], end_terms: tuple[str, ...]) -> str:
    start_pattern = "|".join(re.escape(term) for term in start_terms)
    end_pattern = "|".join(re.escape(term) for term in end_terms)
    match = re.search(
        rf"^##\s+[^\n]*(?:{start_pattern})[^\n]*\n"
        rf"(?P<body>.*?)(?=^##\s+[^\n]*(?:{end_pattern})|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group("body").strip() if match else ""


def _timecodes(value: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _TIMECODE_RE.finditer(value)))


def _related_ids(value: str, prefix: str) -> list[str]:
    expected = prefix.upper()
    return [
        item_id.upper()
        for item_id in dict.fromkeys(
            match.group(1).upper() for match in _ITEM_ID_RE.finditer(value)
        )
        if item_id.startswith(expected)
    ]


def _plain_cell(value: str) -> str:
    return re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.IGNORECASE).strip()


def _content_without_evidence(value: str) -> str:
    plain = _plain_cell(value)
    return re.sub(
        r"(?:\n|\s)*佐證[：:].*$",
        "",
        plain,
        flags=re.DOTALL,
    ).strip()


def _table_rows(section: str, expected_columns: int, errors: list[str], label: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line_number, raw_line in enumerate(section.splitlines(), start=1):
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        first = cells[0].strip().upper() if cells else ""
        if not first or first in {"ID", "編號"} or re.fullmatch(r":?-+:?", first):
            continue
        if not re.fullmatch(rf"{re.escape(label)}\d+", first, flags=re.IGNORECASE):
            continue
        if len(cells) != expected_columns:
            errors.append(
                f"{label} table line {line_number}: expected {expected_columns} columns, got {len(cells)}"
            )
            continue
        rows.append(cells)
    return rows


def parse_standardized_minutes(markdown: str, *, source_path: Path | None = None) -> dict[str, Any]:
    """Parse D/R/A records without inference or model calls."""
    text = str(markdown or "").replace("\r\n", "\n")
    errors: list[str] = []
    discussions_section = _section(
        text,
        ("一、討論摘要", "Discussion Summary"),
        ("二、最終決議", "Final Decisions"),
    )
    decisions_section = _section(
        text,
        ("二、最終決議", "Final Decisions"),
        ("三、待辦事項", "Action Items"),
    )
    actions_section = _section(
        text,
        ("三、待辦事項", "Action Items"),
        ("四、完整逐字稿", "Verbatim Transcript"),
    )
    if not discussions_section:
        errors.append("missing discussion section")
    if not decisions_section:
        errors.append("missing decision section")
    if not actions_section:
        errors.append("missing action section")

    discussions: list[dict[str, Any]] = []
    discussion_matches = list(_DISCUSSION_HEADING_RE.finditer(discussions_section))
    for index, match in enumerate(discussion_matches):
        block_end = (
            discussion_matches[index + 1].start()
            if index + 1 < len(discussion_matches)
            else len(discussions_section)
        )
        block = discussions_section[match.end():block_end]
        fields: dict[str, str] = {}
        for line in block.splitlines():
            field_match = _BULLET_FIELD_RE.match(line.strip())
            if field_match:
                fields[field_match.group(1).strip()] = field_match.group(2).strip()
        normalized_fields = {
            key.replace("/", "／").replace(" ", ""): value
            for key, value in fields.items()
        }
        evidence_value = normalized_fields.get("佐證時間", "")
        item = {
            "id": match.group(1).upper(),
            "topic": match.group(2).strip(),
            "context": normalized_fields.get("背景", "未提及"),
            "summary": normalized_fields.get("摘要", "未提及"),
            "key_points": [normalized_fields.get("重點", "未提及")],
            "impact": normalized_fields.get("影響／風險", "未提及"),
            "open_questions": [normalized_fields.get("待釐清", "未提及")],
            "evidence_timecodes": _timecodes(evidence_value),
        }
        item["confirmation_required"] = (
            [] if item["evidence_timecodes"] else ["evidence_timecodes"]
        )
        if not item["summary"] or item["summary"] == "未提及":
            errors.append(f"{item['id']}: missing summary")
        discussions.append(item)

    decisions: list[dict[str, Any]] = []
    for cells in _table_rows(decisions_section, 5, errors, "R"):
        item = {
            "id": cells[0].upper(),
            "related_discussions": _related_ids(cells[1], "D"),
            "decision": _plain_cell(cells[2]),
            "basis": _content_without_evidence(cells[3]),
            "status": cells[4].strip().lower(),
            "evidence_timecodes": _timecodes(cells[3]),
        }
        item["confirmation_required"] = (
            [] if item["evidence_timecodes"] else ["evidence_timecodes"]
        )
        if not item["decision"]:
            errors.append(f"{item['id']}: missing decision")
        decisions.append(item)

    actions: list[dict[str, Any]] = []
    for cells in _table_rows(actions_section, 7, errors, "A"):
        owner = _plain_cell(cells[4]) or "未提及"
        due = _plain_cell(cells[5]) or "未提及"
        item = {
            "id": cells[0].upper(),
            "related_discussions": _related_ids(cells[1], "D"),
            "related_decisions": _related_ids(cells[2], "R"),
            "task": _content_without_evidence(cells[3]),
            "owner": owner,
            "due": due,
            "due_source": due,
            "priority": _plain_cell(cells[6]) or "中",
            "source_timecodes": _timecodes(cells[3]),
        }
        item["confirmation_required"] = [
            field
            for field, value in (("owner", owner), ("due", due))
            if value in {"未提及", "待確認", "不確定", ""}
        ]
        if not item["source_timecodes"]:
            item["confirmation_required"].append("source_timecodes")
        if not item["task"]:
            errors.append(f"{item['id']}: missing task")
        actions.append(item)

    all_ids = [
        *(item["id"] for item in discussions),
        *(item["id"] for item in decisions),
        *(item["id"] for item in actions),
    ]
    if len(all_ids) != len(set(all_ids)):
        errors.append("duplicate D/R/A ids")

    return {
        "source": "deterministic_markdown_backfill_v1",
        "source_path": str(source_path) if source_path is not None else None,
        "discussion_summary": discussions,
        "final_decisions": decisions,
        "action_items": actions,
        "parse_errors": errors,
    }
