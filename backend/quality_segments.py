"""Helpers for deriving transcript review segment labels from quality text."""

from __future__ import annotations

import re


_SEGMENT_TEXT_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*段"),
    re.compile(r"\bSegment\s*#?\s*(\d+)\b", flags=re.IGNORECASE),
)


def review_segment_label(index: int) -> str:
    """Return the user-facing one-based transcript segment label."""
    return f"第 {index + 1} 段"


def review_segment_indices_from_text(text: str) -> list[int]:
    """Extract zero-based segment indices from Chinese or English warning text."""
    indices: set[int] = set()
    for matcher in _SEGMENT_TEXT_PATTERNS:
        for match in matcher.finditer(str(text or "")):
            try:
                index = int(match.group(1)) - 1
            except (TypeError, ValueError):
                continue
            if index >= 0:
                indices.add(index)
    return sorted(indices)


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
