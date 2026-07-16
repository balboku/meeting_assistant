"""Helpers for deriving transcript review segment labels from quality text."""

from __future__ import annotations

import re


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


def _clock_seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


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
