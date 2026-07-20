"""
=============================================================================
backend/tasks.py — 背景任務處理器
=============================================================================
封裝 meeting_assistant.py 的核心 AI 邏輯，
讓 FastAPI 能夠以「背景任務（Background Task）」的方式非同步執行，
確保上傳媒體檔後 API 能立即回應，不讓使用者等待。
=============================================================================
"""

import os
import sys
import uuid
import re
import time
import json
import logging
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Optional

# 將專案根目錄加入 sys.path，才能 import meeting_assistant
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from google import genai
from google.genai import types
from dotenv import load_dotenv

from backend.database import (
    _repeated_transcript_turn_review_segments,
    is_job_cancel_requested,
    update_job_status,
    save_meeting
)

# 載入 .env 環境變數
load_dotenv(dotenv_path=ROOT_DIR / ".env")

logger = logging.getLogger("MeetingAssistant.Tasks")


def _env_model(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


# 常數
TRANSCRIPTION_MODEL     = _env_model("TRANSCRIPTION_MODEL", _env_model("GEMINI_MODEL", "gemini-3.1-flash-lite"))
GEMINI_MODEL            = TRANSCRIPTION_MODEL
SUMMARY_MODEL           = _env_model("SUMMARY_MODEL", "gemma-4-31b-it")
SUMMARY_FALLBACK_MODEL  = _env_model("SUMMARY_FALLBACK_MODEL", GEMINI_MODEL)
SUMMARY_VERIFIER_MODEL  = _env_model("SUMMARY_VERIFIER_MODEL", "gemini-3.5-flash")
# 增加處理的等待時間上限 (10分鐘)
MAX_UPLOAD_WAIT_SECONDS = 600
POLLING_INTERVAL        = 3
SEGMENT_MINUTES         = 10
TIMESTAMP_PATTERN       = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]")
SEGMENT_CACHE_VERSION   = 4
SEGMENT_CACHE_DIRNAME   = "segment_cache"
SEGMENT_TARGET_SECONDS  = SEGMENT_MINUTES * 60
SEGMENT_SILENCE_WINDOW_SECONDS = int(os.getenv("SEGMENT_SILENCE_WINDOW_SECONDS", "45"))
SEGMENT_OVERLAP_SECONDS = int(os.getenv("SEGMENT_OVERLAP_SECONDS", "2"))
AUDIO_PREPROCESSING_ENABLED = os.getenv("AUDIO_PREPROCESSING", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
AUDIO_PREPROCESSING_VERSION = 1
AUDIO_MIN_DBFS = float(os.getenv("AUDIO_MIN_DBFS", "-55"))
AUDIO_NORMALIZE_BELOW_DBFS = float(os.getenv("AUDIO_NORMALIZE_BELOW_DBFS", "-28"))
SEGMENT_COMPLETENESS_GRACE_SECONDS = 120
SEGMENT_TIMESTAMP_BOUNDARY_TOLERANCE_SECONDS = max(
    0,
    int(os.getenv("SEGMENT_TIMESTAMP_BOUNDARY_TOLERANCE_SECONDS", "5")),
)
SEGMENT_RECOVERY_SPLIT_SECONDS = (300, 180, 120, 60, 30, 15, 10, 5)
TRANSCRIPT_SECTION_HEADING = "## 📝 四、完整逐字稿 (Verbatim Transcript)"
TRANSCRIPT_SECTION_PATTERN = re.compile(
    r"^##\s*[^\n]*(?:Verbatim Transcript|完整逐字稿|逐字稿|蝔)[^\n]*\n",
    re.MULTILINE | re.IGNORECASE,
)
NEXT_TOP_LEVEL_SECTION_PATTERN = re.compile(r"^##\s+", re.MULTILINE)
SEGMENT_INCOMPLETE_MARKERS = (
    "系統提示：此處音檔包含無意義雜訊",
    "已自動過濾後續重複內容",
)
SEGMENT_REPETITION_MIN_LINES = 12
SEGMENT_REPETITION_RUN_THRESHOLD = 8
SEGMENT_REPETITION_RATIO_THRESHOLD = 0.5
SEGMENT_SHORT_TURN_MAX_CHARS = 8
SEGMENT_SHORT_TURN_RUN_THRESHOLD = 20
SEGMENT_LONG_TURN_CHARS = 1200
SEGMENT_MAX_NORMALIZED_TURN_CHARS = 8000
SEGMENT_REPEATED_NGRAM_CHARS = 18
SEGMENT_REPEATED_NGRAM_THRESHOLD = 12
SEGMENT_STRUCTURED_TURN_REPEAT_THRESHOLD = 12
TRANSCRIPT_INTEGRITY_MIN_CHAR_RATIO = 0.95
TRANSCRIPT_INTEGRITY_MIN_TIMESTAMP_RATIO = 0.95
TRANSCRIPT_OMISSION_MARKERS = (
    "為節省篇幅",
    "以下省略",
    "已省略",
    "省略逐字稿",
    "逐字稿省略",
    "不逐字列出",
    "已過濾逐字稿",
    "omitted for brevity",
    "transcript omitted",
    "transcript truncated",
)


class JobCancelled(RuntimeError):
    """Raised when a persisted job receives a cancellation request."""


@dataclass(frozen=True)
class AudioSlice:
    """Temporary audio segment with its absolute position in the meeting."""

    path: Path
    start_seconds: int
    end_seconds: int


def _raise_if_cancelled(job_id: str) -> None:
    if is_job_cancel_requested(job_id):
        raise JobCancelled("任務已取消")


def _prepend_tool_dir(tool_path: str) -> None:
    path = Path(tool_path.strip().strip('"'))
    if not path.is_file():
        return

    tool_dir = str(path.parent)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if tool_dir not in path_entries:
        os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")


def _configure_ffmpeg_tools() -> None:
    """Make pydub find ffmpeg/ffprobe even before Windows PATH refreshes."""
    for env_name in ("FFMPEG_PATH", "FFMPEG_BINARY", "FFPROBE_PATH", "FFPROBE_BINARY"):
        configured = os.getenv(env_name, "").strip()
        if configured:
            _prepend_tool_dir(configured)


def clean_hallucinated_loops(text: str) -> str:
    """清理結尾常見的瘋狂重複迴圈 (例如不斷重複「那個，」)"""
    if not text:
        return ""
    # 尋找長度1~20的字串片段，連續出現超過8次
    pattern = re.compile(r'(.{1,20})\1{8,}')
    meaningful_chars = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]")
    for match in pattern.finditer(text):
        repeated_unit = match.group(1)
        if not meaningful_chars.search(repeated_unit):
            continue
        return text[:match.start()] + "\n\n[系統提示：此處音檔包含無意義雜訊，已自動過濾後續重複內容]"
    return text


def _normalize_domain_terms(text: str) -> str:
    """Normalize common STT/LLM mistakes for Maxima medical-device meetings."""
    if not text:
        return ""
    for source, target in TERMINOLOGY_REPLACEMENTS:
        text = text.replace(source, target)
    for pattern, target in TERMINOLOGY_REGEX_REPLACEMENTS:
        text = re.sub(pattern, target, text)
    for source in ("Qisda", "Jasta", "加斯達"):
        text = text.replace(source, "佳世達")
    text = re.sub(r"佳世達\s*[（(]\s*佳世達\s*[）)]", "佳世達", text)
    text = re.sub(r"\$\s*\\?right\s*arrow\s*\$", "→", text, flags=re.IGNORECASE)
    text = re.sub(r"\$\s*\\?rightarrow\s*\$", "→", text, flags=re.IGNORECASE)
    text = re.sub(r"\$\s*ightarrow\s*\$", "→", text, flags=re.IGNORECASE)
    return text


def _transcript_quality_notice(transcript: str) -> str:
    if not transcript:
        return ""
    quality_markers = ("系統提示：", "已自動過濾", "雜訊", "音訊不清晰")
    if any(marker in transcript for marker in quality_markers):
        return (
            "逐字稿品質註記：音訊中有片段被標示為雜訊、聽不清或已自動過濾；"
            "該時間點附近內容可能缺漏，重要結論請回查原始媒體檔。"
        )
    return ""


def _prepend_transcript_quality_notice(meeting_content: str, transcript: str) -> str:
    notice = _transcript_quality_notice(transcript)
    if not notice or "逐字稿品質註記" in meeting_content:
        return meeting_content

    match = re.search(r"##\s*📋\s*一、[^\n]*\n", meeting_content)
    if not match:
        return f"> ⚠️ {notice}\n\n{meeting_content}"

    insert_at = match.end()
    return (
        meeting_content[:insert_at]
        + f"\n> ⚠️ {notice}\n"
        + meeting_content[insert_at:]
    )


def _replace_transcript_section(meeting_content: str, full_transcript: str) -> str:
    """Keep the generated summary, but force the transcript section to remain verbatim."""
    content = meeting_content or ""
    transcript = (full_transcript or "").strip()
    match = TRANSCRIPT_SECTION_PATTERN.search(content)
    if match:
        prefix = content[:match.start()].rstrip()
        suffix = _extract_post_transcript_sections(content)
    else:
        prefix = content.rstrip()
        suffix = ""

    prefix = re.sub(r"\n-{3,}\s*$", "", prefix).rstrip()
    separator = "\n\n---\n\n" if prefix else ""
    result = f"{prefix}{separator}{TRANSCRIPT_SECTION_HEADING}\n{transcript}\n"
    if suffix:
        result = f"{result.rstrip()}\n\n{suffix.rstrip()}\n"
    return result


def _canonical_transcript_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines()).strip()


def _extract_post_transcript_sections(meeting_content: str) -> str:
    match = TRANSCRIPT_SECTION_PATTERN.search(meeting_content or "")
    if not match:
        return ""
    next_section = NEXT_TOP_LEVEL_SECTION_PATTERN.search(meeting_content or "", match.end())
    if not next_section:
        return ""
    return (meeting_content or "")[next_section.start():].strip()


def _extract_transcript_section_body(meeting_content: str) -> Optional[str]:
    match = TRANSCRIPT_SECTION_PATTERN.search(meeting_content or "")
    if not match:
        return None
    next_section = NEXT_TOP_LEVEL_SECTION_PATTERN.search(meeting_content or "", match.end())
    end = next_section.start() if next_section else len(meeting_content or "")
    return (meeting_content or "")[match.end():end].strip()


_TRANSCRIPT_SEGMENT_HEADING_PATTERN = re.compile(
    r"(?m)^#{1,6}\s*(?:"
    r"【第\s*(?P<zh_index>\d+)\s*段\s*[｜|]\s*"
    r"(?P<zh_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<zh_end>\d{1,3}:[0-5]\d|end)】"
    r"|\[?Segment\s+(?P<en_index>\d+)(?:/\d+)?\s*[|｜]\s*"
    r"(?P<en_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<en_end>\d{1,3}:[0-5]\d|end)\]?)\s*$",
    flags=re.IGNORECASE,
)


def _clock_seconds(value: str, default: int = 0) -> int:
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", value or "")
    if not match:
        return default
    return int(match.group(1)) * 60 + int(match.group(2))


def _transcript_segment_metadata(transcript: str) -> list[dict[str, Any]]:
    """Recover segment controls from transcript headings, including older records."""
    matches = list(_TRANSCRIPT_SEGMENT_HEADING_PATTERN.finditer(transcript or ""))
    metadata: list[dict[str, Any]] = []
    for position, match in enumerate(matches):
        raw_index = match.group("zh_index") or match.group("en_index")
        raw_start = match.group("zh_start") or match.group("en_start") or "00:00"
        raw_end = match.group("zh_end") or match.group("en_end") or "end"
        start_seconds = _clock_seconds(raw_start)
        if raw_end.lower() == "end":
            next_start = None
            if position + 1 < len(matches):
                next_match = matches[position + 1]
                next_raw_start = next_match.group("zh_start") or next_match.group("en_start") or ""
                next_start = _clock_seconds(next_raw_start, start_seconds + SEGMENT_TARGET_SECONDS)
            end_seconds = next_start or start_seconds + SEGMENT_TARGET_SECONDS
        else:
            end_seconds = _clock_seconds(raw_end, start_seconds + SEGMENT_TARGET_SECONDS)
        metadata.append({
            "index": max(0, int(raw_index) - 1),
            "start_seconds": start_seconds,
            "end_seconds": max(start_seconds + 1, end_seconds),
            "status": "existing_record",
            "issues": [],
        })
    if not metadata:
        for index in _timestamp_bucketed_transcript_segments(transcript):
            metadata.append({
                "index": index,
                "start_seconds": index * SEGMENT_TARGET_SECONDS,
                "end_seconds": (index + 1) * SEGMENT_TARGET_SECONDS,
                "status": "existing_record",
                "issues": [],
            })
    return metadata


def _timestamp_bucketed_transcript_segments(transcript: str) -> dict[int, str]:
    """Split legacy transcripts without headings into 10-minute timestamp buckets."""
    segments: dict[int, list[str]] = {}
    current_index: Optional[int] = None
    for raw_line in (transcript or "").splitlines():
        line = raw_line.rstrip()
        match = TIMESTAMP_PATTERN.search(line)
        if match:
            seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
            current_index = max(0, seconds // SEGMENT_TARGET_SECONDS)
        if current_index is None or not line.strip():
            continue
        segments.setdefault(current_index, []).append(line)
    return {
        index: "\n".join(lines).strip()
        for index, lines in sorted(segments.items())
        if "\n".join(lines).strip()
    }


def _transcript_segments_by_index(transcript: str) -> dict[int, str]:
    matches = list(_TRANSCRIPT_SEGMENT_HEADING_PATTERN.finditer(transcript or ""))
    segments: dict[int, str] = {}
    for position, match in enumerate(matches):
        raw_index = match.group("zh_index") or match.group("en_index")
        body_end = matches[position + 1].start() if position + 1 < len(matches) else len(transcript or "")
        body = (transcript or "")[match.end():body_end].strip()
        if body:
            segments[max(0, int(raw_index) - 1)] = body
    if not segments:
        segments = _timestamp_bucketed_transcript_segments(transcript)
    if not segments and (transcript or "").strip():
        segments[0] = (transcript or "").strip()
    return segments


def _transcript_segment_heading_count(transcript: str) -> int:
    return len(
        re.findall(
            r"(?m)^#{1,6}\s*(?:【第\s*\d+\s*段|\[Segment\s+\d+/\d+)",
            transcript or "",
        )
    )


def _timestamp_count(transcript: str) -> int:
    return len(TIMESTAMP_PATTERN.findall(transcript or ""))


def _format_segment_clock(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds or 0)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _full_transcript_repetition_quality_issues(transcript: str) -> list[str]:
    metadata_by_index = {
        int(segment["index"]): segment
        for segment in _transcript_segment_metadata(transcript)
        if isinstance(segment, dict) and "index" in segment
    }
    segment_bodies = _transcript_segments_by_index(transcript)
    if not metadata_by_index or not segment_bodies:
        issue = _segment_repetition_quality_issue(transcript)
        return [issue] if issue else []

    issues: list[str] = []
    for index, body in sorted(segment_bodies.items()):
        issue = _segment_repetition_quality_issue(body)
        if not issue:
            continue
        segment = metadata_by_index.get(index) or {}
        try:
            start_seconds = int(segment["start_seconds"])
            end_seconds = int(segment["end_seconds"])
            location = (
                f"第 {index + 1} 段｜"
                f"{_format_segment_clock(start_seconds)}-{_format_segment_clock(end_seconds)}"
            )
        except (KeyError, TypeError, ValueError):
            location = f"第 {index + 1} 段"
        issues.append(f"{location}：{issue}")
    return issues


def _transcript_integrity_issues(meeting_content: str, full_transcript: str) -> list[str]:
    """Final guardrail: the saved transcript must match the verified transcript."""
    issues: list[str] = []
    expected = _canonical_transcript_text(full_transcript)
    actual_body = _extract_transcript_section_body(meeting_content)
    if actual_body is None:
        return ["缺少完整逐字稿區塊"]

    actual = _canonical_transcript_text(actual_body)
    if not actual:
        return ["完整逐字稿區塊內容空白"]

    lowered_actual = actual.lower()
    if any(marker.lower() in lowered_actual for marker in TRANSCRIPT_OMISSION_MARKERS):
        issues.append("完整逐字稿區塊疑似含省略或截斷說明")

    if expected and actual != expected:
        issues.append("完整逐字稿區塊與原始轉錄結果不一致")

    expected_chars = len(expected)
    if expected_chars and len(actual) < expected_chars * TRANSCRIPT_INTEGRITY_MIN_CHAR_RATIO:
        issues.append(
            "完整逐字稿區塊字數低於原始轉錄結果"
            f"（{len(actual)}/{expected_chars}）"
        )

    expected_timestamps = _timestamp_count(expected)
    actual_timestamps = _timestamp_count(actual)
    if expected_timestamps and actual_timestamps < expected_timestamps * TRANSCRIPT_INTEGRITY_MIN_TIMESTAMP_RATIO:
        issues.append(
            "完整逐字稿區塊時間戳數量低於原始轉錄結果"
            f"（{actual_timestamps}/{expected_timestamps}）"
        )

    expected_segments = _transcript_segment_heading_count(expected)
    actual_segments = _transcript_segment_heading_count(actual)
    if expected_segments and actual_segments < expected_segments:
        issues.append(
            "完整逐字稿區塊缺少分段標題"
            f"（{actual_segments}/{expected_segments}）"
        )

    for repetition_issue in _full_transcript_repetition_quality_issues(actual):
        issues.append(f"完整逐字稿區塊{repetition_issue}")

    return list(dict.fromkeys(issues))


def _full_transcript_quality_issues(full_transcript: str) -> list[str]:
    """Check the assembled transcript before spending summary-model tokens."""
    probe_content = _replace_transcript_section("", full_transcript)
    return _transcript_integrity_issues(probe_content, full_transcript)


def _raise_if_full_transcript_unsafe(full_transcript: str, job_id: str) -> None:
    issues = _full_transcript_quality_issues(full_transcript)
    if issues:
        raise RuntimeError("完整逐字稿品質檢查失敗：" + "；".join(issues))
    logger.info("[%s] ✅ 完整逐字稿品質檢查通過", job_id)


def _resolve_summary_models(
    transcription_model: str,
    summary_model: Optional[str] = None,
    summary_fallback_model: Optional[str] = None,
) -> tuple[str, str]:
    primary = (summary_model or SUMMARY_MODEL or transcription_model).strip()
    fallback = (summary_fallback_model or SUMMARY_FALLBACK_MODEL or transcription_model).strip()
    return primary or transcription_model, fallback or transcription_model


def _generate_text_with_fallback(
    client,
    *,
    primary_model: str,
    fallback_model: str,
    contents: list[Any],
    config: types.GenerateContentConfig,
    job_id: str,
    stage: str,
) -> tuple[Any, str]:
    try:
        response = client.models.generate_content(
            model=primary_model,
            contents=contents,
            config=config,
        )
        return response, primary_model
    except Exception as primary_error:
        if fallback_model and fallback_model != primary_model:
            logger.warning(
                "[%s] ⚠️ %s 使用模型 %s 失敗，改用 %s：%s",
                job_id,
                stage,
                primary_model,
                fallback_model,
                primary_error,
            )
            update_job_status(
                job_id,
                "processing",
                f"⚠️ {stage} 使用 {primary_model} 失敗，改用 {fallback_model}...",
            )
            response = client.models.generate_content(
                model=fallback_model,
                contents=contents,
                config=config,
            )
            return response, fallback_model
        raise


def _format_mmss(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, total_seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _segment_time_range_text(segment: dict[str, Any]) -> str:
    try:
        start_seconds = int(segment.get("start_seconds", 0))
    except (TypeError, ValueError):
        start_seconds = 0
    try:
        end_seconds = int(segment.get("end_seconds", start_seconds))
    except (TypeError, ValueError):
        end_seconds = start_seconds
    return f"{_format_mmss(start_seconds)}-{_format_mmss(end_seconds)}"


def _quality_report_review_segments(segment_report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    review_segments: list[dict[str, Any]] = []
    for position, segment in enumerate(segment_report or []):
        if not isinstance(segment, dict):
            continue
        issues = [
            str(issue).strip()
            for issue in segment.get("issues") or []
            if str(issue).strip()
        ]
        if not issues:
            continue
        try:
            index = int(segment.get("index", position))
        except (TypeError, ValueError):
            index = position
        item: dict[str, Any] = {
            "index": index,
            "label": f"第 {index + 1} 段",
            "issues": list(dict.fromkeys(issues)),
        }
        for key in ("start_seconds", "end_seconds", "status"):
            if key in segment:
                item[key] = segment[key]
        review_segments.append(item)
    return review_segments


def _merge_repeated_turn_review_segments(
    segment_report: list[dict[str, Any]],
    full_transcript: str,
) -> None:
    """Add timestamp-located repeated-turn issues to segment quality metadata."""
    repeated_segments = _repeated_transcript_turn_review_segments(
        full_transcript,
        segments=segment_report,
    )
    if not repeated_segments:
        return

    segments_by_index: dict[int, dict[str, Any]] = {}
    for position, segment in enumerate(segment_report):
        if not isinstance(segment, dict):
            continue
        try:
            index = int(segment.get("index", position))
        except (TypeError, ValueError):
            continue
        segments_by_index[index] = segment

    for repeated_segment in repeated_segments:
        if not isinstance(repeated_segment, dict):
            continue
        try:
            index = int(repeated_segment.get("index", -1))
        except (TypeError, ValueError):
            continue
        if index < 0:
            continue
        segment = segments_by_index.get(index)
        if segment is None:
            segment = {
                "index": index,
                "start_seconds": repeated_segment.get("start_seconds"),
                "end_seconds": repeated_segment.get("end_seconds"),
                "status": "review",
                "issues": [],
            }
            segment_report.append(segment)
            segments_by_index[index] = segment
        for key in ("start_seconds", "end_seconds"):
            if segment.get(key) is None and repeated_segment.get(key) is not None:
                segment[key] = repeated_segment.get(key)
        issues = [
            str(issue).strip()
            for issue in segment.get("issues") or []
            if str(issue).strip()
        ]
        for issue in repeated_segment.get("issues") or []:
            issue_text = str(issue).strip()
            if issue_text and issue_text not in issues:
                issues.append(issue_text)
        segment["issues"] = issues


def _quality_report_segment_warnings(review_segments: list[dict[str, Any]]) -> list[str]:
    if not review_segments:
        return []
    descriptions: list[str] = []
    for segment in review_segments[:5]:
        try:
            index = int(segment.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        issue = str((segment.get("issues") or ["需複核"])[0]).strip() or "需複核"
        descriptions.append(
            f"第 {index + 1} 段｜{_segment_time_range_text(segment)}（{issue}）"
        )
    if len(review_segments) > 5:
        descriptions.append(f"另有 {len(review_segments) - 5} 段")
    return [
        "逐字稿品質警示：以下分段曾觸發轉錄品質補救或需複核："
        + "、".join(descriptions)
        + "。建議點選需複核分段定位原始錄音/錄影後抽查，必要時只重跑指定分段。"
    ]


def _quality_event_issues_for_segment(
    quality_events: list[dict[str, Any]],
    segment_index: int,
) -> list[str]:
    issues: list[str] = []
    for event in quality_events:
        if not isinstance(event, dict):
            continue
        try:
            event_index = int(event.get("segment_index", -1))
        except (TypeError, ValueError):
            continue
        if event_index != segment_index:
            continue
        issue = str(event.get("issue") or "").strip()
        if "：" in issue:
            issue = issue.split("：", 1)[1].strip()
        if issue:
            issues.append(f"曾觸發轉錄補救：{issue}")
    return list(dict.fromkeys(issues))


def _offset_transcript_timestamps(transcript: str, offset_seconds: int) -> str:
    """Convert segment-relative [mm:ss] markers to full-meeting timestamps."""
    if offset_seconds <= 0:
        return transcript

    def replace(match: re.Match) -> str:
        local_seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        return f"[{_format_mmss(local_seconds + offset_seconds)}]"

    return TIMESTAMP_PATTERN.sub(replace, transcript)


def _format_transcript_segment(
    segment_index: int,
    total_segments: int,
    start_seconds: int,
    end_seconds: Optional[int],
    transcript: str,
) -> str:
    """Wrap a transcript chunk in a stable Markdown heading for UI and export."""
    start = _format_mmss(start_seconds)
    end = _format_mmss(end_seconds) if end_seconds is not None else "end"
    body = (transcript or "").strip()
    return f"\n\n### [Segment {segment_index + 1}/{total_segments} | {start} - {end}]\n\n{body}"


def _sort_transcript_blocks_by_timestamp(transcript: str) -> str:
    blocks: list[str] = []
    current_block: list[str] = []
    for raw_line in (transcript or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if TIMESTAMP_PATTERN.match(line) and current_block:
            blocks.append("\n".join(current_block).strip())
            current_block = [line]
        else:
            current_block.append(line)
    if current_block:
        blocks.append("\n".join(current_block).strip())

    if len(blocks) < 2:
        return (transcript or "").strip()

    timed_blocks: list[tuple[int, int, str]] = []
    has_inversion = False
    previous_seconds = -1
    carry_seconds = 10**9
    for order, block in enumerate(blocks):
        match = TIMESTAMP_PATTERN.search(block)
        if match:
            seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
            if seconds < previous_seconds:
                has_inversion = True
            previous_seconds = seconds
            carry_seconds = seconds
        else:
            seconds = carry_seconds
        timed_blocks.append((seconds, order, block))

    if not has_inversion:
        return "\n\n".join(blocks)

    return "\n\n".join(
        block
        for _seconds, _order, block in sorted(timed_blocks, key=lambda item: (item[0], item[1]))
    )


def _normalized_transcript_line_content(raw_line: str) -> str:
    line = raw_line.strip()
    line = TIMESTAMP_PATTERN.sub("", line)
    line = re.sub(r"\*\*\[[^\]]+\]\*\*", "", line)
    line = re.sub(r"\[[^\]]+\]", "", line)
    line = re.sub(r"^[\uff1a:,\uff0c\s]+", "", line)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", line)


def _normalized_transcript_turns(transcript: str) -> list[str]:
    turns: list[str] = []
    for raw_line in (transcript or "").splitlines():
        line = _normalized_transcript_line_content(raw_line)
        if not line:
            continue
        if len(line) >= 12:
            turns.append(line)
    return turns


def _max_repeated_ngram_count(text: str, ngram_chars: int) -> int:
    if len(text) < ngram_chars * 2:
        return 1

    counts: dict[str, int] = {}
    max_count = 1
    for index in range(0, len(text) - ngram_chars + 1):
        ngram = text[index:index + ngram_chars]
        counts[ngram] = counts.get(ngram, 0) + 1
        if counts[ngram] > max_count:
            max_count = counts[ngram]
            if max_count >= SEGMENT_REPEATED_NGRAM_THRESHOLD:
                return max_count
    return max_count


def _long_turn_repetition_quality_issue(transcript: str) -> Optional[str]:
    for raw_line in (transcript or "").splitlines():
        line = _normalized_transcript_line_content(raw_line)
        line_length = len(line)
        if line_length < SEGMENT_LONG_TURN_CHARS:
            continue

        repeated_ngram_count = _max_repeated_ngram_count(
            line,
            SEGMENT_REPEATED_NGRAM_CHARS,
        )
        if (
            line_length >= SEGMENT_MAX_NORMALIZED_TURN_CHARS
            or repeated_ngram_count >= SEGMENT_REPEATED_NGRAM_THRESHOLD
        ):
            return (
                "\u5206\u6bb5\u7591\u4f3c\u55ae\u53e5\u91cd\u8907\u8f49\u9304\u5e7b\u89ba"
                f"\uff08\u55ae\u53e5\u9577\u5ea6 {line_length} \u5b57\uff0c"
                f"\u91cd\u8907\u7247\u6bb5 {repeated_ngram_count} \u6b21\uff09"
            )
    return None


def _short_turn_repetition_quality_issue(transcript: str) -> Optional[str]:
    longest = 1
    current = 1
    longest_turn = ""
    previous = ""
    for raw_line in (transcript or "").splitlines():
        line = _normalized_transcript_line_content(raw_line)
        if not line:
            continue
        if not (2 <= len(line) <= SEGMENT_SHORT_TURN_MAX_CHARS):
            previous = ""
            current = 1
            continue

        if line == previous:
            current += 1
        else:
            current = 1
            previous = line

        if current > longest:
            longest = current
            longest_turn = line

    if longest >= SEGMENT_SHORT_TURN_RUN_THRESHOLD:
        return (
            "分段疑似短句重複轉錄幻覺"
            f"（「{longest_turn}」連續重複 {longest} 次）"
        )
    return None


def _repetition_run_length(turns: list[str]) -> tuple[int, int]:
    longest = 1
    repeated = 0
    current = 1
    previous = ""
    for turn in turns:
        if previous and (turn == previous or turn in previous or previous in turn):
            current += 1
        else:
            if current >= 2:
                repeated += current
            longest = max(longest, current)
            current = 1
        previous = turn
    if current >= 2:
        repeated += current
    longest = max(longest, current)
    return longest, repeated


def _segment_repetition_quality_issue(transcript: str) -> Optional[str]:
    long_turn_issue = _long_turn_repetition_quality_issue(transcript)
    if long_turn_issue:
        return long_turn_issue

    short_turn_issue = _short_turn_repetition_quality_issue(transcript)
    if short_turn_issue:
        return short_turn_issue

    turns = _normalized_transcript_turns(transcript)
    if len(turns) < SEGMENT_REPETITION_MIN_LINES:
        return None

    longest_run, repeated_turns = _repetition_run_length(turns)
    repeated_ratio = repeated_turns / len(turns)
    if (
        longest_run >= SEGMENT_REPETITION_RUN_THRESHOLD
        or repeated_ratio >= SEGMENT_REPETITION_RATIO_THRESHOLD
    ):
        return (
            "分段疑似重複轉錄幻覺"
            f"（連續重複 {longest_run} 句，重複比例 {repeated_ratio:.0%}）"
        )
    return None


def _structured_numeric_turn_quality_issue(transcript: str) -> Optional[str]:
    """Detect pattern-completion loops that change only the numeric values."""
    templates: dict[str, list[tuple[str, ...]]] = {}
    number_pattern = re.compile(r"[+-]?\d+(?:[.,]\d+)?%?")

    for raw_line in (transcript or "").splitlines():
        line = TIMESTAMP_PATTERN.sub("", raw_line)
        line = re.sub(r"\*\*\[[^\]]+\]\*\*", "", line).strip()
        numbers = tuple(number_pattern.findall(line))
        if len(numbers) < 2:
            continue

        template = number_pattern.sub("#", line)
        template = re.sub(r"[^\w\u4e00-\u9fff#]+", "", template)
        if len(template) < 6:
            continue
        templates.setdefault(template, []).append(numbers)

    for numeric_variants in templates.values():
        count = len(numeric_variants)
        distinct_count = len(set(numeric_variants))
        if (
            count >= SEGMENT_STRUCTURED_TURN_REPEAT_THRESHOLD
            and distinct_count >= SEGMENT_STRUCTURED_TURN_REPEAT_THRESHOLD
        ):
            return (
                "分段疑似數列延伸轉錄幻覺"
                f"（相同句型僅替換數字，共 {count} 次）"
            )
    return None


def _speaker_context_from_transcripts(transcripts: list[str], max_lines: int = 8) -> str:
    """Expose only prior anonymous labels, never prior utterance text, to STT."""
    if not transcripts:
        return ""

    text = "\n".join(transcripts)
    labels = sorted(set(re.findall(r"\*\*\[([^\]]+)\]\*\*", text)))
    labels.extend(
        match
        for match in re.findall(r"(?:發言者\s*[A-Z]|發言者不明|多人重疊)", text)
        if match not in labels
    )
    if not labels:
        return ""

    # Keep the legacy argument for compatibility while intentionally refusing
    # to carry semantic content across audio chunks.
    del max_lines
    return "Existing speaker labels from earlier segments:\n" + ", ".join(labels[:12])


def _segment_transcript_quality_issues(
    transcript: str,
    segment_index: int,
    total_segments: int,
    segment_minutes: int = SEGMENT_MINUTES,
    expected_start_seconds: Optional[int] = None,
    expected_end_seconds: Optional[int] = None,
    is_last_segment: Optional[bool] = None,
) -> list[str]:
    """Return quality issues that make a segment unsafe to reuse or summarize."""
    issues: list[str] = []
    if not transcript or not transcript.strip():
        return ["轉錄內容為空"]

    if is_last_segment is None:
        is_last_segment = segment_index >= total_segments - 1
    has_incomplete_marker = any(marker in transcript for marker in SEGMENT_INCOMPLETE_MARKERS)
    if has_incomplete_marker and not is_last_segment:
        issues.append("非最後分段含自動過濾/截斷提示")

    repetition_issue = _segment_repetition_quality_issue(transcript)
    if repetition_issue:
        issues.append(repetition_issue)

    structured_numeric_issue = _structured_numeric_turn_quality_issue(transcript)
    if structured_numeric_issue:
        issues.append(structured_numeric_issue)

    timestamps = [
        int(match.group("minutes")) * 60 + int(match.group("seconds"))
        for match in TIMESTAMP_PATTERN.finditer(transcript)
    ]
    expected_start = expected_start_seconds
    if expected_start is None:
        expected_start = segment_index * segment_minutes * 60
    expected_end = expected_end_seconds
    if expected_end is None:
        expected_end = (segment_index + 1) * segment_minutes * 60

    if timestamps:
        earliest_timestamp = min(timestamps)
        latest_timestamp = max(timestamps)
        tolerance = SEGMENT_TIMESTAMP_BOUNDARY_TOLERANCE_SECONDS
        if earliest_timestamp < expected_start - tolerance:
            issues.append(
                f"分段時間戳早於段首 {_format_mmss(expected_start)}："
                f"{_format_mmss(earliest_timestamp)}"
            )
        if latest_timestamp > expected_end + tolerance:
            issues.append(
                f"分段時間戳超過段尾 {_format_mmss(expected_end)}："
                f"{_format_mmss(latest_timestamp)}"
            )

    if is_last_segment:
        return issues

    if not timestamps:
        issues.append("非最後分段缺少時間戳")
        return issues

    latest_timestamp = max(timestamps)
    if latest_timestamp < expected_end - SEGMENT_COMPLETENESS_GRACE_SECONDS:
        issues.append(
            f"非最後分段時間戳只到 {_format_mmss(latest_timestamp)}，"
            f"未接近段尾 {_format_mmss(expected_end)}"
        )

    return issues


def _raise_if_segment_transcript_incomplete(
    transcript: str,
    segment_index: int,
    total_segments: int,
    segment_minutes: int = SEGMENT_MINUTES,
    expected_start_seconds: Optional[int] = None,
    expected_end_seconds: Optional[int] = None,
    is_last_segment: Optional[bool] = None,
) -> None:
    issues = _segment_transcript_quality_issues(
        transcript=transcript,
        segment_index=segment_index,
        total_segments=total_segments,
        segment_minutes=segment_minutes,
        expected_start_seconds=expected_start_seconds,
        expected_end_seconds=expected_end_seconds,
        is_last_segment=is_last_segment,
    )
    if issues:
        raise RuntimeError(
            f"第 {segment_index + 1}/{total_segments} 段轉錄不完整："
            + "；".join(issues)
        )


def _record_segment_reuse_blocking_issues(
    transcript: str,
    *,
    segment_index: int,
    total_segments: int,
    expected_start_seconds: int,
    expected_end_seconds: int,
) -> list[str]:
    """Return high-risk issues that make an old record unsafe to reuse.

    Legacy records may have sparse timestamps, so incompleteness alone remains
    reviewable. Hallucination markers and impossible time bounds must instead
    force a fresh transcription, even when that segment was not selected.
    """
    issues = _segment_transcript_quality_issues(
        transcript=transcript,
        segment_index=segment_index,
        total_segments=total_segments,
        segment_minutes=SEGMENT_MINUTES,
        expected_start_seconds=expected_start_seconds,
        expected_end_seconds=expected_end_seconds,
        is_last_segment=segment_index >= total_segments - 1,
    )
    blocking_markers = (
        "轉錄幻覺",
        "自動過濾/截斷",
        "早於段首",
        "超過段尾",
    )
    return [issue for issue in issues if any(marker in issue for marker in blocking_markers)]


def _safe_segment_cache_name(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", job_id) or "unknown-job"


def _segment_cache_dir(output_dir: Path, job_id: str) -> Path:
    return Path(output_dir) / SEGMENT_CACHE_DIRNAME / _safe_segment_cache_name(job_id)


def _segment_cache_file(output_dir: Path, job_id: str, segment_index: int) -> Path:
    return _segment_cache_dir(output_dir, job_id) / f"segment_{segment_index + 1:03d}.json"


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _shared_segment_cache_file(
    output_dir: Path,
    context: dict[str, Any],
    segment_index: int,
) -> Optional[Path]:
    source_sha256 = str(context.get("source_audio_sha256") or "").strip()
    model = re.sub(r"[^A-Za-z0-9_.-]", "_", str(context.get("model") or "model"))
    if not source_sha256:
        return None
    profile_data = {
        "version": context.get("cache_version", SEGMENT_CACHE_VERSION),
        "model": model,
        "source": source_sha256,
        "bounds": context.get("segment_bounds") or [],
        "preprocessing": context.get("audio_preprocessing_version"),
    }
    profile_hash = hashlib.sha256(
        json.dumps(profile_data, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    profile = f"{source_sha256[:16]}_{model}_{profile_hash}"
    return Path(output_dir) / SEGMENT_CACHE_DIRNAME / "shared" / profile / f"segment_{segment_index + 1:03d}.json"


def _segment_cache_context(
    audio_path: Path,
    model: str,
    total_segments: int,
    segment_minutes: int,
    segment_bounds: Optional[list[list[int]]] = None,
) -> dict[str, Any]:
    stat = audio_path.stat()
    try:
        source_path = str(audio_path.resolve())
    except OSError:
        source_path = str(audio_path.absolute())

    return {
        "cache_version": SEGMENT_CACHE_VERSION,
        "source_audio_path": source_path,
        "source_audio_name": audio_path.name,
        "source_audio_size": stat.st_size,
        "source_audio_mtime_ns": stat.st_mtime_ns,
        "source_audio_sha256": _sha256_file(audio_path),
        "model": model,
        "total_segments": total_segments,
        "segment_minutes": segment_minutes,
        "segment_bounds": segment_bounds or [],
        "audio_preprocessing_version": AUDIO_PREPROCESSING_VERSION,
    }


def _segment_cache_matches(
    payload: dict[str, Any],
    context: dict[str, Any],
    segment_index: int,
) -> bool:
    expected = dict(context)
    expected["segment_index"] = segment_index
    return all(payload.get(key) == value for key, value in expected.items())


def _load_segment_transcript_cache(
    output_dir: Path,
    job_id: str,
    segment_index: int,
    context: dict[str, Any],
) -> Optional[str]:
    cache_files = [_segment_cache_file(output_dir, job_id, segment_index)]
    shared_file = _shared_segment_cache_file(output_dir, context, segment_index)
    if shared_file is not None and shared_file not in cache_files:
        cache_files.append(shared_file)

    for cache_file in cache_files:
        if not cache_file.is_file():
            continue
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[%s] ⚠️  分段快取讀取失敗，將重新轉錄：%s", job_id, exc)
            continue

        if not isinstance(payload, dict) or not _segment_cache_matches(payload, context, segment_index):
            logger.info("[%s] ♻️  分段 %s 快取與目前音檔/模型不符，略過", job_id, segment_index + 1)
            continue

        transcript = payload.get("transcript")
        if not isinstance(transcript, str):
            continue
        bounds = context.get("segment_bounds") or []
        expected_start = None
        expected_end = None
        if segment_index < len(bounds) and len(bounds[segment_index]) >= 2:
            expected_start = int(bounds[segment_index][0])
            expected_end = int(bounds[segment_index][1])
        issues = _segment_transcript_quality_issues(
            transcript=transcript,
            segment_index=segment_index,
            total_segments=int(context.get("total_segments") or 1),
            segment_minutes=int(context.get("segment_minutes") or SEGMENT_MINUTES),
            expected_start_seconds=expected_start,
            expected_end_seconds=expected_end,
        )
        if issues:
            logger.warning(
                "[%s] ⚠️  第 %s 段快取不完整，將重新轉錄：%s",
                job_id,
                segment_index + 1,
                "；".join(issues),
            )
            try:
                cache_file.unlink()
            except OSError:
                pass
            continue
        if cache_file == shared_file:
            logger.info("[%s] ♻️  第 %s 段使用相同音檔的共用快取", job_id, segment_index + 1)
        return transcript
    return None


def _save_segment_transcript_cache(
    output_dir: Path,
    job_id: str,
    segment_index: int,
    context: dict[str, Any],
    transcript: str,
) -> Path:
    payload = {
        **context,
        "segment_index": segment_index,
        "transcript": transcript,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cache_files = [_segment_cache_file(output_dir, job_id, segment_index)]
    shared_file = _shared_segment_cache_file(output_dir, context, segment_index)
    if shared_file is not None and shared_file not in cache_files:
        cache_files.append(shared_file)

    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    for cache_file in cache_files:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = cache_file.with_suffix(".tmp")
        temp_file.write_text(serialized, encoding="utf-8")
        temp_file.replace(cache_file)
    return cache_files[0]


SUPPORTED_MEDIA_FORMATS = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
}

MULTILINGUAL_TRANSCRIPT_POLICY = """
【多語言處理規則】
- 摘要、決議與待辦事項仍統一使用繁體中文。
- 完整逐字稿需忠實呈現語言切換，不要把所有發言一律翻成同一種語言。
- 英文發言請保留英文原文；若句子較長，請在同段後方補上繁體中文翻譯，例如 `（中譯：...）`。
- 中文國語發言請以繁體中文轉寫。
- 台語發言請標記為 `[台語]`，並以繁體中文做語意轉寫；不要硬湊不確定的台語漢字。
- 台語聽不清楚時，請在對應位置標記 `[台語音訊不清晰]`。
- 人名、公司名、產品名、技術名詞與英文縮寫請盡量保留原文；必要時在後方補中文說明。
""".strip()


SPEAKER_DIFFERENTIATION_POLICY = """
【發言者辨識規則】
- 目標是分辨「不同聲音」，不是猜測真實姓名；除非音訊中明確自我介紹或互稱姓名，否則一律使用匿名標籤。
- 使用固定格式 **[發言者 A]**：、**[發言者 B]**：、**[發言者 C]**：；同一個聲音再次出現時必須沿用相同標籤。
- 聽到新的不同聲音時，依序新增下一個標籤；不要把不同人的發言合併成同一位。
- 若一小段無法判斷是誰，但可辨識內容，標示為 **[發言者不明]**：；不要為了填滿而硬分派。
- 若多人同時說話，標示為 **[多人重疊]**：並盡量轉寫可辨識內容。
""".strip()


DOMAIN_TERMINOLOGY_POLICY = """
【久方醫材研發術語表】
- 「佳世達」為正確名稱，英文可標為 Qisda；請勿寫成「加斯達」、「嘉士達」或 Jasta。
- IEC 62304 為醫療器材軟體生命週期流程標準；請勿寫成 IEC 6304 或 IC6304。
- 研發、製造、品保討論中常見「治具、放電治具、自製治具、品保、品管、機械老化、頻率/振幅、內徑固定塊」；請勿寫成「字句、自句、平保、平寶、氣械、政府」等語音誤聽。
- ISO 13485、FDA eSTAR、URA、SRS、SDS、SAD、SIS、traceability matrix、DHF、DMR、P4/P5/P6、Q0/Q4 請保留原文或常用縮寫。
- 久方生技 / Maxima Biotech 的研發會議若提及供應商、法規、設計階段、驗證報告與送件時程，摘要與待辦需保留日期、負責人與風險。
""".strip()


MEDICAL_DEVICE_RND_ANALYSIS_POLICY = """
【醫材研發會議判讀規則】
- 討論摘要需依「專案/議題」分組，每點包含目前狀態、卡點/風險、下一步/期限；FDA、IEC、ISO、QMS、設計移轉、驗證與送件內容不可簡化成一般進度描述。
- 最終決議只放已確認的日期、做法、採用/不採用、責任分工或風險處置；追蹤目標、個人建議、教學說明與背景知識不得列為決議。
- 待辦事項只放可驗收行動；任務描述要能被完成與檢查，避免「處理文件」「撰寫軟體工程」等大包任務，應拆成 SRS、SDS、SAD、traceability matrix、驗證計畫、RA 法規導入單等具體輸出物。
- 若逐字稿出現系統提示、雜訊過濾、片段缺漏或聽不清，需在討論摘要第一段加入「逐字稿品質註記」，標示可能缺漏與需複核。
""".strip()


TERMINOLOGY_REPLACEMENTS = (
    ("加斯達", "佳世達"),
    ("嘉士達", "佳世達"),
    ("Jasta", "Qisda"),
    ("平保", "品保"),
    ("平寶", "品保"),
    ("平管", "品管"),
    ("氣械老化", "機械老化"),
    ("頻率政府", "頻率振幅"),
    ("內型固定塊", "內徑固定塊"),
    ("內心固定塊", "內徑固定塊"),
    ("IEC 6304", "IEC 62304"),
    ("IEC6304", "IEC 62304"),
    ("IC 6304", "IEC 62304"),
    ("IC6304", "IEC 62304"),
)


TERMINOLOGY_REGEX_REPLACEMENTS = (
    (r"(?<!文件)(?<!文字)(?<!條文)字句", "治具"),
    (r"自製具", "自製治具"),
    (r"自句", "治具"),
)


MEETING_PROMPT = f"""
# 角色設定
你是一位擁有 15 年經驗的國際企業專業高階秘書（Executive Secretary），
精通會議記錄、商業寫作與多語言溝通。你的任務是分析上方的音訊會議內容，
並生成一份格式完整、語意精確的專業會議記錄文件。

# 輸出要求
請嚴格按照以下四個區塊輸出，使用 **繁體中文**，並保持 Markdown 格式：

{MULTILINGUAL_TRANSCRIPT_POLICY}

{SPEAKER_DIFFERENTIATION_POLICY}

{DOMAIN_TERMINOLOGY_POLICY}

{MEDICAL_DEVICE_RND_ANALYSIS_POLICY}

---

## 📋 一、討論摘要 (Discussion Summary)
請依專案或議題分組，整理各方提出的關鍵意見、時程、卡點、風險與下一步，幫助讀者快速掌握會議脈絡。
若有多個討論項目，請以 D1、D2、D3... 表示。

---

## ✅ 二、最終決議 (Final Decisions)
請清楚寫下經過討論後已確認的共識或結論；不要把追蹤目標、背景說明或教學內容列為決議。
如果某個議題沒有結論，也應明確註記「尚未決定」或「需延至下次討論」。
每項決議請以 R1、R2、R3... 表示，並標明關聯討論 D 編號。

---

## 📌 三、待辦事項 (Action Items)
請以表格呈現所有被提及的任務、負責人與期限：

| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|---------|---------|--------|------|--------|
| A1 | D1 | R1 | [任務內容] | [姓名/部門] | [日期或「未定」] | 高/中/低 |

若未明確提及負責人或期限，請填入「未明確指定」或「未提及」。
若只能辨識到匿名發言者，負責人請保留匿名標籤，例如「發言者 A」，不要自行推測姓名。
若任務過大，請拆成可驗收的文件、測試、追蹤或會議安排項目。

---

## 📝 四、完整逐字稿 (Verbatim Transcript)
請提供完整逐字稿。請「嚴格」遵守以下排版規則：

【嚴格排版格式要求】
1. 每當「發言者更換」或是「同一人發言超過3句話」時，**必須強制換段落**。
2. 每一個新段落的最前面，**必須強制標註發言者**（如 **[發言者 A]**：、**[發言者 B]**：；只有明確聽到姓名時才可使用姓名）。
3. 絕對不可將不同人的對話、或過長的單人發言混在同一大段中。
4. 每隔 30-60 秒，在段落開頭加上時間戳記。

範例格式：
[00:00] **[發言者 A]**：大家好，今天開會主要討論明年的預算。
**[發言者 A]**：這部分的重點在於...

[00:45] **[發言者 B]**：我認為這個部分需要再確認。

> ⚠️ 注意事項：
> 1. 逐字稿應盡量完整，保留語氣詞，不要省略或摘要化。
> 2. **嚴格禁止重複迴圈**：遇到無聲、背景音樂、雜訊或音檔結束時，請直接結束輸出。絕對不要反覆輸出相同的單字。
"""


def _extract_summary_preview(content: str, max_chars: int = 200) -> str:
    """從完整 Markdown 內容中提取摘要段落的前 N 個字"""
    try:
        match = re.search(
            r"##\s*📋\s*一、[^\n]*\n(?P<body>.*?)(?=\n##\s*✅|\Z)",
            content,
            flags=re.DOTALL,
        )
        if match:
            excerpt = match.group("body").strip()
            return excerpt[:max_chars]
    except Exception:
        pass
    return content[:max_chars]


def _meeting_content_quality_issues(content: str) -> list[str]:
    """Return structural issues that would make a meeting note unsafe to save."""
    issues: list[str] = []
    section_patterns = [
        ("缺少討論摘要區塊", r"##\s*📋\s*一、"),
        ("缺少最終決議區塊", r"##\s*✅\s*二、"),
        ("缺少待辦事項區塊", r"##\s*📌\s*三、"),
        ("缺少完整逐字稿區塊", r"##\s*📝\s*四、"),
    ]
    for issue, pattern in section_patterns:
        if not re.search(pattern, content):
            issues.append(issue)

    action_match = re.search(
        r"##\s*📌\s*三、[^\n]*\n(?P<body>.*?)(?=\n##\s*📝|\Z)",
        content,
        flags=re.DOTALL,
    )
    if action_match:
        lines = [
            line.strip()
            for line in action_match.group("body").splitlines()
            if line.strip()
        ]
        header_index = next(
            (
                i
                for i, line in enumerate(lines)
                if all(label in line for label in ("任務描述", "負責人", "期限", "優先級"))
            ),
            None,
        )
        if header_index is None:
            issues.append("待辦事項表格缺少標題列")
        elif header_index + 1 >= len(lines):
            issues.append("待辦事項表格缺少分隔列")
        else:
            separator_cells = [
                cell.strip()
                for cell in lines[header_index + 1].strip("|").split("|")
            ]
            separator_is_complete = (
                len(separator_cells) >= 5
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator_cells[:5])
            )
            if not separator_is_complete:
                issues.append("待辦事項表格分隔列不完整")
            elif header_index + 2 >= len(lines) or not lines[header_index + 2].startswith("|"):
                issues.append("待辦事項表格缺少內容列")

    transcript_match = re.search(
        r"##\s*📝\s*四、[^\n]*\n(?P<body>.*)",
        content,
        flags=re.DOTALL,
    )
    if transcript_match and not transcript_match.group("body").strip():
        issues.append("完整逐字稿區塊沒有內容")

    return issues


def _extract_summary_preview_v2(content: str, max_chars: int = 200) -> str:
    text = content or ""
    patterns = [
        r"##\s*[^\n]*(?:Discussion Summary|討論摘要)[^\n]*\n(?P<body>.*?)(?=\n##\s*[^\n]*(?:Final Decisions|最終決議)|\n---|\Z)",
        r"##\s*??\s*銝?^\n]*\n(?P<body>.*?)(?=\n##\s*?\Z)",
    ]
    for pattern in patterns:
        try:
            match = re.search(pattern, text, flags=re.DOTALL)
            if match:
                excerpt = re.sub(r"\s+", " ", match.group("body")).strip()
                return excerpt[:max_chars]
        except re.error:
            continue
    return text[:max_chars]


def _meeting_content_quality_issues_v2(content: str) -> list[str]:
    text = content or ""
    issues: list[str] = []
    required_sections = [
        ("缺少討論摘要區塊", r"##\s*[^\n]*(?:Discussion Summary|討論摘要|銝)"),
        ("缺少最終決議區塊", r"##\s*[^\n]*(?:Final Decisions|最終決議|鈭)"),
        ("缺少待辦事項區塊", r"##\s*[^\n]*(?:Action Items|待辦事項|銝)"),
        ("缺少完整逐字稿區塊", r"##\s*[^\n]*(?:Verbatim Transcript|完整逐字稿|逐字稿|蝔)"),
    ]
    for issue, pattern in required_sections:
        if not re.search(pattern, text, flags=re.IGNORECASE):
            issues.append(issue)

    action_match = re.search(
        r"##\s*[^\n]*(?:Action Items|待辦事項|銝)[^\n]*\n(?P<body>.*?)(?=\n##|\Z)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if action_match:
        action_body = action_match.group("body")
        table_lines = [
            line.strip()
            for line in action_body.splitlines()
            if line.strip().startswith("|") and line.strip().endswith("|")
        ]
        header_index = next(
            (
                index
                for index, line in enumerate(table_lines)
                if re.search(r"(任務描述|隞餃|\btask\b)", line, flags=re.IGNORECASE)
            ),
            None,
        )
        if header_index is None or header_index + 1 >= len(table_lines):
            issues.append("待辦事項表格分隔列不完整")
        else:
            separator_cells = [
                cell.strip()
                for cell in table_lines[header_index + 1].strip("|").split("|")
            ]
            if len(separator_cells) < 5 or not all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in separator_cells[:5]
            ):
                issues.append("待辦事項表格分隔列不完整")
    elif "缺少待辦事項區塊" not in issues:
        issues.append("缺少待辦事項區塊內容")

    transcript_match = re.search(
        r"##\s*[^\n]*(?:Verbatim Transcript|完整逐字稿|逐字稿|蝔)[^\n]*\n(?P<body>.*)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if transcript_match and not transcript_match.group("body").strip():
        issues.append("完整逐字稿區塊內容空白")

    return issues


_extract_summary_preview = _extract_summary_preview_v2
_meeting_content_quality_issues = _meeting_content_quality_issues_v2


def _finalize_meeting_content(meeting_content: str, full_transcript: str, job_id: str) -> str:
    """Apply final transcript preservation and fail closed on unsafe output."""
    finalized = _replace_transcript_section(meeting_content, full_transcript)
    finalized = _prepend_transcript_quality_notice(finalized, full_transcript)

    issues = [
        *_meeting_content_quality_issues(finalized),
        *_transcript_integrity_issues(finalized, full_transcript),
    ]
    if issues:
        raise RuntimeError("會議記錄最終品質檢查失敗：" + "；".join(dict.fromkeys(issues)))

    logger.info("[%s] ✅ 會議記錄最終品質檢查通過", job_id)
    return finalized


def _repair_meeting_content_if_needed(
    client,
    model: str,
    meeting_content: str,
    job_id: str,
    fallback_model: Optional[str] = None,
) -> str:
    """Ask Gemini once to repair malformed meeting Markdown before saving it."""
    issues = _meeting_content_quality_issues(meeting_content)
    if not issues:
        return meeting_content

    logger.warning("[%s] ⚠️  會議記錄結構需修復：%s", job_id, "；".join(issues))
    repair_prompt = f"""
以下是一份 AI 生成的會議記錄 Markdown，但結構不完整或表格格式損壞。
請只修復格式與缺漏區塊，不要杜撰未出現在原文中的事實。

{DOMAIN_TERMINOLOGY_POLICY}

{MEDICAL_DEVICE_RND_ANALYSIS_POLICY}

必須輸出以下四個區塊，且只輸出 Markdown：
## 📋 一、討論摘要 (Discussion Summary)
## ✅ 二、最終決議 (Final Decisions)
## 📌 三、待辦事項 (Action Items)
## 📝 四、完整逐字稿 (Verbatim Transcript)

待辦事項必須是完整 Markdown 表格，欄位必須為：
| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|---------|---------|--------|------|--------|

討論摘要若有多個議題，請使用 D1、D2、D3... 分段。
最終決議請使用 R1、R2、R3...，並在內容或表格中標明關聯的 D 編號。
待辦事項請使用 A1、A2、A3...，並保留關聯討論與關聯決議欄位。
若沒有待辦事項，請保留表格並填入一列「A1 | 未提及 | 未提及 | 未提及 | 未提及 | 未提及 | 中」。
若逐字稿缺漏，請依現有內容保守整理；不可新增不存在的發言。
完整逐字稿區塊只能原樣保留或補上缺少標題，不可摘要、改寫、刪減、合併或加入「為節省篇幅」等省略說明。

已知問題：
{chr(10).join(f"- {issue}" for issue in issues)}

原始 Markdown：
{meeting_content}
""".strip()

    response, used_model = _generate_text_with_fallback(
        client,
        primary_model=model,
        fallback_model=fallback_model or model,
        contents=[repair_prompt],
        config=types.GenerateContentConfig(
            temperature=0.0,
            top_p=0.8,
            max_output_tokens=65536,
        ),
        job_id=job_id,
        stage="會議記錄結構修復",
    )
    repaired = _normalize_domain_terms(clean_hallucinated_loops(response.text or ""))
    repaired_issues = _meeting_content_quality_issues(repaired)
    if repaired_issues:
        raise RuntimeError(
            "AI 輸出結構修復後仍不完整：" + "；".join(repaired_issues)
        )
    logger.info("[%s] ✅ 會議記錄結構已自動修復（模型：%s）", job_id, used_model)
    return repaired


def _prepare_audio_for_transcription(
    audio_path: Path,
    temp_dir: Path,
    job_id: str,
) -> tuple[Path, dict[str, Any]]:
    """Inspect audio locally and normalize only recordings that are unusually quiet."""
    _configure_ffmpeg_tools()
    from pydub import AudioSegment, effects, silence

    ffmpeg_path = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BINARY")
    if ffmpeg_path and Path(ffmpeg_path).is_file():
        AudioSegment.converter = ffmpeg_path

    try:
        with audio_path.open("rb") as source_handle:
            audio = AudioSegment.from_file(
                source_handle,
                format=audio_path.suffix.lower().lstrip(".") or None,
            )
    except Exception as exc:
        logger.warning(
            "[%s] ⚠️  本機音訊預檢無法解碼，沿用原檔交由既有流程處理：%s",
            job_id,
            str(exc).splitlines()[0],
        )
        return audio_path, {
            "duration_seconds": None,
            "channels": None,
            "sample_rate": None,
            "average_dbfs": None,
            "max_dbfs": None,
            "silence_ratio": None,
            "preprocessed": False,
            "warnings": ["本機音訊預檢無法解碼，已沿用原檔。"],
        }
    duration_seconds = max(1, math.ceil(len(audio) / 1000))
    dbfs = float(audio.dBFS)
    max_dbfs = float(audio.max_dBFS)
    report: dict[str, Any] = {
        "duration_seconds": duration_seconds,
        "channels": audio.channels,
        "sample_rate": audio.frame_rate,
        "average_dbfs": round(dbfs, 1) if math.isfinite(dbfs) else None,
        "max_dbfs": round(max_dbfs, 1) if math.isfinite(max_dbfs) else None,
        "preprocessed": False,
        "warnings": [],
    }
    try:
        file_size_bytes = audio_path.stat().st_size
        report["file_size_bytes"] = file_size_bytes
        report["estimated_bitrate_kbps"] = round(
            file_size_bytes * 8 / max(1, duration_seconds) / 1000,
            1,
        )
    except OSError:
        report["file_size_bytes"] = None
        report["estimated_bitrate_kbps"] = None

    if audio.channels > 2:
        report["warnings"].append("錄音聲道多於 2 聲道，會增加容量但不一定提升會議轉錄品質。")
    if audio.frame_rate < 12_000:
        report["warnings"].append("取樣率低於 12 kHz，可能影響人聲與專有名詞辨識。")

    if (
        not math.isfinite(dbfs)
        or (
            dbfs <= AUDIO_MIN_DBFS
            and (not math.isfinite(max_dbfs) or max_dbfs <= -40.0)
        )
    ):
        raise RuntimeError("音訊幾乎沒有可辨識聲音，已停止送出模型以避免浪費免費額度。")
    if dbfs <= AUDIO_MIN_DBFS:
        report["warnings"].append("錄音平均音量極低，但仍偵測到可辨識峰值，已保守繼續處理。")

    silence_threshold = max(-55.0, min(-32.0, dbfs - 16.0))
    silent_ranges = silence.detect_silence(
        audio,
        min_silence_len=500,
        silence_thresh=silence_threshold,
        seek_step=100,
    )
    silent_ms = sum(max(0, end - start) for start, end in silent_ranges)
    silence_ratio = min(1.0, silent_ms / max(1, len(audio)))
    report["silence_ratio"] = round(silence_ratio, 3)
    if duration_seconds >= 30 and silence_ratio >= 0.995:
        raise RuntimeError("音訊有 99.5% 以上為靜音，已停止送出模型以避免浪費免費額度。")

    if math.isfinite(max_dbfs) and max_dbfs >= -0.1:
        report["warnings"].append("偵測到可能的爆音；原始媒體檔已保留，重要內容請抽查。")

    if not AUDIO_PREPROCESSING_ENABLED or dbfs >= AUDIO_NORMALIZE_BELOW_DBFS:
        return audio_path, report

    cleaned = effects.normalize(audio.high_pass_filter(70), headroom=1.5)
    temp_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = temp_dir / f"_prepared_{_safe_segment_cache_name(job_id)}.mp3"
    cleaned.export(str(prepared_path), format="mp3", parameters=["-q:a", "2"])
    report["preprocessed"] = True
    report["warnings"].append("原錄音音量偏低，轉錄時已使用本機正規化副本。")
    logger.info(
        "[%s] 🎚️  音量偏低（%.1f dBFS），已建立本機正規化轉錄副本",
        job_id,
        dbfs,
    )
    return prepared_path, report


def _smart_segment_boundaries(audio, segment_ms: int) -> list[int]:
    """Choose cuts near quiet passages while keeping segments close to the target size."""
    from pydub import silence

    duration_ms = len(audio)
    if duration_ms <= segment_ms:
        return [0, duration_ms]

    search_ms = max(0, SEGMENT_SILENCE_WINDOW_SECONDS) * 1000
    threshold = max(-55.0, min(-32.0, float(audio.dBFS) - 14.0))
    boundaries = [0]
    while boundaries[-1] + segment_ms < duration_ms:
        target = boundaries[-1] + segment_ms
        window_start = max(boundaries[-1] + segment_ms // 2, target - search_ms)
        window_end = min(duration_ms, target + search_ms)
        quiet_ranges = silence.detect_silence(
            audio[window_start:window_end],
            min_silence_len=350,
            silence_thresh=threshold,
            seek_step=25,
        )
        candidates = [window_start + (start + end) // 2 for start, end in quiet_ranges]
        cut = min(candidates, key=lambda value: abs(value - target)) if candidates else target
        if cut <= boundaries[-1] or duration_ms - cut < 1000:
            cut = target
        boundaries.append(cut)
    boundaries.append(duration_ms)
    return boundaries


def _split_audio_to_segments(audio_path: Path, segment_minutes: int = 10) -> list[AudioSlice]:
    """
    將音訊檔切割成等長分段。若切割失敗（pydub 未安裝等），回傳原始路徑。

    Args:
        audio_path:       完整音訊路徑
        segment_minutes:  每段長度（分鐘）

    Returns:
        list[Path]: 每個分段的暫存路徑（若無需切割則只有一個元素）
    """
    try:
        _configure_ffmpeg_tools()
        from pydub import AudioSegment

        ffmpeg_path = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BINARY")
        if ffmpeg_path and Path(ffmpeg_path).is_file():
            AudioSegment.converter = ffmpeg_path

        audio = AudioSegment.from_file(str(audio_path))
        duration_ms = len(audio)
        segment_ms = segment_minutes * 60 * 1000

        if duration_ms <= segment_ms:
            return [AudioSlice(audio_path, 0, max(1, math.ceil(duration_ms / 1000)))]

        segments: list[AudioSlice] = []
        base = audio_path.parent / f"_seg_{audio_path.stem}"
        base.parent.mkdir(parents=True, exist_ok=True)
        boundaries = _smart_segment_boundaries(audio, segment_ms)
        overlap_ms = max(0, SEGMENT_OVERLAP_SECONDS) * 1000

        for i, (boundary_start, boundary_end) in enumerate(zip(boundaries, boundaries[1:])):
            start = max(0, boundary_start - overlap_ms) if i else 0
            chunk = audio[start:boundary_end]
            seg_path = audio_path.parent / f"_seg_{audio_path.stem}_{i:03d}.mp3"
            chunk.export(str(seg_path), format="mp3", parameters=["-q:a", "3"])
            segments.append(
                AudioSlice(
                    path=seg_path,
                    start_seconds=start // 1000,
                    end_seconds=max(1, math.ceil(boundary_end / 1000)),
                )
            )

        logger.info(
            "🔪 音訊已依靜音位置切割為 %s 段（目標 %s 分鐘，重疊 %s 秒）",
            len(segments),
            segment_minutes,
            SEGMENT_OVERLAP_SECONDS,
        )
        return segments
    except ImportError:
        logger.warning("⚠️  pydub 未安裝，無法切割音訊，將以整體方式送出")
        return [AudioSlice(audio_path, 0, SEGMENT_TARGET_SECONDS)]
    except Exception as e:
        logger.warning(f"⚠️  音訊切割失敗（{e}），改以整體方式送出")
        return [AudioSlice(audio_path, 0, SEGMENT_TARGET_SECONDS)]


def _coerce_audio_slices(items: list[Any]) -> list[AudioSlice]:
    """Keep compatibility with tests and integrations that still return plain paths."""
    slices: list[AudioSlice] = []
    for index, item in enumerate(items):
        if isinstance(item, AudioSlice):
            slices.append(item)
            continue
        start = index * SEGMENT_TARGET_SECONDS
        slices.append(AudioSlice(Path(item), start, start + SEGMENT_TARGET_SECONDS))
    return slices


def _split_audio_to_subsegments(audio_path: Path, chunk_seconds: int) -> list[tuple[Path, int, int]]:
    """
    將已切出的分段再切成更小段，回傳 (路徑, 起始秒, 結束秒)。
    這個函式用於轉錄補救；切割失敗時讓呼叫端保留原本的完整性錯誤。
    """
    _configure_ffmpeg_tools()
    from pydub import AudioSegment

    ffmpeg_path = os.getenv("FFMPEG_PATH") or os.getenv("FFMPEG_BINARY")
    if ffmpeg_path and Path(ffmpeg_path).is_file():
        AudioSegment.converter = ffmpeg_path

    audio = AudioSegment.from_file(str(audio_path))
    duration_ms = len(audio)
    chunk_ms = max(1, chunk_seconds) * 1000
    if duration_ms <= chunk_ms:
        end_seconds = max(1, (duration_ms + 999) // 1000)
        return [(audio_path, 0, end_seconds)]

    # Split evenly instead of leaving a tiny final chunk (for example,
    # 603 seconds used to become 300 + 300 + 3). Very short tails are highly
    # prone to semantic continuation when an STT model receives prior context.
    part_count = max(2, math.ceil(duration_ms / chunk_ms))
    boundaries = [round(index * duration_ms / part_count) for index in range(part_count + 1)]
    subsegments: list[tuple[Path, int, int]] = []
    for i, (start_ms, end_ms) in enumerate(zip(boundaries, boundaries[1:])):
        chunk = audio[start_ms:end_ms]
        sub_path = audio_path.parent / f"_sub_{audio_path.stem}_{chunk_seconds}s_{i:03d}.mp3"
        chunk.export(str(sub_path), format="mp3", parameters=["-q:a", "3"])
        subsegments.append((sub_path, start_ms // 1000, max(1, (end_ms + 999) // 1000)))

    logger.info(
        "🔪 補救切段：%s 已切成 %s 個小段（每段約 %s 秒）",
        audio_path.name,
        len(subsegments),
        chunk_seconds,
    )
    return subsegments


def _next_recovery_chunk_seconds(duration_seconds: int) -> Optional[int]:
    for chunk_seconds in SEGMENT_RECOVERY_SPLIT_SECONDS:
        if chunk_seconds < duration_seconds:
            return chunk_seconds
    return None


def _transcribe_segment_with_recovery(
    client,
    seg_path: Path,
    seg_index: int,
    total_segs: int,
    job_id: str,
    model: str,
    *,
    offset_seconds: int,
    duration_seconds: int,
    is_last_segment: bool,
    speaker_context: str = "",
    temp_segment_paths: Optional[list[Path]] = None,
    quality_events: Optional[list[dict[str, Any]]] = None,
) -> str:
    transcript = _transcribe_segment(
        client,
        seg_path,
        seg_index,
        total_segs,
        job_id,
        model,
        speaker_context=speaker_context,
    )
    transcript = _offset_transcript_timestamps(transcript, offset_seconds)

    try:
        _raise_if_segment_transcript_incomplete(
            transcript=transcript,
            segment_index=seg_index,
            total_segments=total_segs,
            segment_minutes=SEGMENT_MINUTES,
            expected_start_seconds=offset_seconds,
            expected_end_seconds=offset_seconds + duration_seconds,
            is_last_segment=is_last_segment,
        )
        return transcript
    except RuntimeError as quality_error:
        if quality_events is not None:
            quality_events.append({
                "segment_index": seg_index,
                "start_seconds": offset_seconds,
                "end_seconds": offset_seconds + duration_seconds,
                "issue": str(quality_error),
            })
        chunk_seconds = _next_recovery_chunk_seconds(duration_seconds)
        if chunk_seconds is None:
            raise

        logger.warning(
            "[%s] ⚠️ 第 %s/%s 段轉錄不完整，改切成約 %s 秒小段補救：%s",
            job_id,
            seg_index + 1,
            total_segs,
            chunk_seconds,
            quality_error,
        )
        update_job_status(
            job_id,
            "processing",
            f"🔁 第 {seg_index + 1}/{total_segs} 段轉錄不完整，改切成小段重試...",
            progress_current=seg_index,
            progress_total=total_segs,
        )

        try:
            subsegments = _split_audio_to_subsegments(seg_path, chunk_seconds)
        except Exception as split_error:
            logger.warning(
                "[%s] ⚠️ 第 %s/%s 段補救切段失敗：%s",
                job_id,
                seg_index + 1,
                total_segs,
                split_error,
            )
            raise quality_error

        if len(subsegments) <= 1:
            raise quality_error

        recovered: list[str] = []
        for sub_index, (sub_path, start_seconds, end_seconds) in enumerate(subsegments):
            _raise_if_cancelled(job_id)
            if (
                temp_segment_paths is not None
                and sub_path != seg_path
                and sub_path not in temp_segment_paths
            ):
                temp_segment_paths.append(sub_path)

            update_job_status(
                job_id,
                "processing",
                f"📝 正在補救轉錄第 {seg_index + 1}/{total_segs} 段的小段 {sub_index + 1}/{len(subsegments)}...",
                progress_current=seg_index,
                progress_total=total_segs,
            )
            child_context = _speaker_context_from_transcripts([speaker_context, *recovered])
            child_transcript = _transcribe_segment_with_recovery(
                client,
                sub_path,
                seg_index,
                total_segs,
                job_id,
                model,
                offset_seconds=offset_seconds + start_seconds,
                duration_seconds=max(1, end_seconds - start_seconds),
                is_last_segment=is_last_segment and sub_index == len(subsegments) - 1,
                speaker_context=child_context,
                temp_segment_paths=temp_segment_paths,
                quality_events=quality_events,
            )
            recovered.append(child_transcript)

        recovered_transcript = _sort_transcript_blocks_by_timestamp(
            "\n\n".join(part.strip() for part in recovered if part.strip())
        )
        _raise_if_segment_transcript_incomplete(
            transcript=recovered_transcript,
            segment_index=seg_index,
            total_segments=total_segs,
            segment_minutes=SEGMENT_MINUTES,
            expected_start_seconds=offset_seconds,
            expected_end_seconds=offset_seconds + duration_seconds,
            is_last_segment=is_last_segment,
        )
        return recovered_transcript


def _transcribe_segment(
    client,
    seg_path: Path,
    seg_index: int,
    total_segs: int,
    job_id: str,
    model: str,
    speaker_context: str = "",
) -> str:
    """上傳單一分段並請 Gemini 輸出逐字稿（純文字，不含摘要）"""

    SEGMENT_PROMPT = _build_segment_prompt(seg_index, total_segs, speaker_context=speaker_context)

    mime = SUPPORTED_MEDIA_FORMATS.get(seg_path.suffix.lower(), "audio/mpeg")
    uploaded = client.files.upload(
        file=str(seg_path),
        config=types.UploadFileConfig(display_name=seg_path.name, mime_type=mime)
    )

    # 等待處理就緒
    elapsed = 0
    while not uploaded.state or uploaded.state.name == "PROCESSING":
        _raise_if_cancelled(job_id)
        if elapsed >= MAX_UPLOAD_WAIT_SECONDS:
            raise RuntimeError(f"分段 {seg_index + 1} 媒體處理逾時")
        time.sleep(POLLING_INTERVAL)
        elapsed += POLLING_INTERVAL
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state.name == "FAILED":
        raise RuntimeError(f"分段 {seg_index + 1} 媒體處理失敗")

    _raise_if_cancelled(job_id)
    response = client.models.generate_content(
        model=model,
        contents=[uploaded, SEGMENT_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0.1,
            top_p=0.9,
            max_output_tokens=65536,
        )
    )

    # 清除雲端暫存
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    raw_text = response.text or ""
    return _normalize_domain_terms(clean_hallucinated_loops(raw_text))


def _build_segment_prompt(seg_index: int, total_segs: int, speaker_context: str = "") -> str:
    prompt = f"""
請聽這段音訊分段（第 {seg_index + 1} 段，共 {total_segs} 段）並進行轉錄。
請直接輸出這段音訊的逐字稿內容，不需加上標題。

{MULTILINGUAL_TRANSCRIPT_POLICY}

{SPEAKER_DIFFERENTIATION_POLICY}

{DOMAIN_TERMINOLOGY_POLICY}

【嚴格排版格式要求】
1. 每當「發言者更換」或是「同一人發言超過3句話」時，**必須強制換段落**。
2. 每一個新段落的最前面，**必須強制標註發言者**（如 **[發言者 A]**：、**[發言者 B]**：；只有明確聽到姓名時才可使用姓名）。
3. 絕對不可將不同人的對話、或過長的單人發言混在同一大段中。
4. 每隔 30-60 秒，在段落開頭加上時間戳記（相對於本段開始）。

範例格式：
[00:00] **[發言者 A]**：這部分的重點在於...
**[發言者 A]**：還有就是行銷費用的拿捏。

[00:45] **[發言者 B]**：我認為這個部分需要再確認。

> ⚠️ 注意事項：
> 1. 逐字稿應盡量完整，保留語氣詞，不要省略或摘要化。
> 2. **嚴格禁止重複迴圈**：遇到無聲、背景音樂、雜訊或音檔結束時，請直接結束輸出。絕對不要反覆輸出相同的單字。
""".strip()
    if speaker_context.strip():
        prompt += (
            "\n\n# Cross-segment speaker continuity\n"
            "Use the prior speaker context below only to keep anonymous labels stable across chunks. "
            "If the same voice continues, reuse the same label. If uncertain, use an unknown-speaker label. "
            "The context contains labels only: never infer, continue, paraphrase, or copy prior utterances.\n\n"
            f"{speaker_context.strip()}"
        )
    return prompt


def _coerce_summary_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _plain_markdown_cell(value: Any, default: str = "未提及") -> str:
    if value is None:
        text = ""
    elif isinstance(value, list):
        text = "、".join(_plain_markdown_cell(item, "") for item in value)
    elif isinstance(value, dict):
        text = "；".join(
            f"{key}: {_plain_markdown_cell(val, '')}"
            for key, val in value.items()
            if val not in (None, "", [])
        )
    else:
        text = str(value)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = text.replace("|", "／").replace("\r", " ").replace("\n", "<br>")
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def _summary_item_id(item: Any, prefix: str, index: int) -> str:
    if isinstance(item, dict):
        raw = item.get("id") or item.get(f"{prefix.lower()}_id") or item.get("number")
        if raw not in (None, "", []):
            text = _plain_markdown_cell(raw, "")
            match = re.search(r"\d+", text)
            if match:
                return f"{prefix}{int(match.group(0))}"
            cleaned = re.sub(r"[^A-Za-z0-9_-]", "", text).upper()
            if cleaned:
                return cleaned
    return f"{prefix}{index}"


def _summary_reference_ids(value: Any, prefix: str, default: str = "未提及") -> str:
    if value in (None, "", []):
        return default
    if isinstance(value, str):
        candidates = re.split(r"[,，、;/\s]+", value.strip())
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]

    refs: list[str] = []
    for candidate in candidates:
        text = _plain_markdown_cell(candidate, "")
        if not text:
            continue
        for match in re.finditer(r"[A-Za-z]*\s*(\d+)", text):
            ref = f"{prefix}{int(match.group(1))}"
            if ref not in refs:
                refs.append(ref)
        if not re.search(r"\d+", text):
            cleaned = re.sub(r"[^A-Za-z0-9_-]", "", text).upper()
            if cleaned and cleaned not in refs:
                refs.append(cleaned)
    return "、".join(refs) if refs else default


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    candidate = fence_match.group(1) if fence_match else cleaned
    if not candidate.lstrip().startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validated_summary_timecodes(value: Any, transcript: str) -> list[str]:
    candidates = value if isinstance(value, list) else [value]
    available_seconds = sorted({
        int(match.group("minutes")) * 60 + int(match.group("seconds"))
        for match in TIMESTAMP_PATTERN.finditer(transcript or "")
    })
    if not available_seconds:
        return [
            _format_mmss(int(match.group(1)) * 60 + int(match.group(2)))
            for candidate in candidates
            if (match := re.search(r"(\d{1,3}):([0-5]\d)", str(candidate or "")))
        ]

    validated: list[str] = []
    for candidate in candidates:
        match = re.search(r"(\d{1,3}):([0-5]\d)", str(candidate or ""))
        if not match:
            continue
        requested = int(match.group(1)) * 60 + int(match.group(2))
        nearest = min(available_seconds, key=lambda seconds: abs(seconds - requested))
        if abs(nearest - requested) > 90:
            continue
        normalized = _format_mmss(nearest)
        if normalized not in validated:
            validated.append(normalized)
    return validated


def _validated_summary_refs(value: Any, prefix: str, allowed: set[str]) -> list[str]:
    refs = _summary_reference_ids(value, prefix, default="")
    if not refs:
        return []
    return [ref for ref in refs.split("、") if ref in allowed]


def _infer_meeting_date(
    meeting_title: Optional[str],
    audio_path: Optional[Path] = None,
    fallback: Optional[date] = None,
) -> date:
    """Infer the actual meeting date from the title or retained source filename."""
    candidates = [meeting_title or ""]
    if audio_path is not None:
        candidates.extend([audio_path.stem, audio_path.name])

    patterns = (
        re.compile(r"(?<!\d)(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})(?:日)?(?!\d)"),
        re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"),
    )
    for candidate in candidates:
        for pattern in patterns:
            for match in pattern.finditer(candidate):
                try:
                    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    continue
    return fallback or datetime.now().date()


def _transcript_turns(transcript: str) -> list[tuple[int, str, str]]:
    turns: list[tuple[int, str, str]] = []
    pattern = re.compile(
        r"^\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]\s*"
        r"(?:\*{1,2})?\[(?P<speaker>[^\]]+)\](?:\*{1,2})?\s*[：:]\s*(?P<text>.*)$",
        flags=re.MULTILINE,
    )
    for match in pattern.finditer(transcript or ""):
        seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        turns.append((seconds, match.group("speaker").strip(), match.group("text").strip()))
    return turns


def _nearest_transcript_turn(
    transcript: str,
    timecodes: Any,
    max_distance_seconds: int = 20,
) -> Optional[tuple[int, str, str]]:
    validated = _validated_summary_timecodes(timecodes, transcript)
    if not validated:
        return None
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", validated[0])
    turns = _transcript_turns(transcript)
    if not match or not turns:
        return None
    target = int(match.group(1)) * 60 + int(match.group(2))
    nearest = min(turns, key=lambda turn: abs(turn[0] - target))
    return nearest if abs(nearest[0] - target) <= max_distance_seconds else None


def _explicit_first_person_owner(transcript: str, timecodes: Any) -> Optional[str]:
    turn = _nearest_transcript_turn(transcript, timecodes)
    if not turn:
        return None
    _, speaker, spoken_text = turn
    first_person_commitment = re.search(
        r"(?:我們這邊|我這邊|我)"
        r"(?:後續)?(?:會|要|再|來|先|負責|預計|打算|需要)",
        spoken_text,
    )
    if not first_person_commitment:
        return None
    return speaker


_SPOKEN_DUE_PATTERN = re.compile(
    r"(20\d{2}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日號]?"
    r"|(?:下週|下禮拜|下個禮拜)\s*[一二三四五六日天]"
    r"|(?:今天|今日|明天|月底|下週|下禮拜|下個禮拜))"
)
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _spoken_due_from_transcript(
    transcript: str,
    timecodes: Any,
    preferred_source: str = "",
) -> str:
    turn = _nearest_transcript_turn(transcript, timecodes)
    if not turn:
        return ""
    spoken_text = re.sub(r"\s+", "", turn[2])
    preferred = re.sub(r"\s+", "", preferred_source or "").strip()
    if preferred and preferred != "未提及" and preferred in spoken_text:
        return preferred
    matches = list(_SPOKEN_DUE_PATTERN.finditer(turn[2]))
    return re.sub(r"\s+", "", matches[-1].group(1)) if matches else ""


def _resolve_spoken_due(source_text: str, meeting_date: Optional[date]) -> str:
    source = re.sub(r"\s+", "", source_text or "").strip()
    if not source or not meeting_date:
        return source

    if source in {"今天", "今日"}:
        return f"{meeting_date:%Y/%m/%d}（原文：{source}）"
    if source == "明天":
        resolved = meeting_date + timedelta(days=1)
        return f"{resolved:%Y/%m/%d}（原文：{source}）"

    weekday_match = re.fullmatch(r"(?:下週|下禮拜|下個禮拜)([一二三四五六日天])", source)
    if weekday_match:
        start_of_next_week = meeting_date + timedelta(days=7 - meeting_date.weekday())
        resolved = start_of_next_week + timedelta(days=_WEEKDAY_INDEX[weekday_match.group(1)])
        return f"{resolved:%Y/%m/%d}（原文：{source}）"

    full_date_match = re.fullmatch(
        r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?",
        source,
    )
    if full_date_match:
        try:
            resolved = date(*(int(value) for value in full_date_match.groups()))
            return f"{resolved:%Y/%m/%d}"
        except ValueError:
            return source

    month_day_match = re.fullmatch(r"(\d{1,2})月(\d{1,2})[日號]?", source)
    if month_day_match:
        try:
            resolved = date(meeting_date.year, int(month_day_match.group(1)), int(month_day_match.group(2)))
            return f"{resolved:%Y/%m/%d}"
        except ValueError:
            return source
    return source


def _normalize_decision_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "pending").strip().lower()
    if status not in {"confirmed", "pending"}:
        status = "pending"
    decision_text = " ".join(
        str(item.get(key) or "") for key in ("decision", "basis", "reason")
    )
    tentative_markers = (
        "暫定", "預計", "待確認", "尚未決定", "尚待", "需再", "後續再",
        "考慮", "可能", "視情況", "視需求", "再決定", "再討論", "評估中",
    )
    if any(marker in decision_text for marker in tentative_markers):
        return "pending"
    return status


def _normalize_summary_payload(
    payload: dict[str, Any],
    transcript: str,
    meeting_date: Optional[date] = None,
) -> dict[str, Any]:
    """Repair identifiers and evidence references locally without another model call."""
    discussions: list[Any] = []
    for index, raw in enumerate(_coerce_summary_items(payload.get("discussion_summary")), start=1):
        item = dict(raw) if isinstance(raw, dict) else {"summary": raw}
        item["id"] = f"D{index}"
        timecodes = item.get("evidence_timecodes") or item.get("timecodes") or item.get("source_timecodes")
        item["evidence_timecodes"] = _validated_summary_timecodes(timecodes, transcript)
        discussions.append(item)
    discussion_ids = {item["id"] for item in discussions}

    decisions: list[Any] = []
    for index, raw in enumerate(_coerce_summary_items(payload.get("final_decisions")), start=1):
        item = dict(raw) if isinstance(raw, dict) else {"decision": raw}
        item["id"] = f"R{index}"
        refs = item.get("related_discussions") or item.get("discussion_ids") or item.get("related_discussion")
        item["related_discussions"] = _validated_summary_refs(refs, "D", discussion_ids)
        timecodes = item.get("evidence_timecodes") or item.get("timecodes") or item.get("source_timecodes")
        item["evidence_timecodes"] = _validated_summary_timecodes(timecodes, transcript)
        item["status"] = _normalize_decision_status(item)
        decisions.append(item)
    decision_ids = {item["id"] for item in decisions}

    actions: list[Any] = []
    for index, raw in enumerate(_coerce_summary_items(payload.get("action_items")), start=1):
        item = dict(raw) if isinstance(raw, dict) else {"task": raw}
        item["id"] = f"A{index}"
        discussion_refs = item.get("related_discussions") or item.get("discussion_ids") or item.get("related_discussion")
        decision_refs = item.get("related_decisions") or item.get("decision_ids") or item.get("related_decision")
        item["related_discussions"] = _validated_summary_refs(discussion_refs, "D", discussion_ids)
        item["related_decisions"] = _validated_summary_refs(decision_refs, "R", decision_ids)
        source_timecodes = item.get("source_timecodes") or item.get("timecodes")
        item["source_timecodes"] = _validated_summary_timecodes(source_timecodes, transcript)
        priority = str(item.get("priority") or "中").strip()
        item["priority"] = priority if priority in {"高", "中", "低"} else "中"
        explicit_owner = _explicit_first_person_owner(transcript, item["source_timecodes"])
        if explicit_owner:
            item["owner"] = explicit_owner
        model_due_source = str(item.get("due_source") or "").strip()
        spoken_due = _spoken_due_from_transcript(
            transcript,
            item["source_timecodes"],
            model_due_source,
        )
        due_source = spoken_due or model_due_source
        if due_source:
            item["due_source"] = due_source
            item["due"] = _resolve_spoken_due(due_source, meeting_date)
        actions.append(item)

    return {
        "discussion_summary": discussions,
        "final_decisions": decisions,
        "action_items": actions,
    }


def _summary_json_to_markdown(payload: dict[str, Any]) -> str:
    discussion_items = _coerce_summary_items(payload.get("discussion_summary"))
    decision_items = _coerce_summary_items(payload.get("final_decisions"))
    action_items = _coerce_summary_items(payload.get("action_items"))

    lines: list[str] = ["## 一、討論摘要 (Discussion Summary)", ""]
    if discussion_items:
        for index, item in enumerate(discussion_items, start=1):
            discussion_id = _summary_item_id(item, "D", index)
            if isinstance(item, dict):
                topic = _plain_markdown_cell(item.get("topic") or item.get("title"), "")
                heading = topic or f"討論項目 {index}"
                summary = _plain_markdown_cell(item.get("summary") or item.get("content") or item, "")
                context = _plain_markdown_cell(item.get("context") or item.get("background"), "")
                key_points = _plain_markdown_cell(item.get("key_points") or item.get("points"), "")
                impact = _plain_markdown_cell(item.get("impact") or item.get("risk") or item.get("risks"), "")
                open_questions = _plain_markdown_cell(item.get("open_questions") or item.get("pending_questions"), "")
                evidence = _plain_markdown_cell(
                    item.get("evidence_timecodes") or item.get("timecodes") or item.get("source_timecodes"),
                    "",
                )
                lines.append(f"### {discussion_id}. {heading}")
                lines.append(f"- 摘要：{summary}")
                if context:
                    lines.append(f"- 背景：{context}")
                if key_points:
                    lines.append(f"- 重點：{key_points}")
                if impact:
                    lines.append(f"- 影響/風險：{impact}")
                if open_questions:
                    lines.append(f"- 待釐清：{open_questions}")
                if evidence:
                    lines.append(f"- 佐證時間：{evidence}")
                lines.append("")
            else:
                lines.append(f"### {discussion_id}. 討論項目 {index}")
                lines.append(f"- 摘要：{_plain_markdown_cell(item)}")
                lines.append("")
    else:
        lines.append("- 未提及")

    lines.extend(["", "---", "", "## 二、最終決議 (Final Decisions)", ""])
    lines.extend([
        "| # | 關聯討論 | 決議 | 依據 | 狀態 |",
        "|---|---------|------|------|------|",
    ])
    if decision_items:
        for index, item in enumerate(decision_items, start=1):
            decision_id = _summary_item_id(item, "R", index)
            if isinstance(item, dict):
                related_discussions = _summary_reference_ids(
                    item.get("related_discussions") or item.get("discussion_ids") or item.get("related_discussion"),
                    "D",
                )
                decision = _plain_markdown_cell(item.get("decision") or item.get("content") or item, "")
                basis = _plain_markdown_cell(item.get("basis") or item.get("reason"))
                evidence = _plain_markdown_cell(
                    item.get("evidence_timecodes") or item.get("timecodes") or item.get("source_timecodes"),
                    "",
                )
                if evidence:
                    basis = f"{basis}<br>佐證：{evidence}"
                status = _plain_markdown_cell(item.get("status"), "pending")
            else:
                related_discussions = "未提及"
                decision = _plain_markdown_cell(item)
                basis = "未提及"
                status = "pending"
            lines.append(f"| {decision_id} | {related_discussions} | {decision} | {basis} | {status} |")
    else:
        lines.append("| R1 | 未提及 | 未提及 | 未提及 | pending |")

    lines.extend([
        "",
        "---",
        "",
        "## 三、待辦事項 (Action Items)",
        "",
        "| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |",
        "|---|---------|---------|---------|--------|------|--------|",
    ])
    if action_items:
        for index, item in enumerate(action_items, start=1):
            action_id = _summary_item_id(item, "A", index)
            if isinstance(item, dict):
                related_discussions = _summary_reference_ids(
                    item.get("related_discussions") or item.get("discussion_ids") or item.get("related_discussion"),
                    "D",
                )
                related_decisions = _summary_reference_ids(
                    item.get("related_decisions") or item.get("decision_ids") or item.get("related_decision"),
                    "R",
                )
                task = _plain_markdown_cell(item.get("task") or item.get("description") or item.get("content"))
                evidence = _plain_markdown_cell(item.get("source_timecodes") or item.get("timecodes"), "")
                if evidence:
                    task = f"{task}<br>佐證：{evidence}"
                owner = _plain_markdown_cell(item.get("owner") or item.get("assignee"))
                due = _plain_markdown_cell(item.get("due") or item.get("deadline"))
                priority = _plain_markdown_cell(item.get("priority"), "中")
            else:
                related_discussions = related_decisions = "未提及"
                task = _plain_markdown_cell(item)
                owner = due = "未提及"
                priority = "中"
            lines.append(
                f"| {action_id} | {related_discussions} | {related_decisions} | {task} | {owner} | {due} | {priority} |"
            )
    else:
        lines.append("| A1 | 未提及 | 未提及 | 未提及 | 未提及 | 未提及 | 中 |")

    return _normalize_domain_terms("\n".join(lines).strip())


def _summary_response_to_markdown(
    text: str,
    full_transcript: str = "",
    meeting_date: Optional[date] = None,
) -> str:
    payload = _extract_json_object(text)
    if payload:
        return _summary_json_to_markdown(
            _normalize_summary_payload(payload, full_transcript, meeting_date)
        )
    cleaned = _normalize_domain_terms(clean_hallucinated_loops(text or ""))
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    return cleaned.strip()


def _build_summary_prompt(full_transcript: str, meeting_date: Optional[date] = None) -> str:
    actual_meeting_date = meeting_date or datetime.now().date()
    weekday_names = "一二三四五六日"
    meeting_date_text = actual_meeting_date.strftime("%Y/%m/%d")
    meeting_weekday = weekday_names[actual_meeting_date.weekday()]
    prompt = f"""
# 角色設定
你是一位擁有 15 年經驗的國際企業專業高階秘書（Executive Secretary），
精通醫療器材研發會議記錄、法規文件追蹤、商業寫作與多語言溝通。

實際會議日期：{meeting_date_text}（星期{meeting_weekday}）

以下是一份完整的會議逐字稿（已分段），請根據逐字稿生成摘要、決議與待辦事項：

{full_transcript}

---

# 判讀規則
{DOMAIN_TERMINOLOGY_POLICY}

{MEDICAL_DEVICE_RND_ANALYSIS_POLICY}

---

# 輸出格式
請使用 **繁體中文**，**嚴格按照以下三個區塊**輸出，不要新增其他區塊，不要省略任何一個區塊：

## 📋 一、討論摘要 (Discussion Summary)
請依專案或議題分組，整理各方提出的關鍵意見、時程、卡點、風險與下一步，幫助讀者快速掌握會議脈絡。
若會議中有多個討論項目，請分成 D1、D2、D3...，每個討論項目只描述一個主要議題。

---

## ✅ 二、最終決議 (Final Decisions)
請清楚寫下經過討論後已確認的共識或結論；不要把追蹤目標、背景說明或教學內容列為決議。
如果某個議題沒有結論，也應明確註記「尚未決定」或「需延至下次討論」。
每一項決議請標為 R1、R2、R3...，並標明關聯的 D 編號。

---

## 📌 三、待辦事項 (Action Items)
請以表格呈現所有被提及的任務、負責人與期限：

| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|---------|---------|--------|------|--------|
| A1 | D1 | R1 | [可驗收的任務內容] | [姓名/部門] | [日期或「未定」] | 高/中/低 |

若負責人只能從逐字稿辨識為匿名發言者，請保留「發言者 A/B/C」標籤，不要自行推測姓名。
若任務過大，請拆成可驗收的文件、測試、追蹤或會議安排項目。
若逐字稿只提到月/日，年份以實際會議日期為準。
若逐字稿使用「下週二」等相對期限，due_source 必須保留逐字稿原句，不要自行換算日期；系統會在本機換算。

> ⚠️ 重要：輸出完三個區塊後立即停止，不要輸出逐字稿，也不要附加任何秘書備註或後記。
""".strip()
    return prompt + """

---

# Structured output contract
Return JSON only. Do not wrap it in Markdown fences.
Schema:
{
  "discussion_summary": [
    {
      "id": "D1",
      "topic": "主題",
      "context": "討論背景或問題來源",
      "summary": "用會議中的事實整理，不要新增逐字稿沒有的內容",
      "key_points": ["關鍵意見或資訊"],
      "impact": "影響、風險或對後續工作的意義；沒有就寫未提及",
      "open_questions": ["尚未釐清事項；沒有就寫未提及"],
      "evidence_timecodes": ["00:00"]
    }
  ],
  "final_decisions": [
    {
      "id": "R1",
      "related_discussions": ["D1"],
      "decision": "已確認的決議；若只是討論中請寫成待確認",
      "basis": "逐字稿依據",
      "status": "confirmed|pending",
      "evidence_timecodes": ["00:00"]
    }
  ],
  "action_items": [
    {
      "id": "A1",
      "related_discussions": ["D1"],
      "related_decisions": ["R1"],
      "task": "待辦事項",
      "owner": "負責人或未提及",
      "due": "期限或未提及",
      "due_source": "逐字稿中的期限原句；沒有就寫未提及",
      "priority": "高|中|低",
      "source_timecodes": ["00:00"]
    }
  ]
}
Rules:
- Use Traditional Chinese.
- Keep Qisda as 佳世達.
- Before writing JSON, silently build a fact ledger from timestamped utterances, then cluster facts by project and deliverable.
- One discussion item may contain only one independently actionable topic. Split different projects, deliverables, tests, document packages, or decisions into separate D items even when the same speaker discusses them continuously.
- Do not use a combined title such as "A 與 B" when A and B have separate progress, risks, decisions, or owners.
- Every final decision must reference related_discussions when traceable.
- Every final decision must include evidence_timecodes. Use confirmed only for explicit agreement, approval, selection, or a completed fact accepted by the meeting. Words such as 暫定、預計、考慮、待確認、後續再調整 must be pending.
- Every action item must reference related_discussions and related_decisions when traceable.
- Every action item must include source_timecodes and due_source. due_source must copy the spoken date phrase exactly.
- A person or department being asked, consulted, notified, or followed up with is not automatically the owner. For example, "我會問品保" means the current speaker owns the follow-up, not 品保.
- If owner, due date, or decision is not explicit, write 未提及 or pending instead of guessing.
- If only month/day is spoken, use the actual meeting year above. Preserve relative wording in due_source; do not calculate it yourself.
- Do not use **bold** markers in JSON values.
""".strip()


def _generate_meeting_content_from_transcript(
    client,
    *,
    full_transcript: str,
    job_id: str,
    summary_primary_model: str,
    summary_secondary_model: str,
    summary_verifier_model: Optional[str] = None,
    meeting_date: Optional[date] = None,
    high_quality: bool = False,
) -> tuple[str, str]:
    update_job_status(
        job_id,
        "processing",
        f"🤖 AI 正在生成會議摘要與分析（摘要模型：{summary_primary_model}）...",
    )
    logger.info(
        "[%s] 🤖 以完整逐字稿生成整體摘要（摘要模型：%s；備援：%s）...",
        job_id,
        summary_primary_model,
        summary_secondary_model,
    )

    summary_prompt = _build_summary_prompt(full_transcript, meeting_date)
    response, summary_model_used = _generate_text_with_fallback(
        client,
        primary_model=summary_primary_model,
        fallback_model=summary_secondary_model,
        contents=[summary_prompt],
        config=types.GenerateContentConfig(
            temperature=0.1,
            top_p=0.9,
            max_output_tokens=65536,
        ),
        job_id=job_id,
        stage="會議摘要生成",
    )

    summary_section = _summary_response_to_markdown(
        response.text or "",
        full_transcript,
        meeting_date,
    )
    if high_quality:
        update_job_status(job_id, "processing", "🔎 第二模型正在查核摘要證據與逐字稿完整性...")
        verification_prompt = f"""
# 角色
你是第二階段會議紀錄稽核員。請以完整逐字稿為唯一事實來源，查核第一階段摘要並輸出修正版。

# 完整逐字稿
{full_transcript}

# 第一階段摘要
{summary_section}

# 查核規則
1. 每個重要討論主題都要有獨立 D 編號，不可把不同專案、文件、測試或決策合併。
2. 每個 D、R、A 都必須能由時間戳附近的逐字稿支持；找不到證據就刪除或標為未提及。
3. confirmed 只限明確同意、核准、選定或已完成並被會議接受的事實；暫定、預計、可能、待確認一律 pending。
4. 「我會問品保」的負責人是當前發言者，不是品保。被詢問、通知或協作的對象不得自動列為負責人。
5. 期限原句放在 due_source，不可自行猜測日期。
6. 不可新增逐字稿沒有的姓名、文件、數字、日期、風險、決議或待辦。

Return JSON only, without Markdown fences, using exactly these top-level keys:
{{
  "discussion_summary": [{{"id":"D1","topic":"主題","context":"背景","summary":"摘要","key_points":["重點"],"impact":"影響或未提及","open_questions":["待釐清或未提及"],"evidence_timecodes":["00:00"]}}],
  "final_decisions": [{{"id":"R1","related_discussions":["D1"],"decision":"決議","basis":"逐字稿依據","status":"confirmed|pending","evidence_timecodes":["00:00"]}}],
  "action_items": [{{"id":"A1","related_discussions":["D1"],"related_decisions":["R1"],"task":"可驗收任務","owner":"負責人或未提及","due":"期限或未提及","due_source":"期限原句或未提及","priority":"高|中|低","source_timecodes":["00:00"]}}]
}}
""".strip()
        verification_model_name = (summary_verifier_model or summary_secondary_model).strip()
        verification_response, verification_model = _generate_text_with_fallback(
            client,
            primary_model=verification_model_name,
            fallback_model=summary_model_used,
            contents=[verification_prompt],
            config=types.GenerateContentConfig(
                temperature=0.0,
                top_p=0.8,
                max_output_tokens=65536,
            ),
            job_id=job_id,
            stage="高品質摘要查核",
        )
        if not _extract_json_object(verification_response.text or ""):
            raise RuntimeError("高品質摘要查核未回傳有效 JSON，保留任務以便自動重試。")
        summary_section = _summary_response_to_markdown(
            verification_response.text or "",
            full_transcript,
            meeting_date,
        )
        summary_model_used = f"{summary_model_used}+verified:{verification_model}"

    meeting_content = _replace_transcript_section(summary_section, full_transcript)
    meeting_content = _prepend_transcript_quality_notice(meeting_content, full_transcript)
    return meeting_content, summary_model_used


def _build_quality_report(
    audio_report: dict[str, Any],
    segment_report: list[dict[str, Any]],
    full_transcript: str,
) -> dict[str, Any]:
    segment_report = [
        dict(segment)
        for segment in segment_report or []
        if isinstance(segment, dict)
    ]
    _merge_repeated_turn_review_segments(segment_report, full_transcript)
    warnings = list(audio_report.get("warnings") or [])
    silence_ratio = float(audio_report.get("silence_ratio") or 0)
    if silence_ratio >= 0.8:
        warnings.append("錄音中靜音比例偏高，建議抽查聲音較小的時段。")

    review_segments = _quality_report_review_segments(segment_report)
    segment_warnings = _quality_report_segment_warnings(review_segments)
    quality_penalty_units = len(warnings) + len(review_segments)
    warnings.extend(segment_warnings)

    score = 100
    score -= min(20, quality_penalty_units * 5)
    if silence_ratio >= 0.9:
        score -= 10
    score = max(0, score)
    has_review_signal = bool(warnings or review_segments)
    label = (
        "需人工確認"
        if score < 75
        else "可用，建議抽查"
        if has_review_signal or score < 90
        else "良好"
    )
    speakers = sorted(set(re.findall(r"\*\*\[([^\]]+)\]\*\*", full_transcript or "")))
    return {
        "score": score,
        "label": label,
        "warnings": list(dict.fromkeys(warnings)),
        "audio": audio_report,
        "segments": segment_report,
        "review_segments": review_segments,
        "timestamp_count": _timestamp_count(full_transcript),
        "speaker_labels": speakers,
    }


def process_audio_task(
    job_id: str,
    audio_path: Path,
    output_dir: Path,
    model: str = GEMINI_MODEL,
    meeting_title: Optional[str] = None,
    cleanup_source_audio: bool = False,
    summary_model: Optional[str] = None,
    summary_fallback_model: Optional[str] = None,
    summary_verifier_model: Optional[str] = None,
    recording_profile: Optional[str] = None,
    force_segment_indices: Optional[list[int]] = None,
    summary_source_path: Optional[Path] = None,
    transcript_reuse_source_path: Optional[Path] = None,
    high_quality_summary: bool = False,
) -> Optional[Path]:
    """
    主要背景任務函數：接收音檔路徑，執行完整的 AI 會議記錄生成流程。

    所有音訊都先產生逐字稿，再用摘要模型整理會議記錄；長音訊會先切割成分段後依序轉錄。

    此函數由本機持久化佇列 worker 或相容的背景流程呼叫，
    任何步驟的狀態變更都會即時寫入 SQLite，供 /status/{job_id} 查詢。
    """
    client = None
    segment_paths: list[Path] = []
    temporary_segment_paths: list[Path] = []
    audio_report: dict[str, Any] = {}
    segment_report: list[dict[str, Any]] = []
    forced_segments = {int(value) for value in (force_segment_indices or []) if int(value) >= 0}
    summary_primary_model, summary_secondary_model = _resolve_summary_models(
        transcription_model=model,
        summary_model=summary_model,
        summary_fallback_model=summary_fallback_model,
    )
    summary_verifier_model = (summary_verifier_model or SUMMARY_VERIFIER_MODEL).strip()
    recording_profile = (recording_profile or "legacy_upload").strip()
    summary_model_used = model
    actual_meeting_date = _infer_meeting_date(meeting_title, audio_path)

    try:
        # ------------------------------------------------------------------
        # 步驟 1：初始化 Gemini Client
        # ------------------------------------------------------------------
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("找不到 GEMINI_API_KEY 環境變數")

        _raise_if_cancelled(job_id)
        client = genai.Client(api_key=api_key)
        logger.info(
            "[%s] 🔧 Gemini Client 初始化成功（轉錄模型：%s；摘要模型：%s；備援摘要模型：%s）",
            job_id,
            model,
            summary_primary_model,
            summary_secondary_model,
        )

        # ------------------------------------------------------------------
        # 步驟 2：驗證媒體格式
        # ------------------------------------------------------------------
        suffix = audio_path.suffix.lower()
        if suffix not in SUPPORTED_MEDIA_FORMATS:
            raise ValueError(f"不支援的媒體格式：{suffix}")

        _raise_if_cancelled(job_id)

        # ------------------------------------------------------------------
        # 步驟 3：本機音訊檢測、必要時正規化，再依靜音位置切段
        # ------------------------------------------------------------------
        update_job_status(job_id, "processing", "🎚️ 正在進行免費的本機音訊品質檢查...")
        prepared_audio_path, audio_report = _prepare_audio_for_transcription(
            audio_path,
            ROOT_DIR / "temp",
            job_id,
        )
        if prepared_audio_path != audio_path:
            temporary_segment_paths.append(prepared_audio_path)

        raw_segments = _split_audio_to_segments(prepared_audio_path, segment_minutes=SEGMENT_MINUTES)
        legacy_segment_paths = all(not isinstance(item, AudioSlice) for item in raw_segments)
        audio_slices = _coerce_audio_slices(raw_segments)
        segment_paths = [item.path for item in audio_slices]
        total_segs = len(audio_slices)
        is_segmented = total_segs > 1
        segment_bounds = (
            []
            if legacy_segment_paths
            else [[item.start_seconds, item.end_seconds] for item in audio_slices]
        )
        segment_cache_context = _segment_cache_context(
            audio_path,
            model,
            total_segs,
            SEGMENT_MINUTES,
            segment_bounds=segment_bounds,
        )

        # ------------------------------------------------------------------
        # 步驟 4：逐段轉錄（或整體上傳）
        # ------------------------------------------------------------------
        all_transcripts: list[str] = []
        existing_segment_transcripts: dict[int, str] = {}
        segment_quality_events: list[dict[str, Any]] = []
        if transcript_reuse_source_path is not None:
            try:
                reuse_content = transcript_reuse_source_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"無法讀取原會議逐字稿：{transcript_reuse_source_path}") from exc
            reuse_transcript = _extract_transcript_section_body(reuse_content)
            if not reuse_transcript:
                raise RuntimeError("原會議紀錄缺少完整逐字稿，無法沿用未指定分段。")
            existing_segment_transcripts = _transcript_segments_by_index(reuse_transcript)

        if summary_source_path is not None:
            update_job_status(job_id, "processing", "♻️ 正在沿用既有逐字稿重整摘要...")
            try:
                source_content = summary_source_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"無法讀取原會議紀錄：{summary_source_path}") from exc
            full_transcript = _extract_transcript_section_body(source_content)
            if not full_transcript:
                raise RuntimeError("原會議紀錄缺少完整逐字稿，無法只重整摘要。")
            _raise_if_full_transcript_unsafe(full_transcript, job_id)
            segment_report.extend({
                "index": index,
                "start_seconds": audio_slice.start_seconds,
                "end_seconds": audio_slice.end_seconds,
                "status": "reused",
                "issues": [],
            } for index, audio_slice in enumerate(audio_slices))
            meeting_content, summary_model_used = _generate_meeting_content_from_transcript(
                client=client,
                full_transcript=full_transcript,
                job_id=job_id,
                summary_primary_model=summary_primary_model,
                summary_secondary_model=summary_secondary_model,
                summary_verifier_model=summary_verifier_model,
                meeting_date=actual_meeting_date,
                high_quality=high_quality_summary,
            )

        elif is_segmented:
            for i, audio_slice in enumerate(audio_slices):
                _raise_if_cancelled(job_id)
                seg_path = audio_slice.path
                offset_seconds = audio_slice.start_seconds
                segment_start = _format_mmss(offset_seconds)
                segment_end = _format_mmss(audio_slice.end_seconds)

                transcript = None
                transcript_source = ""
                if i not in forced_segments:
                    transcript = _load_segment_transcript_cache(
                        output_dir=output_dir,
                        job_id=job_id,
                        segment_index=i,
                        context=segment_cache_context or {},
                    )
                    transcript_source = "cache" if transcript is not None else ""
                    if transcript is None:
                        record_transcript = existing_segment_transcripts.get(i)
                        if record_transcript is not None:
                            reuse_issues = _record_segment_reuse_blocking_issues(
                                record_transcript,
                                segment_index=i,
                                total_segments=total_segs,
                                expected_start_seconds=audio_slice.start_seconds,
                                expected_end_seconds=audio_slice.end_seconds,
                            )
                            if reuse_issues:
                                logger.warning(
                                    "[%s] ⚠️ 第 %s 段原會議逐字稿不安全，改為重新轉錄：%s",
                                    job_id,
                                    i + 1,
                                    "；".join(reuse_issues),
                                )
                            else:
                                transcript = record_transcript
                                transcript_source = "record"
                if transcript is not None:
                    source_label = "原會議逐字稿" if transcript_source == "record" else "轉錄快取"
                    logger.info(f"[{job_id}] ♻️  使用第 {i + 1}/{total_segs} 段{source_label}")
                    if transcript_source == "record":
                        _save_segment_transcript_cache(
                            output_dir=output_dir,
                            job_id=job_id,
                            segment_index=i,
                            context=segment_cache_context or {},
                            transcript=transcript,
                        )
                    all_transcripts.append(f"\n\n### 【第 {i + 1} 段｜{segment_start} – {segment_end}】\n\n{transcript}")
                    update_job_status(
                        job_id, "processing",
                        f"♻️ 已沿用第 {i + 1}/{total_segs} 段既有逐字稿",
                        progress_current=i + 1,
                        progress_total=total_segs,
                    )
                    segment_report.append({
                        "index": i,
                        "start_seconds": audio_slice.start_seconds,
                        "end_seconds": audio_slice.end_seconds,
                        "status": "reused",
                        "issues": [],
                    })
                    continue

                update_job_status(
                    job_id, "processing",
                    f"📝 正在轉錄第 {i + 1}/{total_segs} 段音訊...",
                    progress_current=i,
                    progress_total=total_segs,
                )
                logger.info(f"[{job_id}] 🎙 轉錄分段 {i + 1}/{total_segs}：{seg_path.name}")
                speaker_context = _speaker_context_from_transcripts(all_transcripts)
                transcript = _transcribe_segment_with_recovery(
                    client,
                    seg_path,
                    i,
                    total_segs,
                    job_id,
                    model,
                    offset_seconds=offset_seconds,
                    duration_seconds=max(1, audio_slice.end_seconds - audio_slice.start_seconds),
                    is_last_segment=i >= total_segs - 1,
                    speaker_context=speaker_context,
                    temp_segment_paths=temporary_segment_paths,
                    quality_events=segment_quality_events,
                )
                _save_segment_transcript_cache(
                    output_dir=output_dir,
                    job_id=job_id,
                    segment_index=i,
                    context=segment_cache_context or {},
                    transcript=transcript,
                )
                all_transcripts.append(f"\n\n### 【第 {i + 1} 段｜{segment_start} – {segment_end}】\n\n{transcript}")
                segment_issues = _quality_event_issues_for_segment(segment_quality_events, i)
                segment_report.append({
                    "index": i,
                    "start_seconds": audio_slice.start_seconds,
                    "end_seconds": audio_slice.end_seconds,
                    "status": "recovered" if segment_issues else ("rerun" if i in forced_segments else "transcribed"),
                    "issues": segment_issues,
                })
                update_job_status(
                    job_id, "processing",
                    f"✅ 已完成第 {i + 1}/{total_segs} 段音訊轉錄",
                    progress_current=i + 1,
                    progress_total=total_segs,
                )

            _raise_if_cancelled(job_id)
            full_transcript = "\n".join(all_transcripts)
            _raise_if_full_transcript_unsafe(full_transcript, job_id)

            # ------------------------------------------------------------------
            # 步驟 5：用完整逐字稿生成摘要/決議/待辦
            # ------------------------------------------------------------------
            meeting_content, summary_model_used = _generate_meeting_content_from_transcript(
                client=client,
                full_transcript=full_transcript,
                job_id=job_id,
                summary_primary_model=summary_primary_model,
                summary_secondary_model=summary_secondary_model,
                summary_verifier_model=summary_verifier_model,
                meeting_date=actual_meeting_date,
                high_quality=high_quality_summary,
            )

        else:
            # 短音訊：也走雙模型，先產生完整逐字稿，再交給摘要模型整理。
            _raise_if_cancelled(job_id)
            audio_slice = audio_slices[0]
            transcription_path = audio_slice.path
            file_size_mb = transcription_path.stat().st_size / (1024 * 1024)
            logger.info(f"[{job_id}] 🎙 轉錄單段音檔（{file_size_mb:.2f} MB；模型：{model}）...")

            transcript = None
            if 0 not in forced_segments:
                transcript = _load_segment_transcript_cache(
                    output_dir=output_dir,
                    job_id=job_id,
                    segment_index=0,
                    context=segment_cache_context,
                )
                if transcript is None:
                    transcript = existing_segment_transcripts.get(0)
            if transcript is None:
                update_job_status(job_id, "processing", "📝 正在轉錄音訊逐字稿...")
                transcript = _transcribe_segment(client, transcription_path, 0, total_segs, job_id, model)
                _raise_if_segment_transcript_incomplete(
                    transcript=transcript,
                    segment_index=0,
                    total_segments=total_segs,
                    segment_minutes=SEGMENT_MINUTES,
                    expected_start_seconds=audio_slice.start_seconds,
                    expected_end_seconds=audio_slice.end_seconds,
                )
                _save_segment_transcript_cache(
                    output_dir=output_dir,
                    job_id=job_id,
                    segment_index=0,
                    context=segment_cache_context,
                    transcript=transcript,
                )
                update_job_status(job_id, "processing", "✅ 已完成音訊逐字稿轉錄")
                segment_status = "rerun" if 0 in forced_segments else "transcribed"
            else:
                logger.info(f"[{job_id}] ♻️  使用單段轉錄快取")
                update_job_status(job_id, "processing", "♻️ 已載入既有逐字稿轉錄")
                segment_status = "reused"

            segment_report.append({
                "index": 0,
                "start_seconds": audio_slice.start_seconds,
                "end_seconds": audio_slice.end_seconds,
                "status": segment_status,
                "issues": [],
            })

            full_transcript = _format_transcript_segment(
                0,
                total_segs,
                0,
                None if legacy_segment_paths else audio_slice.end_seconds,
                transcript,
            )
            _raise_if_full_transcript_unsafe(full_transcript, job_id)
            meeting_content, summary_model_used = _generate_meeting_content_from_transcript(
                client=client,
                full_transcript=full_transcript,
                job_id=job_id,
                summary_primary_model=summary_primary_model,
                summary_secondary_model=summary_secondary_model,
                summary_verifier_model=summary_verifier_model,
                meeting_date=actual_meeting_date,
                high_quality=high_quality_summary,
            )

        repair_model = summary_model_used
        repair_fallback_model = summary_secondary_model if repair_model != summary_secondary_model else model
        meeting_content = _normalize_domain_terms(_repair_meeting_content_if_needed(
            client=client,
            model=repair_model,
            meeting_content=meeting_content,
            job_id=job_id,
            fallback_model=repair_fallback_model,
        ))
        meeting_content = _finalize_meeting_content(meeting_content, full_transcript, job_id)
        logger.info(f"[{job_id}] ✅ 會議記錄生成成功")

        # ------------------------------------------------------------------
        # 步驟 6：儲存 Markdown 輸出檔案
        # ------------------------------------------------------------------
        _raise_if_cancelled(job_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        title = meeting_title or audio_path.stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        generated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        meeting_date_str = actual_meeting_date.strftime("%Y/%m/%d")
        output_filename = f"meeting_notes_{audio_path.stem}_{timestamp}.md"
        output_path = output_dir / output_filename
        quality_report = _build_quality_report(audio_report, segment_report, full_transcript)
        quality_report["summary_quality_mode"] = "high" if high_quality_summary else "standard"
        try:
            source_audio_size = audio_path.stat().st_size
            source_audio_sha256 = _sha256_file(audio_path)
        except OSError:
            source_audio_size = None
            source_audio_sha256 = None
        quality_report["recording"] = {
            "profile": recording_profile,
            "source_audio_name": audio_path.name,
            "source_audio_size_bytes": source_audio_size,
            "source_audio_sha256": source_audio_sha256,
            "source_audio_suffix": audio_path.suffix.lower(),
        }

        seg_note = f"（分 {total_segs} 段處理）" if is_segmented else ""
        frontmatter = f"""---
title: 會議記錄 - {title}
date: {meeting_date_str}
generated_at: {generated_at}
source_audio: {audio_path.name}
generated_by: AI 語音會議助理 Backend{seg_note}
transcription_model: {model}
summary_model: {summary_model_used}
summary_fallback_model: {summary_secondary_model}
summary_verifier_model: {summary_verifier_model}
recording_profile: {recording_profile}
source_audio_size_bytes: {quality_report['recording']['source_audio_size_bytes']}
source_audio_sha256: {quality_report['recording']['source_audio_sha256'] or 'unavailable'}
summary_quality_mode: {'high' if high_quality_summary else 'standard'}
job_id: {job_id}
quality_score: {quality_report['score']}
quality_label: {quality_report['label']}
---

"""
        full_content = frontmatter + meeting_content
        output_path.write_text(full_content, encoding="utf-8")
        logger.info(f"[{job_id}] 💾 Markdown 已儲存：{output_path}")

        # ------------------------------------------------------------------
        # 步驟 7：寫入 SQLite
        # ------------------------------------------------------------------
        summary_preview = _extract_summary_preview(meeting_content)
        save_meeting(
            title=title,
            date=meeting_date_str,
            source_audio=audio_path.name,
            output_path=str(output_path),
            summary=summary_preview,
            job_id=job_id,
            quality_report=quality_report,
        )

        # ------------------------------------------------------------------
        # 步驟 8：更新任務狀態為完成
        # ------------------------------------------------------------------
        update_job_status(
            job_id,
            status="done",
            message="✅ 會議記錄生成完成！",
            output_path=str(output_path)
        )
        logger.info(f"[{job_id}] 🎉 任務完成")
        return output_path

    except JobCancelled:
        logger.info(f"[{job_id}] 🛑 任務已取消")
        update_job_status(
            job_id,
            status="cancelled",
            message="任務已取消。",
        )
        return None

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[{job_id}] ❌ 任務失敗：{error_msg}")
        update_job_status(
            job_id,
            status="failed",
            message="❌ 處理失敗，請查看錯誤詳情",
            error_detail=error_msg
        )
        return None

    finally:
        # 清理本地分段暫存音檔
        seen_temp_paths: set[Path] = set()
        for seg_path in [*segment_paths, *temporary_segment_paths]:
            if seg_path in seen_temp_paths:
                continue
            seen_temp_paths.add(seg_path)
            if seg_path != audio_path and seg_path.exists():
                try:
                    seg_path.unlink()
                    logger.info(f"[{job_id}] 🗑️  已清除分段暫存：{seg_path.name}")
                except Exception:
                    pass

        # 視呼叫端需求清理本地原始媒體檔；後端預設保留。
        try:
            if cleanup_source_audio and audio_path.exists():
                audio_path.unlink()
                logger.info(f"[{job_id}] 🗑️  已清除本地原始媒體檔：{audio_path.name}")
        except Exception as e:
            logger.warning(f"[{job_id}] ⚠️  本地媒體檔清理失敗：{e}")
