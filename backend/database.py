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
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from contextlib import contextmanager

from backend.quality_segments import (
    review_segment_details_from_text,
    review_segment_label,
    review_segment_label_sort_key,
)
from backend.auth import normalize_role

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


def _markdown_section(text: str, heading_terms: tuple[str, ...], next_terms: tuple[str, ...]) -> str:
    heading_pattern = "|".join(re.escape(term) for term in heading_terms)
    next_pattern = "|".join(re.escape(term) for term in next_terms)
    if next_pattern:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*?)(?=^##\s*[^\n]*(?:{next_pattern})|\Z)"
    else:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*)\Z"
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group("body").strip() if match else ""


def _markdown_ids(text: str, prefix: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(prefix)}\d+\b", text or ""))


def _normalize_turn_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return re.sub(r"[，。,.、；;：:！!？?\-—~「」『』（）()\[\]【】\"'`*_]+", "", normalized)


def _has_repeated_transcript_turn_loop(transcript: str, limit: int = 3) -> bool:
    return _repeated_transcript_turn_review_segments(transcript, limit=limit) != []


def _format_clock(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds or 0)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _quality_review_issue_priority(issue: str) -> tuple[int, int]:
    cleaned = str(issue or "").strip()
    if not cleaned:
        return (0, 0)
    score = 1
    if "曾觸發轉錄補救" in cleaned:
        score += 5
    if "重複轉錄幻覺" in cleaned:
        score += 20
    if "疑似連續重複轉錄" in cleaned:
        score += 30
    if re.search(r"(?:重複時間|問題時間|異常時間)[：:]", cleaned):
        score += 30
    if re.search(r"\d{1,3}:[0-5]\d\s*[-–—~至到]\s*\d{1,3}:[0-5]\d", cleaned):
        score += 20
    if "同一句連續重複" in cleaned:
        score += 50
    if re.search(r"同一句連續重複\s*\d+\s*次[：:]", cleaned):
        score += 60
    return (score, len(cleaned))


def _normalize_repeated_phrase_preview(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    cleaned = re.sub(
        r"^(?:\[(台語|臺語|閩南語|英語|英文|日語|日文|中文|國語|普通話)\]"
        r"|【(台語|臺語|閩南語|英語|英文|日語|日文|中文|國語|普通話)】"
        r"|（(台語|臺語|閩南語|英語|英文|日語|日文|中文|國語|普通話)）"
        r"|\((台語|臺語|閩南語|英語|英文|日語|日文|中文|國語|普通話)\))\s*",
        lambda match: next(group for group in match.groups() if group),
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:\[[^\]]{1,12}\]|【[^】]{1,12}】|（[^）]{1,12}）|\([^)]{1,12}\))\s*",
        "",
        cleaned,
    )
    cleaned = cleaned.strip("：:，。,.、；;！!？?()（）[]【】「」『』")
    cleaned = re.sub(r"[，,。．.；;：:、]+", "", cleaned)
    return cleaned.strip()


def _repeated_phrase_dedupe_key(value: str) -> str:
    cleaned = re.sub(r"\s+", "", _normalize_repeated_phrase_preview(value))
    return re.sub(r"^(?:台語|臺語|閩南語|英語|英文|日語|日文|中文|國語|普通話)", "", cleaned)


_REPEATED_PHRASE_TERMINATOR_PATTERN = (
    r"(?="
    r"(?:[；;]\s*(?:重複時間|重複時段|問題時間|異常時間|疑似分段|第\s*\d+\s*段|Segment\b))"
    r"|[）)\]】]"
    r"|(?:[，,。．.]\s*(?:建議|需|請|可|應|若|如果))"
    r"|$"
    r")"
)


def _extract_repeated_phrase_clause(value: str) -> Optional[re.Match[str]]:
    return re.search(
        r"同一句連續重複\s*(?P<count>\d+)\s*次[：:]\s*"
        r"(?P<phrase>[^\r\n]{1,120}?)"
        + _REPEATED_PHRASE_TERMINATOR_PATTERN,
        str(value or ""),
        flags=re.IGNORECASE,
    )


def _normalize_quality_review_issue_text(issue: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(issue or "").strip())
    if not cleaned:
        return ""

    repeated = re.match(
        r"^疑似連續重複轉錄[：:]\s*"
        r"同一句連續重複\s*(?P<count>\d+)\s*次"
        r"(?:[：:]\s*(?P<phrase>.*?))?"
        r"(?:[（(]\s*(?P<start>\d{1,3}:[0-5]\d)\s*[-–—~至到]\s*"
        r"(?P<end>\d{1,3}:[0-5]\d)\s*[）)])?$",
        cleaned,
    )
    if repeated:
        normalized = f"疑似連續重複轉錄；同一句連續重複 {repeated.group('count')} 次"
        phrase = str(repeated.group("phrase") or "").strip()
        if phrase:
            phrase = _normalize_repeated_phrase_preview(phrase)
            normalized += f"：{phrase}"
        if repeated.group("start") and repeated.group("end"):
            normalized += f"；重複時間：{repeated.group('start')}-{repeated.group('end')}"
        return normalized

    return cleaned


def _quality_review_issue_dedupe_key(issue: str) -> str:
    cleaned = _normalize_quality_review_issue_text(issue)
    repeated = re.match(
        r"^疑似連續重複轉錄；同一句連續重複\s*(?P<count>\d+)\s*次"
        r"(?:[：:]\s*(?P<phrase>.*?))?"
        r"(?:；重複時間[：:]\s*(?P<start>\d{1,3}:[0-5]\d)\s*[-–—~至到]\s*"
        r"(?P<end>\d{1,3}:[0-5]\d))?$",
        cleaned,
    )
    if repeated:
        phrase = _repeated_phrase_dedupe_key(str(repeated.group("phrase") or ""))
        return "|".join([
            "repeat",
            repeated.group("count") or "",
            phrase,
            repeated.group("start") or "",
            repeated.group("end") or "",
        ])
    return cleaned


def _is_actionable_repeated_review_issue(issue: str) -> bool:
    cleaned = _normalize_quality_review_issue_text(issue)
    return (
        "疑似連續重複轉錄" in cleaned
        and "同一句連續重複" in cleaned
        and (
            "重複時間" in cleaned
            or re.search(r"\d{1,3}:[0-5]\d\s*[-–—~至到]\s*\d{1,3}:[0-5]\d", cleaned)
        )
    )


def _is_generic_repeated_review_issue(issue: str) -> bool:
    cleaned = _normalize_quality_review_issue_text(issue)
    return (
        "重複轉錄幻覺" in cleaned
        and "同一句連續重複" not in cleaned
        and "重複時間" not in cleaned
    )


def _repeated_review_issue_time_range(issue: str) -> Optional[tuple[str, str]]:
    cleaned = _normalize_quality_review_issue_text(issue)
    match = re.search(
        r"(?:重複時間[：:]\s*)?"
        r"(?P<start>\d{1,3}:[0-5]\d)\s*[-–—~至到]\s*"
        r"(?P<end>\d{1,3}:[0-5]\d)",
        cleaned,
    )
    if not match:
        return None
    return (match.group("start"), match.group("end"))


def _is_time_only_repeated_review_issue(issue: str) -> bool:
    cleaned = _normalize_quality_review_issue_text(issue)
    return (
        "疑似連續重複轉錄" in cleaned
        and "同一句連續重複" not in cleaned
        and _repeated_review_issue_time_range(cleaned) is not None
    )


def _ordered_quality_review_issues(issues: Any) -> list[str]:
    ordered: list[str] = []
    seen_keys: set[str] = set()
    for issue in issues or []:
        normalized = _normalize_quality_review_issue_text(str(issue).strip())
        if not normalized:
            continue
        key = _quality_review_issue_dedupe_key(normalized)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ordered.append(normalized)
    if any(_is_actionable_repeated_review_issue(issue) for issue in ordered):
        ordered = [
            issue
            for issue in ordered
            if not _is_generic_repeated_review_issue(issue)
        ]
    actionable_ranges = {
        time_range
        for issue in ordered
        if _is_actionable_repeated_review_issue(issue)
        for time_range in [_repeated_review_issue_time_range(issue)]
        if time_range is not None
    }
    if actionable_ranges:
        ordered = [
            issue
            for issue in ordered
            if not (
                _is_time_only_repeated_review_issue(issue)
                and _repeated_review_issue_time_range(issue) in actionable_ranges
            )
        ]
    return sorted(ordered, key=_quality_review_issue_priority, reverse=True)


def _quality_review_issue_display(issues: Any) -> str:
    ordered = _ordered_quality_review_issues(issues)
    if not ordered:
        return ""
    primary = ordered[0]
    extras = ordered[1:]
    if not extras:
        return primary
    return f"{primary}（另 {len(extras)} 項）"


def _quality_review_segment_summary(details: list[dict[str, Any]], limit: int = 3) -> Optional[str]:
    if not details:
        return None

    def int_or_none(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def segment_text(detail: dict[str, Any]) -> str:
        label = str(detail.get("label") or "").strip()
        index = int_or_none(detail.get("index"))
        if not label and index is not None:
            label = review_segment_label(index)
        if not label:
            label = "分段"
        start_seconds = int_or_none(detail.get("start_seconds"))
        end_seconds = int_or_none(detail.get("end_seconds"))
        time_text = ""
        if start_seconds is not None or end_seconds is not None:
            start = start_seconds if start_seconds is not None else end_seconds
            end = end_seconds if end_seconds is not None else start_seconds
            time_text = f" {_format_clock(start or 0)}-{_format_clock(end or start or 0)}"
        return f"{label}{time_text}"

    grouped_segments: dict[str, dict[str, Any]] = {}
    for detail in details[:limit]:
        ordered_issues = _ordered_quality_review_issues(detail.get("issues") or [])
        issue = _quality_review_issue_display(ordered_issues)
        key = "\n".join(_quality_review_issue_dedupe_key(issue) for issue in ordered_issues)
        group = grouped_segments.setdefault(key, {"issue": issue, "segments": []})
        group["segments"].append(segment_text(detail))

    summaries: list[str] = []
    for group in grouped_segments.values():
        issue = group["issue"]
        segments = group["segments"]
        segment_list = "、".join(segments)
        summaries.append(f"{segment_list}{f'：{issue}' if issue else ''}")
    if len(details) > limit:
        summaries.append(f"另有 {len(details) - limit} 段")
    return "；".join(summaries) or None


def _review_segment_label_variants(label: str) -> list[str]:
    cleaned = str(label or "").strip()
    variants = [cleaned] if cleaned else []
    match = re.search(r"第\s*(\d+)\s*段", cleaned)
    if match:
        number = match.group(1)
        variants.extend([
            f"第 {number} 段",
            f"第{number}段",
            f"Segment #{number}",
            f"Segment {number}",
        ])
    return list(dict.fromkeys(value for value in variants if value))


def _warning_covers_segment_issue(warning_text: str, preview_text: str) -> bool:
    warning = str(warning_text or "")
    preview = str(preview_text or "").strip()
    if not warning or "：" not in preview:
        return False

    label, issue = preview.split("：", 1)
    if not any(variant in warning for variant in _review_segment_label_variants(label)):
        return False

    issue_tokens = [
        token
        for token in (
            "疑似連續重複轉錄",
            "重複轉錄",
            "曾觸發轉錄補救",
            "轉錄內容為空",
            "自動過濾",
            "截斷提示",
        )
        if token in issue
    ]
    repeated_match = re.search(r"(?:同一句連續重複\s*\d+\s*次|連續重複\s*\d+\s*句|重複比例\s*\d+%)", issue)
    time_match = re.search(r"\d{1,3}:[0-5]\d\s*[-–—~至到]\s*\d{1,3}:[0-5]\d", issue)
    if repeated_match:
        issue_tokens.append(repeated_match.group(0))
    if time_match:
        issue_tokens.append(time_match.group(0).replace(" ", ""))

    if not issue_tokens:
        return False
    normalized_warning = warning.replace(" ", "")
    return any(token.replace(" ", "") in normalized_warning for token in issue_tokens)


def _append_uncovered_quality_previews(
    warning_text_items: list[str],
    warning_text: str,
    previews: list[str],
) -> None:
    for preview in previews:
        cleaned = str(preview or "").strip()
        if not cleaned:
            continue
        if _warning_covers_segment_issue(warning_text, cleaned):
            continue
        warning_text_items.append(cleaned)


TRANSCRIPT_REVIEW_SUMMARY_TOKENS = (
    "疑似連續重複轉錄",
    "重複轉錄",
    "曾觸發轉錄補救",
    "轉錄內容為空",
    "自動過濾",
    "截斷提示",
)


def _has_transcript_review_signal(text: str) -> bool:
    return any(token in str(text or "") for token in TRANSCRIPT_REVIEW_SUMMARY_TOKENS)


_GENERIC_TRANSCRIPT_REVIEW_WARNING_PATTERN = re.compile(
    r"^逐字稿品質警示[：:]\s*"
    r"(?:需抽查|需複核|建議抽查|建議複核|需複核相關內容|需複核相關分段|建議重跑或複核相關分段)"
    r"[。.]?$"
)


def _is_generic_transcript_review_warning(text: str) -> bool:
    return bool(_GENERIC_TRANSCRIPT_REVIEW_WARNING_PATTERN.match(str(text or "").strip()))


def _is_recording_warning_preview(text: str) -> bool:
    return bool(re.search(r"錄音|音檔|媒體|音量|爆音|靜音|聲道|取樣率", str(text or "")))


def _should_promote_review_summary_to_preview(warning_preview: Optional[str], review_summary: str) -> bool:
    warning = str(warning_preview or "").strip()
    review = str(review_summary or "").strip()
    if not warning or not review:
        return False
    if _has_standard_review_location_warning(warning):
        return False
    if review_segment_details_from_text(warning):
        return True
    if _has_transcript_review_signal(warning):
        return True
    if _is_recording_warning_preview(warning) and _has_transcript_review_signal(review):
        return True
    if warning.startswith("品質分數") and _has_transcript_review_signal(review):
        return True
    return False


def _review_summary_location_warning(review_summary: str) -> str:
    return (
        f"逐字稿品質警示：問題位置：{review_summary}。"
        "建議重跑上述分段或複核相關內容。"
    )


_STANDARD_REVIEW_LOCATION_PATTERN = re.compile(
    r"(?:^|\n)\s*逐字稿品質警示[：:]\s*(?:問題位置|需複核分段)[：:]"
)


def _has_standard_review_location_warning(text: str) -> bool:
    return bool(_STANDARD_REVIEW_LOCATION_PATTERN.search(str(text or "")))


def _is_derived_review_location_warning(text: str) -> bool:
    warning = str(text or "")
    return (
        _has_standard_review_location_warning(warning)
        and "建議重跑上述分段或複核相關內容" in warning
    )


_PROBLEM_LOCATION_WARNING_PATTERN = re.compile(
    r"(?:^|\n)\s*逐字稿品質警示[：:]\s*問題位置[：:]"
)


def _has_problem_location_warning(text: str) -> bool:
    return bool(_PROBLEM_LOCATION_WARNING_PATTERN.search(str(text or "")))


def _quality_warning_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]


def _is_redundant_transcript_warning_line(line: str, *, has_problem_location: bool) -> bool:
    if not has_problem_location:
        return False
    warning = str(line or "").strip()
    if not warning:
        return False
    if "摘要品質警示" in warning:
        return False
    if re.match(r"^逐字稿品質警示[：:]\s*問題位置[：:]", warning):
        return False
    if re.match(r"^逐字稿品質警示[：:]\s*需複核分段[：:]", warning):
        return True
    if _is_generic_transcript_review_warning(warning):
        return True
    if warning.startswith("逐字稿品質警示") and (
        _has_transcript_review_signal(warning)
        or "以下分段曾觸發轉錄品質補救或需複核" in warning
        or "完整逐字稿區塊" in warning
    ):
        return True
    if _is_recording_warning_preview(warning):
        return False
    if re.match(r"^(?:第\s*\d+\s*段|Segment\s*#?\d+)\s*[：:]", warning) and _has_transcript_review_signal(warning):
        return True
    return False


def _compact_quality_warning_lines(lines: list[str]) -> list[str]:
    unique_lines = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
    has_problem_location = any(_has_problem_location_warning(line) for line in unique_lines)
    return [
        line
        for line in unique_lines
        if not _is_redundant_transcript_warning_line(
            line,
            has_problem_location=has_problem_location,
        )
    ]


_TRANSCRIPT_SEGMENT_HEADING_PATTERN = re.compile(
    r"(?m)^(?:#{1,6}\s*)?(?:"
    r"【第\s*(?P<zh_index>\d+)\s*段\s*[｜|]\s*"
    r"(?P<zh_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<zh_end>\d{1,3}:[0-5]\d|end)】"
    r"|\[?Segment\s+(?P<en_index>\d+)(?:/\d+)?\s*[|｜]\s*"
    r"(?P<en_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<en_end>\d{1,3}:[0-5]\d|end)\]?)\s*$",
    flags=re.IGNORECASE,
)
_TRANSCRIPT_TIMESTAMP_PATTERN = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]")


def _timestamp_segment_indices(transcript: str, segment_seconds: int = 600) -> set[int]:
    indices: set[int] = set()
    for match in _TRANSCRIPT_TIMESTAMP_PATTERN.finditer(transcript or ""):
        seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        indices.add(max(0, seconds // segment_seconds))
    return indices


def _markdown_transcript_segment_indices_from_text(text: str) -> set[int]:
    indices: set[int] = set()
    for match in _TRANSCRIPT_SEGMENT_HEADING_PATTERN.finditer(text):
        raw_index = match.group("zh_index") or match.group("en_index")
        try:
            index = int(raw_index) - 1
        except (TypeError, ValueError):
            continue
        if index >= 0:
            indices.add(index)
    if indices:
        return indices
    transcript = _markdown_section(text, ("完整逐字稿", "Verbatim Transcript"), ())
    return _timestamp_segment_indices(transcript)


def _markdown_transcript_segment_indices(output_path: str) -> set[int]:
    path = Path(output_path or "")
    if not path.is_file():
        return set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return _markdown_transcript_segment_indices_from_text(text)


def _repeated_transcript_turn_review_segments(
    transcript: str,
    *,
    limit: int = 3,
    segment_seconds: int = 600,
    segments: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]\s*"
        r"(?:\*\*\[[^\]]+\]\*\*|\[[^\]]+\])?\s*[：:]?\s*(?P<text>.+)"
    )
    runs: list[dict[str, Any]] = []
    current_text = ""
    current_preview = ""
    current_run = 0
    current_timestamps: list[int] = []
    current_block_segments: list[dict[str, int]] = []
    current_heading_segment: Optional[dict[str, int]] = None

    def flush_current() -> None:
        if current_run <= limit or not current_timestamps:
            return
        runs.append({
            "text": current_preview,
            "run": current_run,
            "timestamps": list(current_timestamps),
            "block_segments": list(current_block_segments),
        })

    def preview_text(value: str, max_chars: int = 24) -> str:
        cleaned = _normalize_repeated_phrase_preview(value)
        return cleaned[:max_chars]

    def heading_segment(line: str) -> Optional[dict[str, int]]:
        match = _TRANSCRIPT_SEGMENT_HEADING_PATTERN.match(str(line or "").strip())
        if not match:
            return None
        raw_index = match.group("zh_index") or match.group("en_index")
        raw_start = match.group("zh_start") or match.group("en_start")
        raw_end = match.group("zh_end") or match.group("en_end")
        try:
            index = int(raw_index) - 1
        except (TypeError, ValueError):
            return None
        start_seconds = _clock_text_to_seconds(raw_start)
        end_seconds = _clock_text_to_seconds(raw_end)
        if index < 0 or start_seconds is None:
            return None
        if end_seconds is None or end_seconds <= start_seconds:
            end_seconds = start_seconds + 1
        return {
            "index": index,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
        }

    for line in (transcript or "").splitlines():
        segment_heading = heading_segment(line)
        if segment_heading is not None:
            current_heading_segment = segment_heading
            continue
        match = pattern.search(line)
        if not match:
            continue
        raw_text = match.group("text")
        normalized = _normalize_turn_text(match.group("text"))
        timestamp = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        if len(normalized) < 8:
            flush_current()
            current_text = ""
            current_preview = ""
            current_run = 0
            current_timestamps = []
            current_block_segments = []
            continue
        if normalized == current_text:
            current_run += 1
            current_timestamps.append(timestamp)
            if current_heading_segment is not None:
                current_block_segments.append(dict(current_heading_segment))
        else:
            flush_current()
            current_text = normalized
            current_preview = preview_text(raw_text)
            current_run = 1
            current_timestamps = [timestamp]
            current_block_segments = [dict(current_heading_segment)] if current_heading_segment is not None else []
    flush_current()

    if not runs:
        return []

    def fallback_matches(start_time: int, end_time: int) -> list[dict[str, int]]:
        first_index = max(0, start_time // segment_seconds)
        last_index = max(first_index, end_time // segment_seconds)
        return [
            {
                "index": index,
                "start_seconds": index * segment_seconds,
                "end_seconds": (index + 1) * segment_seconds,
            }
            for index in range(first_index, last_index + 1)
        ]

    def segment_metadata_by_index(index: int, fallback: dict[str, int]) -> dict[str, int]:
        for position, segment in enumerate(segments or []):
            try:
                segment_index = int(segment.get("index", position))
            except (TypeError, ValueError, AttributeError):
                continue
            if segment_index != index:
                continue
            try:
                start_seconds = int(segment.get("start_seconds", fallback.get("start_seconds", 0)))
                end_seconds = int(segment.get("end_seconds", fallback.get("end_seconds", start_seconds + 1)))
            except (TypeError, ValueError, AttributeError):
                start_seconds = int(fallback.get("start_seconds", index * segment_seconds))
                end_seconds = int(fallback.get("end_seconds", start_seconds + segment_seconds))
            if end_seconds <= start_seconds:
                end_seconds = start_seconds + 1
            return {
                "index": index,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            }
        return {
            "index": index,
            "start_seconds": int(fallback.get("start_seconds", index * segment_seconds)),
            "end_seconds": int(fallback.get("end_seconds", (index + 1) * segment_seconds)),
        }

    def primary_block_matches(block_segments: list[dict[str, int]]) -> list[dict[str, int]]:
        if not block_segments:
            return []
        counts: dict[int, int] = {}
        fallback_by_index: dict[int, dict[str, int]] = {}
        for block_segment in block_segments:
            try:
                index = int(block_segment.get("index", -1))
            except (TypeError, ValueError, AttributeError):
                continue
            if index < 0:
                continue
            counts[index] = counts.get(index, 0) + 1
            fallback_by_index.setdefault(index, block_segment)
        if not counts:
            return []
        best_count = max(counts.values())
        return [
            segment_metadata_by_index(index, fallback_by_index[index])
            for index in sorted(counts)
            if counts[index] == best_count
        ]

    def segment_matches(timestamps: list[int], block_segments: list[dict[str, int]]) -> list[dict[str, int]]:
        if not timestamps:
            return []
        from_blocks = primary_block_matches(block_segments)
        if from_blocks:
            return from_blocks
        start_time = min(timestamps)
        end_time = max(timestamps)
        matched: list[tuple[int, dict[str, int]]] = []
        seen_indices: set[int] = set()
        for position, segment in enumerate(segments or []):
            try:
                index = int(segment.get("index", position))
                start_seconds = int(segment.get("start_seconds", 0))
                end_seconds = int(segment.get("end_seconds", start_seconds + 1))
            except (TypeError, ValueError, AttributeError):
                continue
            if end_seconds <= start_seconds:
                end_seconds = start_seconds + 1
            hit_count = sum(1 for timestamp in timestamps if start_seconds <= timestamp < end_seconds)
            overlaps = (
                hit_count > 0
                or (start_seconds <= start_time < end_seconds)
                or (start_seconds < end_time < end_seconds)
            )
            if overlaps and index not in seen_indices:
                matched.append((
                    hit_count,
                    {
                        "index": index,
                        "start_seconds": start_seconds,
                        "end_seconds": end_seconds,
                    },
                ))
                seen_indices.add(index)
        if not matched:
            return fallback_matches(start_time, end_time)
        best_hit_count = max(hit_count for hit_count, _ in matched)
        if best_hit_count <= 0:
            return [match for _, match in matched]
        return [match for hit_count, match in matched if hit_count == best_hit_count]

    details_by_index: dict[int, dict[str, Any]] = {}
    for run in runs:
        timestamps = run.get("timestamps") or []
        if not timestamps:
            continue
        start_time = min(timestamps)
        end_time = max(timestamps)
        run_length = int(run.get("run") or 0)
        preview = str(run.get("text") or "").strip()
        preview_part = f"：{preview}" if preview else ""
        issue = (
            f"疑似連續重複轉錄；同一句連續重複 {run_length} 次"
            f"{preview_part}；重複時間：{_format_clock(start_time)}-{_format_clock(end_time)}"
        )
        for match in segment_matches(timestamps, run.get("block_segments") or []):
            index = int(match["index"])
            detail = details_by_index.setdefault(
                index,
                {
                    "index": index,
                    "label": review_segment_label(index),
                    "start_seconds": int(match["start_seconds"]),
                    "end_seconds": int(match["end_seconds"]),
                    "issues": [],
                },
            )
            detail["start_seconds"] = int(match["start_seconds"])
            detail["end_seconds"] = int(match["end_seconds"])
            if issue not in detail["issues"]:
                detail["issues"].append(issue)
    return [
        details_by_index[index]
        for index in sorted(details_by_index)
    ]


def _clock_text_to_seconds(value: Any) -> Optional[int]:
    match = re.fullmatch(r"\s*(\d{1,3}):([0-5]\d)\s*", str(value or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _transcript_segment_spans(transcript: str) -> list[dict[str, int]]:
    spans: list[dict[str, int]] = []
    for match in _TRANSCRIPT_SEGMENT_HEADING_PATTERN.finditer(transcript or ""):
        raw_index = match.group("zh_index") or match.group("en_index")
        raw_start = match.group("zh_start") or match.group("en_start")
        raw_end = match.group("zh_end") or match.group("en_end")
        try:
            index = int(raw_index) - 1
        except (TypeError, ValueError):
            continue
        start_seconds = _clock_text_to_seconds(raw_start)
        end_seconds = _clock_text_to_seconds(raw_end)
        if index < 0 or start_seconds is None:
            continue
        if end_seconds is None or end_seconds <= start_seconds:
            end_seconds = start_seconds + 1
        spans.append({
            "index": index,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
        })
    if not spans:
        for index in sorted(_timestamp_segment_indices(transcript)):
            spans.append({
                "index": index,
                "start_seconds": index * 600,
                "end_seconds": (index + 1) * 600,
            })
    return spans


def _repeated_phrase_from_warning(warning: str) -> Optional[dict[str, Any]]:
    match = _extract_repeated_phrase_clause(warning)
    if not match:
        return None
    phrase = str(match.group("phrase") or "").strip()
    phrase = phrase.strip("：:，,。．.、；;！!？?()（）[]【】「」『』")
    normalized = _normalize_turn_text(phrase)
    if len(normalized) < 4:
        return None
    return {
        "count": int(match.group("count")),
        "phrase": phrase,
        "normalized": normalized,
    }


def _transcript_lookup_line_text(line: str) -> str:
    cleaned = re.sub(r"\[\d{1,3}:[0-5]\d\]", "", str(line or ""))
    cleaned = re.sub(r"\*\*\[[^\]]+\]\*\*", "", cleaned)
    cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
    cleaned = re.sub(r"^[：:,\s]+", "", cleaned)
    return _normalize_turn_text(cleaned)


def _heading_segment_from_line(line: str) -> Optional[dict[str, int]]:
    match = _TRANSCRIPT_SEGMENT_HEADING_PATTERN.match(str(line or "").strip())
    if not match:
        return None
    raw_index = match.group("zh_index") or match.group("en_index")
    raw_start = match.group("zh_start") or match.group("en_start")
    raw_end = match.group("zh_end") or match.group("en_end")
    try:
        index = int(raw_index) - 1
    except (TypeError, ValueError):
        return None
    start_seconds = _clock_text_to_seconds(raw_start)
    end_seconds = _clock_text_to_seconds(raw_end)
    if index < 0 or start_seconds is None:
        return None
    if end_seconds is None or end_seconds <= start_seconds:
        end_seconds = start_seconds + 1
    return {
        "index": index,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
    }


def _warning_phrase_review_segments_from_transcript(
    transcript: str,
    warnings: list[str],
    *,
    segment_seconds: int = 600,
) -> list[dict[str, Any]]:
    phrase_infos = [
        phrase_info
        for warning in warnings
        if (phrase_info := _repeated_phrase_from_warning(warning))
    ]
    if not phrase_infos:
        return []

    details_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    current_segment: Optional[dict[str, int]] = None
    for raw_line in (transcript or "").splitlines():
        heading_segment = _heading_segment_from_line(raw_line)
        if heading_segment is not None:
            current_segment = heading_segment
            continue
        timestamp_match = _TRANSCRIPT_TIMESTAMP_PATTERN.search(raw_line)
        if not timestamp_match and current_segment is None:
            continue
        line_seconds = None
        if timestamp_match:
            line_seconds = (
                int(timestamp_match.group("minutes")) * 60
                + int(timestamp_match.group("seconds"))
            )
        line_text = _transcript_lookup_line_text(raw_line)
        if not line_text:
            continue
        for phrase_info in phrase_infos:
            phrase = str(phrase_info["normalized"])
            if phrase not in line_text and line_text not in phrase:
                continue
            if line_seconds is not None:
                fallback_index = max(0, line_seconds // segment_seconds)
                segment = current_segment or {
                    "index": fallback_index,
                    "start_seconds": fallback_index * segment_seconds,
                    "end_seconds": (fallback_index + 1) * segment_seconds,
                }
            else:
                segment = current_segment
            index = int(segment["index"])
            key = (index, phrase)
            detail = details_by_key.setdefault(
                key,
                {
                    "index": index,
                    "label": review_segment_label(index),
                    "start_seconds": int(segment["start_seconds"]),
                    "end_seconds": int(segment["end_seconds"]),
                    "first_match_seconds": line_seconds,
                    "last_match_seconds": line_seconds,
                    "phrase_info": phrase_info,
                },
            )
            if line_seconds is not None:
                first_match = detail.get("first_match_seconds")
                last_match = detail.get("last_match_seconds")
                detail["first_match_seconds"] = (
                    line_seconds if first_match is None else min(int(first_match), line_seconds)
                )
                detail["last_match_seconds"] = (
                    line_seconds if last_match is None else max(int(last_match), line_seconds)
                )

    details_by_index: dict[int, dict[str, Any]] = {}
    for detail in details_by_key.values():
        phrase_info = detail.pop("phrase_info")
        start_time = detail.pop("first_match_seconds", None)
        end_time = detail.pop("last_match_seconds", None)
        issue = (
            "疑似連續重複轉錄；"
            f"同一句連續重複 {phrase_info['count']} 次：{phrase_info['phrase']}"
        )
        if start_time is not None and end_time is not None:
            issue += f"；重複時間：{_format_clock(int(start_time))}-{_format_clock(int(end_time))}"
        index = int(detail["index"])
        merged = details_by_index.setdefault(
            index,
            {
                "index": index,
                "label": review_segment_label(index),
                "start_seconds": int(detail["start_seconds"]),
                "end_seconds": int(detail["end_seconds"]),
                "issues": [],
            },
        )
        if issue not in merged["issues"]:
            merged["issues"].append(issue)
    return [
        details_by_index[index]
        for index in sorted(details_by_index)
    ]


def _markdown_warning_phrase_review_segments_from_text(
    text: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    transcript = _markdown_section(text, ("完整逐字稿", "Verbatim Transcript"), ())
    return _warning_phrase_review_segments_from_transcript(transcript, warnings)


def _legacy_markdown_warning_phrase_review_segments(
    output_path: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    path = Path(output_path or "")
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _markdown_warning_phrase_review_segments_from_text(text, warnings)


def _markdown_repeated_turn_review_segments_from_text(text: str) -> list[dict[str, Any]]:
    transcript = _markdown_section(text, ("完整逐字稿", "Verbatim Transcript"), ())
    segments = _transcript_segment_spans(transcript)
    return _repeated_transcript_turn_review_segments(transcript, segments=segments)


def _legacy_markdown_repeated_turn_review_segments(output_path: str) -> list[dict[str, Any]]:
    path = Path(output_path or "")
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _markdown_repeated_turn_review_segments_from_text(text)


LEGACY_TRANSCRIPT_OMISSION_PATTERNS = (
    r"為節省篇幅",
    r"省略[^。\n]{0,20}逐字稿",
    r"逐字稿[^。\n]{0,20}省略",
    r"已過濾[^。\n]{0,20}逐字稿",
    r"自動過濾後續重複內容",
)
VIDEO_RECORDING_PROFILES = {"video_balanced"}
AUDIO_RECORDING_PROFILES = {"audio_standard", "audio_compact"}
VIDEO_SOURCE_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mpeg", ".mpg", ".wmv"}
AUDIO_SOURCE_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _source_media_type_from_metadata(record: dict[str, Any], quality_report: Any) -> Optional[str]:
    if isinstance(quality_report, dict):
        recording = quality_report.get("recording") or {}
        if isinstance(recording, dict):
            profile = str(recording.get("profile") or "").strip()
            if profile in VIDEO_RECORDING_PROFILES:
                return "video"
            if profile in AUDIO_RECORDING_PROFILES:
                return "audio"

    suffix = Path(str(record.get("source_audio") or "")).suffix.lower()
    if suffix in VIDEO_SOURCE_EXTENSIONS:
        return "video"
    if suffix in AUDIO_SOURCE_EXTENSIONS:
        return "audio"
    return None


def _source_media_recording_metadata(quality_report: Any) -> dict[str, Any]:
    recording = quality_report.get("recording") if isinstance(quality_report, dict) else {}
    if not isinstance(recording, dict):
        recording = {}

    profile = str(recording.get("profile") or "").strip() or None
    sha256 = str(recording.get("source_audio_sha256") or "").strip() or None
    if sha256 == "unavailable":
        sha256 = None
    raw_size = recording.get("source_audio_size_bytes")
    try:
        size_bytes = int(raw_size) if raw_size not in (None, "") else None
    except (TypeError, ValueError):
        size_bytes = None

    return {
        "recording_profile": profile,
        "source_media_size_bytes": size_bytes,
        "source_media_sha256": sha256,
    }


def _legacy_markdown_quality_warnings(output_path: str) -> list[str]:
    path = Path(output_path or "")
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    summary = _markdown_section(text, ("討論摘要", "Discussion Summary"), ("最終決議", "Final Decisions"))
    decisions = _markdown_section(text, ("最終決議", "Final Decisions"), ("待辦事項", "Action Items"))
    actions = _markdown_section(text, ("待辦事項", "Action Items"), ("完整逐字稿", "Verbatim Transcript"))
    transcript = _markdown_section(text, ("完整逐字稿", "Verbatim Transcript"), ())

    warnings: list[str] = []
    summary_ids = _markdown_ids(summary, "D")
    decision_ids = _markdown_ids(decisions, "R")
    action_ids = _markdown_ids(actions, "A")
    decision_discussion_refs = _markdown_ids(decisions, "D")
    action_discussion_refs = _markdown_ids(actions, "D")
    action_decision_refs = _markdown_ids(actions, "R")

    if summary.strip() and not summary_ids:
        warnings.append("摘要品質警示：討論摘要未使用 D 編號，較難與決議及待辦事項串聯")
    if decisions.strip() and not decision_ids:
        warnings.append("摘要品質警示：最終決議未使用 R 編號，較難被待辦事項引用")
    if actions.strip() and not action_ids:
        warnings.append("摘要品質警示：待辦事項未使用 A 編號，後續追蹤較不清楚")

    missing_d_refs = sorted((decision_discussion_refs | action_discussion_refs) - summary_ids)
    if missing_d_refs:
        warnings.append(f"摘要品質警示：決議或待辦引用不存在的討論編號：{', '.join(missing_d_refs)}")

    missing_r_refs = sorted(action_decision_refs - decision_ids)
    if missing_r_refs:
        warnings.append(f"摘要品質警示：待辦事項引用不存在的決議編號：{', '.join(missing_r_refs)}")

    if transcript and any(re.search(pattern, transcript, flags=re.IGNORECASE) for pattern in LEGACY_TRANSCRIPT_OMISSION_PATTERNS):
        warnings.append("逐字稿品質警示：舊紀錄逐字稿疑似省略或自動過濾內容")
    repeated_review_segments = _repeated_transcript_turn_review_segments(
        transcript,
        segments=_transcript_segment_spans(transcript),
    )
    if repeated_review_segments:
        review_summary = _quality_review_segment_summary(repeated_review_segments, limit=5)
        if review_summary:
            warnings.append(
                "逐字稿品質警示：疑似連續重複轉錄；"
                f"問題位置：{review_summary}。"
                "建議重跑上述分段或複核相關內容。"
            )
        else:
            warnings.append("逐字稿品質警示：疑似連續重複轉錄")

    return list(dict.fromkeys(warnings))


def _legacy_markdown_quality_warning_count(output_path: str) -> int:
    return len(_legacy_markdown_quality_warnings(output_path))


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
        raise ValueError("找不到可重試的原始媒體檔，請重新上傳。")
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
                message or "媒體檔已接收，等待處理...",
                task_type,
                source,
                _serialize_payload(payload),
                max_attempts,
                now,
                now,
            )
        )
        _record_job_event(conn, job_id, "created", message or "媒體檔已接收，等待處理...")
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


def apply_quality_preview_fields(
    record: dict[str, Any],
    *,
    quality_report: Optional[dict[str, Any]] = None,
    quality_report_json: Optional[str] = None,
) -> dict[str, Any]:
    """Populate derived quality fields used by list, search, and detail APIs."""
    record = dict(record)
    if quality_report_json is None and "quality_report_json" in record:
        quality_report_json = record.pop("quality_report_json", None)
    else:
        record.pop("quality_report_json", None)
    if quality_report is None and isinstance(record.get("quality_report"), dict):
        quality_report = record.get("quality_report")
    warning_count = 0
    warning_preview = None
    quality_warning_text = ""
    review_segment_labels: list[str] = []
    review_segment_detail_by_label: dict[str, dict[str, Any]] = {}
    known_segment_indices: set[int] = set()
    segment_issue_previews: list[str] = []
    review_segment_issue_previews: list[str] = []
    quality_warning_lines_for_lookup: list[str] = []
    generic_review_issue = "品質警示提及此分段"

    def has_structured_review_issues(report: Any) -> bool:
        if not isinstance(report, dict):
            return False
        for key in ("review_segments", "segments"):
            for segment in report.get(key) or []:
                if not isinstance(segment, dict):
                    continue
                if any(str(issue or "").strip() for issue in segment.get("issues") or []):
                    return True
        return False

    def clean_review_issues(issues: list[str]) -> list[str]:
        unique_issues = list(dict.fromkeys(issue for issue in issues if issue))
        if any(issue != generic_review_issue for issue in unique_issues):
            unique_issues = [issue for issue in unique_issues if issue != generic_review_issue]
        repeated_hallucination_preview = "分段疑似重複轉錄幻覺"
        if repeated_hallucination_preview in unique_issues and any(
            issue != repeated_hallucination_preview
            and repeated_hallucination_preview in issue
            for issue in unique_issues
        ):
            unique_issues = [
                issue
                for issue in unique_issues
                if issue != repeated_hallucination_preview
            ]
        return unique_issues

    def add_review_segment_label(
        label: str,
        *,
        index: Optional[int] = None,
        start_seconds: Optional[int] = None,
        end_seconds: Optional[int] = None,
        issues: Optional[list[str]] = None,
    ) -> None:
        cleaned = str(label or "").strip()
        if not cleaned:
            return
        if cleaned not in review_segment_labels:
            review_segment_labels.append(cleaned)
        detail = review_segment_detail_by_label.setdefault(cleaned, {"label": cleaned})
        if index is not None:
            detail["index"] = index
        if start_seconds is not None:
            detail["start_seconds"] = start_seconds
        if end_seconds is not None:
            detail["end_seconds"] = end_seconds
        clean_issues = [
            _normalize_quality_review_issue_text(str(issue).strip())
            for issue in issues or []
            if _normalize_quality_review_issue_text(str(issue).strip())
        ]
        if clean_issues:
            existing_issues = [
                _normalize_quality_review_issue_text(str(issue).strip())
                for issue in detail.get("issues") or []
                if _normalize_quality_review_issue_text(str(issue).strip())
            ]
            merged_issues: list[str] = []
            seen_issue_keys: set[str] = set()
            for issue in [*existing_issues, *clean_issues]:
                key = _quality_review_issue_dedupe_key(issue)
                if key in seen_issue_keys:
                    continue
                merged_issues.append(issue)
                seen_issue_keys.add(key)
            detail["issues"] = clean_review_issues(merged_issues)

    def add_review_segment_labels_from_text(text: str) -> None:
        for detail in review_segment_details_from_text(text):
            detail_issues = [
                _normalize_quality_review_issue_text(str(issue).strip())
                for issue in detail.get("issues") or []
                if _normalize_quality_review_issue_text(str(issue).strip())
            ] or [generic_review_issue]
            add_review_segment_label(
                detail.get("label") or review_segment_label(int(detail["index"])),
                index=int(detail["index"]),
                start_seconds=int_or_none(detail.get("start_seconds")),
                end_seconds=int_or_none(detail.get("end_seconds")),
                issues=detail_issues,
            )

    def int_or_none(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if quality_report is None:
        try:
            quality_report = json.loads(quality_report_json) if quality_report_json else None
        except (TypeError, json.JSONDecodeError):
            quality_report = None
    record["source_media_type"] = _source_media_type_from_metadata(record, quality_report)
    record.update(_source_media_recording_metadata(quality_report))
    if isinstance(quality_report, dict):
        skip_derived_location_warning_segments = has_structured_review_issues(quality_report)
        warnings = quality_report.get("warnings") or []
        if isinstance(warnings, list):
            warning_count = len(warnings)
            quality_warning_text = "\n".join(str(warning) for warning in warnings)
            if warnings:
                warning_preview = str(warnings[0]).strip() or None
            for warning in warnings:
                warning_text = str(warning)
                quality_warning_lines_for_lookup.append(warning_text)
                if (
                    skip_derived_location_warning_segments
                    and _is_derived_review_location_warning(warning_text)
                ):
                    continue
                add_review_segment_labels_from_text(warning_text)
        for review_segment in quality_report.get("review_segments") or []:
            if not isinstance(review_segment, dict):
                continue
            label = str(review_segment.get("label") or "").strip()
            segment_index = int_or_none(review_segment.get("index"))
            if not label:
                label = review_segment_label(segment_index) if segment_index is not None else "分段"
            segment_issues = [
                _normalize_quality_review_issue_text(str(issue).strip())
                for issue in review_segment.get("issues") or []
                if _normalize_quality_review_issue_text(str(issue).strip())
            ]
            for issue_text in segment_issues:
                review_segment_issue_previews.append(f"{label}：{issue_text}")
            add_review_segment_label(
                label,
                index=segment_index,
                start_seconds=int_or_none(review_segment.get("start_seconds")),
                end_seconds=int_or_none(review_segment.get("end_seconds")),
                issues=segment_issues,
            )
        for segment in quality_report.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            segment_index = int_or_none(segment.get("index"))
            if segment_index is not None and segment_index >= 0:
                known_segment_indices.add(segment_index)
            segment_label = review_segment_label(segment_index) if segment_index is not None else "分段"
            for issue in segment.get("issues") or []:
                issue_text = _normalize_quality_review_issue_text(str(issue).strip())
                if issue_text:
                    segment_issue_previews.append(f"{segment_label}：{issue_text}")
                    add_review_segment_label(
                        segment_label,
                        index=segment_index,
                        start_seconds=int_or_none(segment.get("start_seconds")),
                        end_seconds=int_or_none(segment.get("end_seconds")),
                        issues=[issue_text],
                    )
        if segment_issue_previews:
            warning_count += len(segment_issue_previews)
            if not warning_preview:
                warning_preview = segment_issue_previews[0]
    if not warning_count and record.get("quality_score") is None and not record.get("quality_label"):
        legacy_warnings = _legacy_markdown_quality_warnings(str(record.get("output_path") or ""))
        warning_count = len(legacy_warnings)
        quality_warning_text = "\n".join(legacy_warnings)
        if legacy_warnings:
            warning_preview = legacy_warnings[0]
            for warning in legacy_warnings:
                quality_warning_lines_for_lookup.append(warning)
                add_review_segment_labels_from_text(warning)
    output_path_text = str(record.get("output_path") or "")
    legacy_repeated_details = _legacy_markdown_repeated_turn_review_segments(output_path_text)
    full_content_text = str(record.get("full_content") or "")
    if not legacy_repeated_details and full_content_text.strip():
        legacy_repeated_details = _markdown_repeated_turn_review_segments_from_text(full_content_text)
    phrase_review_details: list[dict[str, Any]] = []
    if not review_segment_labels and not legacy_repeated_details:
        phrase_review_details = _legacy_markdown_warning_phrase_review_segments(
            output_path_text,
            quality_warning_lines_for_lookup,
        )
        if not phrase_review_details and full_content_text.strip():
            phrase_review_details = _markdown_warning_phrase_review_segments_from_text(
                full_content_text,
                quality_warning_lines_for_lookup,
            )
    if phrase_review_details:
        known_segment_indices.update(_markdown_transcript_segment_indices(output_path_text))
        if not known_segment_indices and full_content_text.strip():
            known_segment_indices.update(_markdown_transcript_segment_indices_from_text(full_content_text))
    if legacy_repeated_details:
        known_segment_indices.update(_markdown_transcript_segment_indices(output_path_text))
        if not known_segment_indices and full_content_text.strip():
            known_segment_indices.update(_markdown_transcript_segment_indices_from_text(full_content_text))
    for detail in [*legacy_repeated_details, *phrase_review_details]:
        segment_issues = [
            _normalize_quality_review_issue_text(str(issue).strip())
            for issue in detail.get("issues") or []
            if _normalize_quality_review_issue_text(str(issue).strip())
        ]
        add_review_segment_label(
            detail.get("label") or review_segment_label(int(detail["index"])),
            index=int_or_none(detail.get("index")),
            start_seconds=int_or_none(detail.get("start_seconds")),
            end_seconds=int_or_none(detail.get("end_seconds")),
            issues=segment_issues,
        )
        if segment_issues and not warning_count:
            warning_count = 1
        if not warning_preview and segment_issues:
            warning_preview = f"{detail.get('label') or review_segment_label(int(detail['index']))}：{segment_issues[0]}"
    if not warning_preview:
        try:
            quality_score = int(record["quality_score"]) if record.get("quality_score") is not None else None
        except (TypeError, ValueError):
            quality_score = None
        if quality_score is not None and quality_score < 85:
            warning_preview = f"品質分數 {quality_score} 低於 85，建議複核"
    if not warning_preview:
        label = str(record.get("quality_label") or "").strip()
        if label and any(token in label for token in ("需", "警", "低", "不")):
            warning_preview = f"品質標籤：{label}"
    if known_segment_indices:
        review_segment_labels = [
            label
            for label in review_segment_labels
            if not isinstance(review_segment_detail_by_label.get(label, {}).get("index"), int)
            or review_segment_detail_by_label[label]["index"] in known_segment_indices
        ]
    sorted_review_segment_labels = sorted(review_segment_labels, key=review_segment_label_sort_key)
    record["quality_review_segments"] = sorted_review_segment_labels
    record["quality_review_segment_details"] = [
        review_segment_detail_by_label[label]
        for label in sorted_review_segment_labels
        if label in review_segment_detail_by_label
    ]
    record["quality_review_segment_summary"] = _quality_review_segment_summary(
        record["quality_review_segment_details"]
    )
    review_summary = str(record.get("quality_review_segment_summary") or "").strip()
    if review_summary and not warning_preview:
        warning_preview = f"逐字稿品質警示：問題位置：{review_summary}"
    elif review_summary and _has_standard_review_location_warning(str(warning_preview or "")):
        warning_preview = f"逐字稿品質警示：問題位置：{review_summary}"
    elif (
        review_summary
        and record["quality_review_segment_details"]
        and str(warning_preview or "").strip().startswith("逐字稿品質警示")
    ):
        warning_preview = f"逐字稿品質警示：問題位置：{review_summary}"
    elif _should_promote_review_summary_to_preview(warning_preview, review_summary):
        warning_preview = f"逐字稿品質警示：問題位置：{review_summary}"
    record["quality_warning_preview"] = warning_preview
    warning_text_items = [
        line
        for item in [quality_warning_text]
        for line in _quality_warning_lines(str(item or ""))
    ]
    combined_warning_text = "\n".join(warning_text_items)
    if review_summary and not _has_standard_review_location_warning(combined_warning_text):
        warning_text_items.insert(0, _review_summary_location_warning(review_summary))
    _append_uncovered_quality_previews(
        warning_text_items,
        "\n".join(warning_text_items),
        [*review_segment_issue_previews, *segment_issue_previews],
    )
    if not warning_text_items and warning_preview:
        warning_text_items.append(str(warning_preview).strip())
    warning_text_items = _compact_quality_warning_lines(warning_text_items)
    record["quality_warning_count"] = len(warning_text_items) or warning_count
    record["quality_warning_text"] = "\n".join(warning_text_items) or None
    record["quality_review_segment_count"] = len(sorted_review_segment_labels)
    if not known_segment_indices and record["quality_review_segment_details"]:
        known_segment_indices = _markdown_transcript_segment_indices(str(record.get("output_path") or ""))
        if not known_segment_indices and full_content_text.strip():
            known_segment_indices = _markdown_transcript_segment_indices_from_text(full_content_text)
    record["quality_review_rerunnable_segments"] = [
        detail["index"]
        for detail in record["quality_review_segment_details"]
        if isinstance(detail.get("index"), int) and detail["index"] in known_segment_indices
    ]
    _apply_effective_quality_status(record)
    return record


def _meeting_row_with_quality_preview(row: sqlite3.Row) -> dict[str, Any]:
    return apply_quality_preview_fields(dict(row))


def _meeting_record_needs_review(record: dict[str, Any]) -> bool:
    warning_count = int(record.get("quality_warning_count") or 0)
    if warning_count > 0:
        return True

    review_segment_count = int(record.get("quality_review_segment_count") or 0)
    if review_segment_count > 0:
        return True

    score = record.get("quality_score")
    if score is not None:
        try:
            if int(score) < 85:
                return True
        except (TypeError, ValueError):
            pass

    label = str(record.get("quality_label") or "").strip()
    return any(token in label for token in ("需", "警", "低", "不"))


QUALITY_FILTER_TYPES = {"summary", "recording", "transcript", "rerunnable", "other"}


def _normalize_quality_filter_type(quality_type: Optional[str] = None) -> str:
    normalized = str(quality_type or "all").strip().lower()
    return normalized if normalized in QUALITY_FILTER_TYPES else "all"


def _is_recording_quality_warning(warning: str) -> bool:
    return bool(re.search(r"錄音|錄影|音檔|媒體|音量|爆音|靜音|聲道|取樣率|預覽畫面|黑畫面", str(warning or "")))


def _is_transcript_quality_warning(warning: str) -> bool:
    return bool(re.search(r"(^|\n)\s*(?:逐字稿品質警示|需複核分段)\s*[：:]", str(warning or "")))


def _meeting_record_quality_types(record: dict[str, Any]) -> set[str]:
    warning_preview = str(record.get("quality_warning_preview") or "").strip()
    warning_text = "\n".join(
        str(value).strip()
        for value in (record.get("quality_warning_text"), warning_preview)
        if str(value or "").strip()
    )
    warning_count = int(record.get("quality_warning_count") or 0)
    review_segment_count = int(record.get("quality_review_segment_count") or 0)
    review_segment_details = record.get("quality_review_segment_details") or []
    rerunnable_segments = record.get("quality_review_rerunnable_segments") or []
    types: set[str] = set()

    if "摘要品質警示" in warning_text:
        types.add("summary")
    if _is_recording_quality_warning(warning_text):
        types.add("recording")
    if (
        _is_transcript_quality_warning(warning_text)
        or review_segment_count > 0
        or bool(review_segment_details)
        or bool(rerunnable_segments)
    ):
        types.add("transcript")
    if rerunnable_segments:
        types.add("rerunnable")

    score = record.get("quality_score")
    has_score = score is not None and score != ""
    is_other_score = False
    if has_score:
        try:
            is_other_score = int(score) < 85
        except (TypeError, ValueError):
            is_other_score = False
    label = str(record.get("quality_label") or "").strip()
    is_other_label = any(token in label for token in ("需", "警", "低", "不")) and not types
    is_other_warning = warning_count > 0 and not types
    if is_other_warning or is_other_score or is_other_label:
        types.add("other")

    return types


def _int_quality_score(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _capped_effective_quality_status(
    record: dict[str, Any],
    *,
    cap: int,
    label: str,
) -> None:
    original_score = _int_quality_score(record.get("quality_score"))
    record["quality_effective_score"] = min(original_score, cap) if original_score is not None else cap
    record["quality_effective_label"] = label


def _apply_effective_quality_status(record: dict[str, Any]) -> None:
    """Expose a display-oriented quality state without overwriting raw scores."""
    quality_types = _meeting_record_quality_types(record)
    if "rerunnable" in quality_types:
        _capped_effective_quality_status(record, cap=80, label="需重跑問題分段")
        return
    if "transcript" in quality_types:
        _capped_effective_quality_status(record, cap=82, label="需複核逐字稿")
        return
    if "summary" in quality_types:
        _capped_effective_quality_status(record, cap=84, label="需重整摘要")
        return
    if "recording" in quality_types:
        _capped_effective_quality_status(record, cap=88, label="需抽查原始檔")
        return
    if "other" in quality_types:
        label = str(record.get("quality_label") or "需複核").strip() or "需複核"
        _capped_effective_quality_status(record, cap=84, label=label)
        return
    record["quality_effective_score"] = _int_quality_score(record.get("quality_score"))
    record["quality_effective_label"] = str(record.get("quality_label") or "").strip() or None


def _meeting_record_matches_quality_type(record: dict[str, Any], quality_type: Optional[str] = None) -> bool:
    normalized = _normalize_quality_filter_type(quality_type)
    if normalized == "all":
        return True
    return normalized in _meeting_record_quality_types(record)


def _meeting_record_matches_review_filters(
    record: dict[str, Any],
    *,
    needs_review: bool = False,
    quality_type: Optional[str] = None,
) -> bool:
    normalized = _normalize_quality_filter_type(quality_type)
    if needs_review or normalized != "all":
        if not _meeting_record_needs_review(record):
            return False
    return _meeting_record_matches_quality_type(record, normalized)


def list_meetings(
    limit: int = 50,
    offset: int = 0,
    needs_review: bool = False,
    quality_type: Optional[str] = None,
) -> list[dict]:
    """列出所有會議記錄（依建立時間倒序）"""
    limit = min(max(int(limit), 1), 500)
    offset = max(int(offset), 0)
    normalized_quality_type = _normalize_quality_filter_type(quality_type)
    with get_db() as conn:
        if needs_review or normalized_quality_type != "all":
            rows = conn.execute(
                """SELECT id, title, date, source_audio, output_path,
                          substr(summary, 1, 200) as summary_preview,
                          job_id, quality_score, quality_label,
                          quality_report_json,
                          created_at
                   FROM meetings
                   ORDER BY created_at DESC"""
            ).fetchall()
            records = [
                record
                for record in (_meeting_row_with_quality_preview(r) for r in rows)
                if _meeting_record_matches_review_filters(
                    record,
                    needs_review=needs_review,
                    quality_type=normalized_quality_type,
                )
            ]
            return records[offset:offset + limit]

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


def count_meetings(needs_review: bool = False, quality_type: Optional[str] = None) -> int:
    """統計會議記錄總數"""
    normalized_quality_type = _normalize_quality_filter_type(quality_type)
    with get_db() as conn:
        if needs_review or normalized_quality_type != "all":
            rows = conn.execute(
                """SELECT id, title, date, source_audio, output_path,
                          substr(summary, 1, 200) as summary_preview,
                          job_id, quality_score, quality_label,
                          quality_report_json,
                          created_at
                   FROM meetings"""
            ).fetchall()
            return sum(
                1
                for record in (_meeting_row_with_quality_preview(r) for r in rows)
                if _meeting_record_matches_review_filters(
                    record,
                    needs_review=needs_review,
                    quality_type=normalized_quality_type,
                )
            )
        return conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]


def list_meeting_source_audio_refs() -> list[dict]:
    """Return lightweight meeting references keyed by retained source media."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, title, source_audio, created_at, quality_report_json
               FROM meetings
               WHERE COALESCE(source_audio, '') <> ''
               ORDER BY created_at DESC, id DESC"""
        ).fetchall()
        refs = []
        for row in rows:
            record = dict(row)
            quality_report_json = record.pop("quality_report_json", None)
            try:
                record["quality_report"] = json.loads(quality_report_json) if quality_report_json else None
            except json.JSONDecodeError:
                record["quality_report"] = None
            refs.append(record)
        return refs


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
    normalized_role = normalize_role(role)

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
            (normalized_email, display_name, normalized_role, 1 if is_active else 0, now, now),
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


def search_meetings(
    query: str,
    limit: int = 50,
    needs_review: bool = False,
    quality_type: Optional[str] = None,
) -> list[dict]:
    """
    搜尋會議記錄的標題、媒體檔名、摘要、完整 Markdown 內容與品質報告。

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
    normalized_quality_type = _normalize_quality_filter_type(quality_type)
    query_limit = 500 if needs_review or normalized_quality_type != "all" else limit
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
                (fts_query, query_limit),
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
                (fts_query, query_limit),
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
                       OR COALESCE(m.quality_report_json, '') LIKE ? ESCAPE '\\'
                       OR COALESCE(c.content, '') LIKE ? ESCAPE '\\'
                    ORDER BY m.created_at DESC
                    LIMIT ?""",
                (pattern, pattern, pattern, pattern, pattern, pattern, query_limit),
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
                      OR COALESCE(quality_report_json, '') LIKE ? ESCAPE '\\'
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (pattern, pattern, pattern, pattern, pattern, query_limit),
            ).fetchall()
            add_rows(rows)

    records = list(records_by_id.values())
    if needs_review or normalized_quality_type != "all":
        records = [
            record
            for record in records
            if _meeting_record_matches_review_filters(
                record,
                needs_review=needs_review,
                quality_type=normalized_quality_type,
            )
        ]
    return records[:limit]


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
