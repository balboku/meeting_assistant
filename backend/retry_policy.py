"""Retry classification and bounded backoff policy without persistence."""

from __future__ import annotations

import os


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
    "補救遞迴已達安全上限",
)


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
    if not any(marker in str(error_detail or "") for marker in QUALITY_RETRY_MARKERS):
        return maximum
    try:
        quality_maximum = int(os.getenv("JOB_QUEUE_QUALITY_MAX_ATTEMPTS", "3"))
    except ValueError:
        quality_maximum = 3
    return min(maximum, max(1, quality_maximum))
