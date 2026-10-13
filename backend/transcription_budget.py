"""Bound model requests made by one audio-transcription attempt."""

from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field


BUDGET_ERROR_MARKER = "TRANSCRIPTION_REQUEST_BUDGET_EXHAUSTED"
DEFAULT_SEGMENT_REQUEST_LIMIT = 32
DEFAULT_JOB_REQUEST_LIMIT = 160


class TranscriptionRequestBudgetExceeded(Exception):
    """A recoverable stop, never a valid partial transcript."""


@dataclass
class TranscriptionRequestBudget:
    job_id: str
    segment_limit: int = DEFAULT_SEGMENT_REQUEST_LIMIT
    job_limit: int = DEFAULT_JOB_REQUEST_LIMIT
    total_requests: int = 0
    segment_requests: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def ensure_available(self, segment_index: int) -> None:
        segment_count = self.segment_requests[segment_index]
        if segment_count >= self.segment_limit or self.total_requests >= self.job_limit:
            raise TranscriptionRequestBudgetExceeded(
                f"{BUDGET_ERROR_MARKER}: job={self.job_id}, "
                f"segment={segment_index + 1}, "
                f"segment_requests={segment_count}/{self.segment_limit}, "
                f"job_requests={self.total_requests}/{self.job_limit}"
            )

    def reserve(self, segment_index: int) -> None:
        """Count a request before sending it, including fallback-model calls."""
        self.ensure_available(segment_index)
        self.segment_requests[segment_index] += 1
        self.total_requests += 1


ACTIVE_TRANSCRIPTION_BUDGET: ContextVar[TranscriptionRequestBudget | None] = (
    ContextVar("active_transcription_budget", default=None)
)
