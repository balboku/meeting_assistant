"""Audio-backed guards for transcript hallucination detection."""

from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("MeetingAssistant.TranscriptAudioQuality")

TRANSCRIPT_SPEECH_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND = min(
    50.0,
    max(5.0, float(os.getenv("TRANSCRIPT_SPEECH_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND", "8"))),
)
TRANSCRIPT_SPEECH_DENSITY_MAX_WORDS_PER_ACTIVE_SECOND = min(
    20.0,
    max(3.0, float(os.getenv("TRANSCRIPT_SPEECH_DENSITY_MAX_WORDS_PER_ACTIVE_SECOND", "8"))),
)
TRANSCRIPT_SPEECH_DENSITY_EXCESS_MIN_CHARACTERS = max(
    8,
    int(os.getenv("TRANSCRIPT_SPEECH_DENSITY_EXCESS_MIN_CHARACTERS", "8")),
)
TRANSCRIPT_LOCAL_SILENCE_SKIP_MIN_SECONDS = max(
    5.0, float(os.getenv("TRANSCRIPT_LOCAL_SILENCE_SKIP_MIN_SECONDS", "5"))
)
TRANSCRIPT_SPEECH_ACTIVITY_DBFS_FLOOR = min(
    -32.0, max(-70.0, float(os.getenv("TRANSCRIPT_SPEECH_ACTIVITY_DBFS_FLOOR", "-50")))
)
TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_SECONDS = max(
    0.0, float(os.getenv("TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_SECONDS", "2"))
)
TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_RATIO = min(
    0.2, max(0.0, float(os.getenv("TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_RATIO", "0.05")))
)
TRANSCRIPT_SPEECH_EXCESS_DENSITY_ISSUE_MARKER = (
    "逐字稿文字量明顯超過音訊語音活動可支持範圍"
)
_TIMESTAMP_PATTERN = re.compile(r"\[\d{1,3}:[0-5]\d\]")
_UNCERTAINTY_MARKERS = ("[聽不清]", "[台語音訊不清晰]")
_TIMED_TURN_PATTERN = re.compile(
    r"^\[\d{1,3}:[0-5]\d\].*?[：:]\s*(?P<body>.+)$", re.MULTILINE
)


def _spoken_text(transcript: str) -> str:
    text = _TIMESTAMP_PATTERN.sub("", transcript or "")
    text = re.sub(r"\*{0,2}\[[^\]]+\]\*{0,2}\s*[：:]", "", text)
    for marker in _UNCERTAINTY_MARKERS:
        text = text.replace(marker, "")
    return text


def _spoken_character_count(transcript: str) -> int:
    return len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", _spoken_text(transcript)))


def _spoken_density_counts(transcript: str) -> tuple[int, int]:
    """Count CJK/digits as characters, but alphabetic speech as whole words.

    Latin letters are not comparable to Chinese characters. A normal English
    presentation previously exceeded the Chinese character-rate limit and was
    repeatedly retranscribed, then replaced by synthetic uncertainty markers.
    """
    text = _spoken_text(transcript)
    characters = len(re.findall(r"[0-9\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z\u00c0-\u02af]+(?:['’][A-Za-z\u00c0-\u02af]+)*", text))
    return characters, words


def transcript_is_uncertainty_only(transcript: str) -> bool:
    """Return whether the model explicitly declined to guess any spoken text."""
    return any(marker in (transcript or "") for marker in _UNCERTAINTY_MARKERS) and _spoken_character_count(transcript) == 0


def uncertainty_dominated_summary_payload(transcript: str) -> Optional[dict[str, Any]]:
    """Return an evidence-safe summary when most timestamped turns are unknown."""
    bodies = [match.group("body").strip() for match in _TIMED_TURN_PATTERN.finditer(transcript or "")]
    if len(bodies) < 10:
        return None
    uncertain = [body for body in bodies if any(marker in body for marker in _UNCERTAINTY_MARKERS)]
    verified = [body for body in bodies if body not in uncertain]
    verified_characters = sum(_spoken_character_count(body) for body in verified)
    if len(uncertain) / len(bodies) < 0.75 or len(verified) > 20 or verified_characters > 600:
        return None
    return {
        "discussion_summary": [{
            "id": "D1",
            "topic": "來源音訊無法可靠辨識",
            "summary": "逐字稿大部分為未確認內容，無法可靠整理會議主題、決議或執行事項。",
            "context": "系統已保留時間戳與原始媒體，請由人工回查重要時段。",
            "key_points": ["不得將零散或重複的泛用句視為已確認結論。"],
            "impact": "若直接採用自動摘要，可能產生不存在的決議或待辦。",
            "open_questions": ["實際討論內容、決議、負責人與期限均待人工確認。"],
            "evidence_timecodes": [],
        }],
        "final_decisions": [],
        "action_items": [],
    }


def _configure_audio_segment() -> Any:
    from pydub import AudioSegment

    configured = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BINARY")
    if configured and Path(configured).is_file():
        AudioSegment.converter = configured
    return AudioSegment


def speech_activity_threshold(dbfs: float) -> float:
    """Ignore amplified room noise while retaining sustained quiet speech."""
    return max(
        TRANSCRIPT_SPEECH_ACTIVITY_DBFS_FLOOR,
        min(-32.0, dbfs - 14.0),
    )


def recovery_depth_limit_fallback(
    *,
    segment_index: int,
    offset_seconds: int,
    duration_seconds: int,
    max_depth: int,
) -> tuple[str, dict[str, Any]]:
    """Return a bounded local uncertainty result instead of failing the whole job."""
    minutes, seconds = divmod(max(0, int(offset_seconds)), 60)
    issue = f"補救遞迴已達安全上限（{max_depth} 層），局部保守標記為聽不清"
    return (
        f"[{minutes:02d}:{seconds:02d}] **[發言者不明]**：[聽不清]",
        {
            "segment_index": segment_index,
            "start_seconds": offset_seconds,
            "end_seconds": offset_seconds + duration_seconds,
            "issue": issue,
        },
    )


def audio_has_insufficient_transcription_evidence(audio_path: Path) -> bool:
    """Return whether a material audio window has virtually no voice activity."""
    try:
        audio = _configure_audio_segment().from_file(str(audio_path))
        from pydub import silence

        duration_seconds = len(audio) / 1000
        dbfs = float(audio.dBFS)
        if duration_seconds < TRANSCRIPT_LOCAL_SILENCE_SKIP_MIN_SECONDS or not math.isfinite(dbfs):
            return False
        threshold = speech_activity_threshold(dbfs)
        active_ranges = silence.detect_nonsilent(
            audio,
            min_silence_len=500,
            silence_thresh=threshold,
            seek_step=100,
        )
        active_seconds = sum(max(0, end - start) for start, end in active_ranges) / 1000
        return (
            active_seconds <= TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_SECONDS
            and active_seconds / duration_seconds <= TRANSCRIPT_LOCAL_SILENCE_SKIP_MAX_ACTIVE_RATIO
        )
    except Exception as exc:
        logger.debug(
            "Unable to run local low-information audio gate for %s: %s",
            audio_path.name,
            str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__,
        )
        return False


def speech_backed_transcript_excess_density_quality_issues(
    audio_path: Path,
    transcript: str,
    *,
    expected_start_seconds: int,
    expected_end_seconds: int,
    audio_offset_seconds: Optional[int] = None,
    audio_cache: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Flag text volume that sparse source speech cannot plausibly support."""
    try:
        cache_key = str(audio_path)
        audio = audio_cache.get(cache_key) if audio_cache is not None else None
        if audio is None:
            audio = _configure_audio_segment().from_file(str(audio_path))
            if audio_cache is not None:
                audio_cache[cache_key] = audio
        from pydub import silence

        dbfs = float(audio.dBFS)
        if not math.isfinite(dbfs):
            return []
        silence_threshold = speech_activity_threshold(dbfs)
    except Exception as exc:
        logger.debug(
            "無法檢查逐字稿過量文字（%s）：%s",
            audio_path.name,
            str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__,
        )
        return []

    audio_offset_seconds = (
        expected_start_seconds if audio_offset_seconds is None else audio_offset_seconds
    )
    start_ms = max(0, (expected_start_seconds - audio_offset_seconds) * 1000)
    end_ms = min(len(audio), (expected_end_seconds - audio_offset_seconds) * 1000)
    if end_ms <= start_ms:
        return []

    active_ranges = silence.detect_nonsilent(
        audio[start_ms:end_ms],
        min_silence_len=500,
        silence_thresh=silence_threshold,
        seek_step=100,
    )
    active_seconds = sum(max(0, end - start) for start, end in active_ranges) / 1000
    spoken_characters = _spoken_character_count(transcript)
    if spoken_characters < TRANSCRIPT_SPEECH_DENSITY_EXCESS_MIN_CHARACTERS:
        return []

    characters, words = _spoken_density_counts(transcript)
    # Add the language-specific time requirements so mixed speech cannot use
    # the full duration twice. These are conservative heuristics, not proof
    # that the energy detector has recognized speech or understood its words.
    minimum_seconds = (
        characters / TRANSCRIPT_SPEECH_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND
        + words / TRANSCRIPT_SPEECH_DENSITY_MAX_WORDS_PER_ACTIVE_SECOND
    )
    if active_seconds > 0 and minimum_seconds <= max(1.0, active_seconds):
        return []
    rate = spoken_characters / max(1.0, active_seconds)
    detail = (
        f"{characters} 個中文字／數字、{words} 個英文詞／{active_seconds:.0f} 秒音訊活動"
        if words else
        f"{spoken_characters} 字／{active_seconds:.0f} 秒有效語音，{rate:.1f} 字/秒"
    )
    return [
        f"{TRANSCRIPT_SPEECH_EXCESS_DENSITY_ISSUE_MARKER}（{detail}）"
    ]
