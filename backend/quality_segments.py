"""Helpers for deriving transcript review segment labels from quality text."""

from __future__ import annotations

import re


REVIEW_SEGMENT_SECONDS = 600

_SEGMENT_TEXT_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*段"),
    re.compile(r"\bSegment\s*#?\s*(\d+)\b", flags=re.IGNORECASE),
)

_SEGMENT_DETAIL_PATTERN = re.compile(
    r"(?:第\s*(?P<chinese>\d+)\s*段|\bSegment\s*#?\s*(?P<english>\d+)\b)"
    r"(?P<trailing>[^,，、;；)\]\n]{0,40})",
    flags=re.IGNORECASE,
)
_TIME_RANGE_PATTERN = re.compile(
    r"(?P<start_minutes>\d{1,3}):(?P<start_seconds>[0-5]\d)"
    r"\s*(?:-|–|—|~|至|到)\s*"
    r"(?P<end_minutes>\d{1,3}):(?P<end_seconds>[0-5]\d)"
)
_WARNING_TIME_RANGE_PATTERN = re.compile(
    r"(?:重複時間|重複時段|問題時間|異常時間)[：:]\s*"
    r"(?P<start_minutes>\d{1,3}):(?P<start_seconds>[0-5]\d)"
    r"\s*(?:-|–|—|~|至|到)\s*"
    r"(?P<end_minutes>\d{1,3}):(?P<end_seconds>[0-5]\d)"
)


def _clock_seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


def _format_clock(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds or 0)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _warning_time_range_text(text: str) -> str:
    match = _WARNING_TIME_RANGE_PATTERN.search(str(text or ""))
    if not match:
        return ""
    start_seconds = _clock_seconds(match.group("start_minutes"), match.group("start_seconds"))
    end_seconds = _clock_seconds(match.group("end_minutes"), match.group("end_seconds"))
    return f"重複時間：{_format_clock(start_seconds)}-{_format_clock(end_seconds)}"


def _warning_issue_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    cleaned = re.sub(r"^(?:逐字稿|摘要)?品質警示[：:]\s*", "", cleaned)
    time_text = _warning_time_range_text(cleaned)
    if "疑似連續重複轉錄" in cleaned:
        parts = ["疑似連續重複轉錄"]
        repeat_match = re.search(r"同一句連續重複\s*\d+\s*次", cleaned)
        if repeat_match:
            parts.append(repeat_match.group(0))
        if time_text:
            parts.append(time_text)
        return "；".join(parts)
    if "重複轉錄幻覺" in cleaned:
        issue_match = re.search(r"分段疑似[^（(，,。；;）)]*重複轉錄幻覺", cleaned)
        parts = [issue_match.group(0) if issue_match else "疑似重複轉錄幻覺"]
        if time_text:
            parts.append(time_text)
        return "；".join(parts)
    if time_text:
        return time_text
    return ""


def _segment_indices_for_time_range(start_seconds: int, end_seconds: int) -> range:
    normalized_start = max(0, int(start_seconds or 0))
    normalized_end = max(normalized_start, int(end_seconds or normalized_start))
    inclusive_end = normalized_end - 1 if normalized_end > normalized_start else normalized_end
    first_index = normalized_start // REVIEW_SEGMENT_SECONDS
    last_index = max(first_index, inclusive_end // REVIEW_SEGMENT_SECONDS)
    return range(first_index, last_index + 1)


def review_segment_label(index: int) -> str:
    """Return the user-facing one-based transcript segment label."""
    return f"第 {index + 1} 段"


def review_segment_details_from_text(text: str) -> list[dict]:
    """Extract zero-based segment indices and nearby time ranges from warning text."""
    details_by_index: dict[int, dict] = {}
    issue_text = _warning_issue_text(text)

    def add_issue(detail: dict) -> None:
        if not issue_text:
            return
        issues = [
            str(issue).strip()
            for issue in detail.get("issues") or []
            if str(issue).strip()
        ]
        if issue_text not in issues:
            issues.append(issue_text)
        detail["issues"] = issues

    for match in _SEGMENT_DETAIL_PATTERN.finditer(str(text or "")):
        raw_index = match.group("chinese") or match.group("english")
        try:
            index = int(raw_index) - 1
        except (TypeError, ValueError):
            continue
        if index < 0:
            continue
        detail = details_by_index.setdefault(
            index,
            {
                "index": index,
                "label": review_segment_label(index),
            },
        )
        add_issue(detail)
        time_match = _TIME_RANGE_PATTERN.search(match.group("trailing") or "")
        if time_match:
            detail["start_seconds"] = _clock_seconds(
                time_match.group("start_minutes"),
                time_match.group("start_seconds"),
            )
            detail["end_seconds"] = _clock_seconds(
                time_match.group("end_minutes"),
                time_match.group("end_seconds"),
            )
    for match in _WARNING_TIME_RANGE_PATTERN.finditer(str(text or "")):
        start_seconds = _clock_seconds(
            match.group("start_minutes"),
            match.group("start_seconds"),
        )
        end_seconds = _clock_seconds(
            match.group("end_minutes"),
            match.group("end_seconds"),
        )
        for index in _segment_indices_for_time_range(start_seconds, end_seconds):
            detail = details_by_index.setdefault(
                index,
                {
                    "index": index,
                    "label": review_segment_label(index),
                },
            )
            add_issue(detail)
            detail.setdefault("start_seconds", max(start_seconds, index * REVIEW_SEGMENT_SECONDS))
            detail.setdefault(
                "end_seconds",
                min(max(end_seconds, start_seconds), (index + 1) * REVIEW_SEGMENT_SECONDS),
            )
    return [details_by_index[index] for index in sorted(details_by_index)]


def review_segment_indices_from_text(text: str) -> list[int]:
    """Extract zero-based segment indices from Chinese or English warning text."""
    return [detail["index"] for detail in review_segment_details_from_text(text)]


def review_segment_label_sort_key(label: str) -> tuple[int, int, str]:
    """Sort labels by segment number first, then place unrecognized labels last."""
    cleaned = str(label or "").strip()
    for matcher in _SEGMENT_TEXT_PATTERNS:
        match = matcher.search(cleaned)
        if not match:
            continue
        try:
            return (0, int(match.group(1)), cleaned)
        except (TypeError, ValueError):
            continue
    return (1, 0, cleaned)
