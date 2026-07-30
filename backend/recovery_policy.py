"""Pure policy helpers that keep recursive transcription recovery bounded."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional


SEGMENT_RECOVERY_SPLIT_SECONDS = (300, 180, 120, 60, 30, 15, 10, 5)
TRANSCRIPT_RECOVERY_MAX_DEPTH = max(
    1,
    min(16, int(os.getenv("TRANSCRIPT_RECOVERY_MAX_DEPTH", "8"))),
)


def strictly_shrinking_export_bounds(
    duration_ms: int,
    start_ms: int,
    end_ms: int,
    export_start_ms: int,
    export_end_ms: int,
) -> tuple[int, int]:
    if export_end_ms - export_start_ms >= max(1000, duration_ms - 1000):
        return start_ms, end_ms
    return export_start_ms, export_end_ms


def recovery_subsegment_path(
    audio_path: Path,
    chunk_seconds: int,
    index: int,
    start_ms: int,
    end_ms: int,
    export_format: str,
) -> Path:
    identity = hashlib.sha256(
        f"{audio_path.resolve()}|{chunk_seconds}|{index}|{start_ms}|{end_ms}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return audio_path.parent / (
        f"_recovery_{identity}_{chunk_seconds}s_{index:03d}.{export_format}"
    )


def next_recovery_chunk_seconds(
    duration_seconds: int,
    preferred_chunk_seconds: Optional[int] = None,
) -> Optional[int]:
    if preferred_chunk_seconds is not None:
        preferred_chunk_seconds = max(1, int(preferred_chunk_seconds))
    for chunk_seconds in SEGMENT_RECOVERY_SPLIT_SECONDS:
        if preferred_chunk_seconds is not None and chunk_seconds > preferred_chunk_seconds:
            continue
        if chunk_seconds < duration_seconds:
            return chunk_seconds
    return None


def next_smaller_recovery_chunk_seconds(chunk_seconds: int) -> Optional[int]:
    for candidate in SEGMENT_RECOVERY_SPLIT_SECONDS:
        if candidate < chunk_seconds:
            return candidate
    return None
