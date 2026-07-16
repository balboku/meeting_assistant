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
