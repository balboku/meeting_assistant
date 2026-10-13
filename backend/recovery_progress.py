"""Stop transcription recovery when repeated local attempts add no usable words."""

from dataclasses import dataclass


NO_PROGRESS_ERROR_MARKER = "TRANSCRIPTION_RECOVERY_NO_PROGRESS"
MAX_CONSECUTIVE_UNRESOLVED_WINDOWS = 3


class RecoveryNoProgressError(Exception):
    """A terminal, reviewable failure rather than another automatic retry."""


@dataclass
class RecoveryProgress:
    consecutive_unresolved: int = 0

    def resolved(self) -> None:
        self.consecutive_unresolved = 0

    def unresolved(self, *, segment_index: int, start_seconds: int, end_seconds: int) -> None:
        self.consecutive_unresolved += 1
        if self.consecutive_unresolved >= MAX_CONSECUTIVE_UNRESOLVED_WINDOWS:
            raise RecoveryNoProgressError(
                f"{NO_PROGRESS_ERROR_MARKER}: 第 {segment_index + 1} 段連續 "
                f"{self.consecutive_unresolved} 個補救視窗未取得可用語句；"
                f"最近視窗 {start_seconds}-{end_seconds} 秒，需人工核聽來源音訊"
            )
