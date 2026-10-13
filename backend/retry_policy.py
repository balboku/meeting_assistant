"""Retry classification and bounded backoff policy without persistence."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

from backend.transcription_budget import BUDGET_ERROR_MARKER
from backend.recovery_progress import NO_PROGRESS_ERROR_MARKER


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
QUALITY_RETRY_MARKERS = (
    "轉錄不完整",
    "品質閘門",
)
TERMINAL_RETRY_MARKERS = (
    "補救遞迴已達安全上限",
    "音訊語音密度高但逐字稿文字量偏低",
    "逐字稿文字量明顯超過音訊語音活動可支持範圍",
)

DAILY_QUOTA_ID = "generaterequestsperdayperprojectpermodel"


def is_daily_quota_error(error_detail: str) -> bool:
    """Match the quota ID, not every 429 (which can also mean per-minute limits)."""
    normalized = "".join(char for char in str(error_detail or "").casefold() if char.isalnum())
    return DAILY_QUOTA_ID in normalized


def is_transcription_budget_error(error_detail: str) -> bool:
    return BUDGET_ERROR_MARKER in str(error_detail or "")


def is_transcription_recovery_no_progress_error(error_detail: str) -> bool:
    return NO_PROGRESS_ERROR_MARKER in str(error_detail or "")


def _sunday(year: int, month: int, ordinal: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=ordinal - 1)


def _pacific_offset_hours(now_utc: datetime) -> int:
    """US Pacific UTC offset under the current March/November DST rules."""
    year = now_utc.year
    dst_start = datetime.combine(_sunday(year, 3, 2), time(10), timezone.utc)
    dst_end = datetime.combine(_sunday(year, 11, 1), time(9), timezone.utc)
    return -7 if dst_start <= now_utc < dst_end else -8


def next_pacific_daily_reset_utc(now_utc: datetime | None = None) -> datetime:
    """Next Pacific midnight plus five minutes, without requiring OS tzdata."""
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    current = current.astimezone(timezone.utc)
    pacific_date = (current + timedelta(hours=_pacific_offset_hours(current))).date()
    next_date = pacific_date + timedelta(days=1)
    # DST changes at 02:00 local time.  Midnight on the transition Sunday
    # still uses the previous day's offset.
    daylight_at_midnight = (
        _sunday(next_date.year, 3, 2) < next_date
        <= _sunday(next_date.year, 11, 1)
    )
    offset = 7 if daylight_at_midnight else 8
    return datetime.combine(next_date, time(0, 5), timezone.utc) + timedelta(hours=offset)


def transient_retry_delay_seconds(attempts: int = 1) -> int:
    try:
        base_delay = max(
            0,
            int(os.getenv("JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS", "30")),
        )
    except ValueError:
        base_delay = 30
    if base_delay <= 0:
        return 0
    try:
        multiplier = float(
            os.getenv("JOB_QUEUE_TRANSIENT_RETRY_BACKOFF_MULTIPLIER", "2")
        )
    except ValueError:
        multiplier = 2.0
    multiplier = min(10.0, max(1.0, multiplier))
    try:
        max_delay = max(
            0,
            int(os.getenv("JOB_QUEUE_TRANSIENT_RETRY_MAX_DELAY_SECONDS", "300")),
        )
    except ValueError:
        max_delay = 300
    if max_delay <= 0:
        return base_delay
    try:
        retry_number = max(1, int(attempts))
    except (TypeError, ValueError):
        retry_number = 1
    return min(
        max_delay,
        round(base_delay * (multiplier ** (retry_number - 1))),
    )


def is_transient_error(error_detail: str) -> bool:
    normalized = (error_detail or "").lower().replace("_", "")
    return any(marker in normalized for marker in TRANSIENT_RETRY_MARKERS)


def effective_job_max_attempts(error_detail: str, configured: int) -> int:
    maximum = max(1, int(configured))
    if is_transient_error(error_detail):
        return maximum
    if any(marker in str(error_detail or "") for marker in TERMINAL_RETRY_MARKERS):
        return 1
    if not any(marker in str(error_detail or "") for marker in QUALITY_RETRY_MARKERS):
        return maximum
    try:
        quality_maximum = int(os.getenv("JOB_QUEUE_QUALITY_MAX_ATTEMPTS", "3"))
    except ValueError:
        quality_maximum = 3
    return min(maximum, max(1, quality_maximum))
