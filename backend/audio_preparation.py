"""Conservative routing for lossless transcription-only audio copies."""

import math


def needs_initial_speech_focus(
    average_dbfs: float,
    peak_dbfs: float,
    active_ratio: float,
    *,
    clip_dbfs: float,
    min_dynamic_range_db: float,
    min_active_ratio: float,
) -> bool:
    """Include quiet, uneven recordings that peak normalization cannot lift.

    A brief loud sound sets the gain for the entire file during peak
    normalization. Most speech can consequently stay below the activity
    detector's absolute floor, causing false excess-text failures. Reuse the
    existing bounded loudness filter, without lowering the quality gates.
    Energy is not proof of speech: retain the downstream transcription checks.
    """
    if not all(math.isfinite(value) for value in (average_dbfs, peak_dbfs, active_ratio)):
        return False
    dynamic_range = peak_dbfs - average_dbfs
    clipped_dense = (
        peak_dbfs >= clip_dbfs
        and dynamic_range >= min_dynamic_range_db
        and active_ratio >= min_active_ratio
    )
    quiet_uneven = (
        average_dbfs <= -32.0
        and peak_dbfs >= -40.0
        and dynamic_range >= max(20.0, min_dynamic_range_db)
        and active_ratio >= 0.20
    )
    return clipped_dense or quiet_uneven
