"""
=============================================================================
backend/tasks.py — 背景任務處理器
=============================================================================
封裝 meeting_assistant.py 的核心 AI 邏輯，
讓 FastAPI 能夠以「背景任務（Background Task）」的方式非同步執行，
確保上傳音檔後 API 能立即回應，不讓使用者等待。
=============================================================================
"""

import os
import sys
import uuid
import re
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

# 將專案根目錄加入 sys.path，才能 import meeting_assistant
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from google import genai
from google.genai import types
from dotenv import load_dotenv

from backend.database import (
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
# 增加處理的等待時間上限 (10分鐘)
MAX_UPLOAD_WAIT_SECONDS = 600
POLLING_INTERVAL        = 3
SEGMENT_MINUTES         = 10
TIMESTAMP_PATTERN       = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]")
SEGMENT_CACHE_VERSION   = 2
SEGMENT_CACHE_DIRNAME   = "segment_cache"
SEGMENT_COMPLETENESS_GRACE_SECONDS = 120
SEGMENT_INCOMPLETE_MARKERS = (
    "系統提示：此處音檔包含無意義雜訊",
    "已自動過濾後續重複內容",
)


class JobCancelled(RuntimeError):
    """Raised when a persisted job receives a cancellation request."""


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
    return text


def _transcript_quality_notice(transcript: str) -> str:
    if not transcript:
        return ""
    quality_markers = ("系統提示：", "已自動過濾", "雜訊", "音訊不清晰")
    if any(marker in transcript for marker in quality_markers):
        return (
            "逐字稿品質註記：音訊中有片段被標示為雜訊、聽不清或已自動過濾；"
            "該時間點附近內容可能缺漏，重要結論請回查原始音檔。"
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


def _speaker_context_from_transcripts(transcripts: list[str], max_lines: int = 8) -> str:
    """Build compact prior-speaker context for the next segmented STT call."""
    if not transcripts:
        return ""

    text = "\n".join(transcripts)
    labels = sorted(set(re.findall(r"\*\*\[([^\]]+)\]\*\*", text)))
    turns = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and re.search(r"\*\*\[[^\]]+\]\*\*", line)
    ][-max_lines:]
    if not labels and not turns:
        return ""

    parts = ["Existing speaker labels from earlier segments:"]
    if labels:
        parts.append(", ".join(labels[:12]))
    if turns:
        parts.append("Recent speaker turns:")
        parts.extend(f"- {line[:180]}" for line in turns)
    return "\n".join(parts)


def _segment_transcript_quality_issues(
    transcript: str,
    segment_index: int,
    total_segments: int,
    segment_minutes: int = SEGMENT_MINUTES,
) -> list[str]:
    """Return quality issues that make a segment unsafe to reuse or summarize."""
    issues: list[str] = []
    if not transcript or not transcript.strip():
        return ["轉錄內容為空"]

    is_last_segment = segment_index >= total_segments - 1
    has_incomplete_marker = any(marker in transcript for marker in SEGMENT_INCOMPLETE_MARKERS)
    if has_incomplete_marker and not is_last_segment:
        issues.append("非最後分段含自動過濾/截斷提示")

    if is_last_segment:
        return issues

    timestamps = [
        int(match.group("minutes")) * 60 + int(match.group("seconds"))
        for match in TIMESTAMP_PATTERN.finditer(transcript)
    ]
    if not timestamps:
        issues.append("非最後分段缺少時間戳")
        return issues

    expected_end = (segment_index + 1) * segment_minutes * 60
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
) -> None:
    issues = _segment_transcript_quality_issues(
        transcript=transcript,
        segment_index=segment_index,
        total_segments=total_segments,
        segment_minutes=segment_minutes,
    )
    if issues:
        raise RuntimeError(
            f"第 {segment_index + 1}/{total_segments} 段轉錄不完整："
            + "；".join(issues)
        )


def _safe_segment_cache_name(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", job_id) or "unknown-job"


def _segment_cache_dir(output_dir: Path, job_id: str) -> Path:
    return Path(output_dir) / SEGMENT_CACHE_DIRNAME / _safe_segment_cache_name(job_id)


def _segment_cache_file(output_dir: Path, job_id: str, segment_index: int) -> Path:
    return _segment_cache_dir(output_dir, job_id) / f"segment_{segment_index + 1:03d}.json"


def _segment_cache_context(
    audio_path: Path,
    model: str,
    total_segments: int,
    segment_minutes: int,
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
        "model": model,
        "total_segments": total_segments,
        "segment_minutes": segment_minutes,
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
    cache_file = _segment_cache_file(output_dir, job_id, segment_index)
    if not cache_file.is_file():
        return None

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[%s] ⚠️  分段快取讀取失敗，將重新轉錄：%s", job_id, exc)
        return None

    if not isinstance(payload, dict) or not _segment_cache_matches(payload, context, segment_index):
        logger.info("[%s] ♻️  分段 %s 快取與目前音檔/模型不符，略過", job_id, segment_index + 1)
        return None

    transcript = payload.get("transcript")
    if not isinstance(transcript, str):
        return None
    issues = _segment_transcript_quality_issues(
        transcript=transcript,
        segment_index=segment_index,
        total_segments=int(context.get("total_segments") or 1),
        segment_minutes=int(context.get("segment_minutes") or SEGMENT_MINUTES),
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
        return None
    return transcript


def _save_segment_transcript_cache(
    output_dir: Path,
    job_id: str,
    segment_index: int,
    context: dict[str, Any],
    transcript: str,
) -> Path:
    cache_file = _segment_cache_file(output_dir, job_id, segment_index)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **context,
        "segment_index": segment_index,
        "transcript": transcript,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    temp_file = cache_file.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(cache_file)
    return cache_file


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


def _split_audio_to_segments(audio_path: Path, segment_minutes: int = 10) -> list[Path]:
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
            return [audio_path]  # 夠短，不需切割

        segments = []
        base = audio_path.parent / f"_seg_{audio_path.stem}"
        base.parent.mkdir(parents=True, exist_ok=True)

        for i, start in enumerate(range(0, duration_ms, segment_ms)):
            chunk = audio[start:start + segment_ms]
            seg_path = audio_path.parent / f"_seg_{audio_path.stem}_{i:03d}.mp3"
            chunk.export(str(seg_path), format="mp3", parameters=["-q:a", "3"])
            segments.append(seg_path)

        logger.info(f"🔪 音訊已切割為 {len(segments)} 段（每段 {segment_minutes} 分鐘）")
        return segments
    except ImportError:
        logger.warning("⚠️  pydub 未安裝，無法切割音訊，將以整體方式送出")
        return [audio_path]
    except Exception as e:
        logger.warning(f"⚠️  音訊切割失敗（{e}），改以整體方式送出")
        return [audio_path]


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
            raise RuntimeError(f"分段 {seg_index + 1} 音檔處理逾時")
        time.sleep(POLLING_INTERVAL)
        elapsed += POLLING_INTERVAL
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state.name == "FAILED":
        raise RuntimeError(f"分段 {seg_index + 1} 音檔處理失敗")

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
            "If the same voice continues, reuse the same label. If uncertain, use an unknown-speaker label.\n\n"
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


def _summary_response_to_markdown(text: str) -> str:
    payload = _extract_json_object(text)
    if payload:
        return _summary_json_to_markdown(payload)
    cleaned = _normalize_domain_terms(clean_hallucinated_loops(text or ""))
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    return cleaned.strip()


def _build_summary_prompt(full_transcript: str) -> str:
    prompt = f"""
# 角色設定
你是一位擁有 15 年經驗的國際企業專業高階秘書（Executive Secretary），
精通醫療器材研發會議記錄、法規文件追蹤、商業寫作與多語言溝通。

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
      "status": "confirmed|pending"
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
      "priority": "高|中|低",
      "source_timecodes": ["00:00"]
    }
  ]
}
Rules:
- Use Traditional Chinese.
- Keep Qisda as 佳世達.
- If there are multiple discussion topics, split them into D1, D2, D3... instead of merging unrelated topics.
- Every final decision must reference related_discussions when traceable.
- Every action item must reference related_discussions and related_decisions when traceable.
- If owner, due date, or decision is not explicit, write 未提及 or pending instead of guessing.
- Do not use **bold** markers in JSON values.
""".strip()


def _generate_meeting_content_from_transcript(
    client,
    *,
    full_transcript: str,
    job_id: str,
    summary_primary_model: str,
    summary_secondary_model: str,
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

    summary_prompt = _build_summary_prompt(full_transcript)
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

    summary_section = _summary_response_to_markdown(response.text or "")
    meeting_content = (
        summary_section
        + "\n\n---\n\n## 📝 四、完整逐字稿 (Verbatim Transcript)\n"
        + full_transcript
    )
    meeting_content = _prepend_transcript_quality_notice(meeting_content, full_transcript)
    return meeting_content, summary_model_used


def process_audio_task(
    job_id: str,
    audio_path: Path,
    output_dir: Path,
    model: str = GEMINI_MODEL,
    meeting_title: Optional[str] = None,
    cleanup_source_audio: bool = False,
    summary_model: Optional[str] = None,
    summary_fallback_model: Optional[str] = None,
) -> Optional[Path]:
    """
    主要背景任務函數：接收音檔路徑，執行完整的 AI 會議記錄生成流程。

    所有音訊都先產生逐字稿，再用摘要模型整理會議記錄；長音訊會先切割成分段後依序轉錄。

    此函數由本機持久化佇列 worker 或相容的背景流程呼叫，
    任何步驟的狀態變更都會即時寫入 SQLite，供 /status/{job_id} 查詢。
    """
    client = None
    segment_paths: list[Path] = []
    summary_primary_model, summary_secondary_model = _resolve_summary_models(
        transcription_model=model,
        summary_model=summary_model,
        summary_fallback_model=summary_fallback_model,
    )
    summary_model_used = model

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
        # 步驟 3：切割音訊（若音訊過長）
        # ------------------------------------------------------------------
        update_job_status(job_id, "processing", "✂️  正在分析音訊長度...")
        segment_paths = _split_audio_to_segments(audio_path, segment_minutes=SEGMENT_MINUTES)
        total_segs = len(segment_paths)
        is_segmented = total_segs > 1
        segment_cache_context = _segment_cache_context(audio_path, model, total_segs, SEGMENT_MINUTES)

        # ------------------------------------------------------------------
        # 步驟 4：逐段轉錄（或整體上傳）
        # ------------------------------------------------------------------
        all_transcripts: list[str] = []

        if is_segmented:
            for i, seg_path in enumerate(segment_paths):
                _raise_if_cancelled(job_id)
                offset_seconds = i * SEGMENT_MINUTES * 60
                segment_start = _format_mmss(offset_seconds)
                segment_end = _format_mmss((i + 1) * SEGMENT_MINUTES * 60)

                transcript = _load_segment_transcript_cache(
                    output_dir=output_dir,
                    job_id=job_id,
                    segment_index=i,
                    context=segment_cache_context or {},
                )
                if transcript is not None:
                    logger.info(f"[{job_id}] ♻️  使用第 {i + 1}/{total_segs} 段轉錄快取")
                    all_transcripts.append(f"\n\n### 【第 {i + 1} 段｜{segment_start} – {segment_end}】\n\n{transcript}")
                    update_job_status(
                        job_id, "processing",
                        f"♻️ 已載入第 {i + 1}/{total_segs} 段既有轉錄",
                        progress_current=i + 1,
                        progress_total=total_segs,
                    )
                    continue

                update_job_status(
                    job_id, "processing",
                    f"📝 正在轉錄第 {i + 1}/{total_segs} 段音訊...",
                    progress_current=i,
                    progress_total=total_segs,
                )
                logger.info(f"[{job_id}] 🎙 轉錄分段 {i + 1}/{total_segs}：{seg_path.name}")
                speaker_context = _speaker_context_from_transcripts(all_transcripts)
                transcript = _transcribe_segment(
                    client,
                    seg_path,
                    i,
                    total_segs,
                    job_id,
                    model,
                    speaker_context=speaker_context,
                )
                transcript = _offset_transcript_timestamps(transcript, offset_seconds)
                _raise_if_segment_transcript_incomplete(
                    transcript=transcript,
                    segment_index=i,
                    total_segments=total_segs,
                    segment_minutes=SEGMENT_MINUTES,
                )
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
                    f"✅ 已完成第 {i + 1}/{total_segs} 段音訊轉錄",
                    progress_current=i + 1,
                    progress_total=total_segs,
                )

            _raise_if_cancelled(job_id)
            full_transcript = "\n".join(all_transcripts)

            # ------------------------------------------------------------------
            # 步驟 5：用完整逐字稿生成摘要/決議/待辦
            # ------------------------------------------------------------------
            meeting_content, summary_model_used = _generate_meeting_content_from_transcript(
                client=client,
                full_transcript=full_transcript,
                job_id=job_id,
                summary_primary_model=summary_primary_model,
                summary_secondary_model=summary_secondary_model,
            )

        else:
            # 短音訊：也走雙模型，先產生完整逐字稿，再交給摘要模型整理。
            _raise_if_cancelled(job_id)
            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            logger.info(f"[{job_id}] 🎙 轉錄單段音檔（{file_size_mb:.2f} MB；模型：{model}）...")

            transcript = _load_segment_transcript_cache(
                output_dir=output_dir,
                job_id=job_id,
                segment_index=0,
                context=segment_cache_context,
            )
            if transcript is None:
                update_job_status(job_id, "processing", "📝 正在轉錄音訊逐字稿...")
                transcript = _transcribe_segment(client, audio_path, 0, total_segs, job_id, model)
                _raise_if_segment_transcript_incomplete(
                    transcript=transcript,
                    segment_index=0,
                    total_segments=total_segs,
                    segment_minutes=SEGMENT_MINUTES,
                )
                _save_segment_transcript_cache(
                    output_dir=output_dir,
                    job_id=job_id,
                    segment_index=0,
                    context=segment_cache_context,
                    transcript=transcript,
                )
                update_job_status(job_id, "processing", "✅ 已完成音訊逐字稿轉錄")
            else:
                logger.info(f"[{job_id}] ♻️  使用單段轉錄快取")
                update_job_status(job_id, "processing", "♻️ 已載入既有逐字稿轉錄")

            full_transcript = _format_transcript_segment(
                0,
                total_segs,
                0,
                None,
                transcript,
            )
            meeting_content, summary_model_used = _generate_meeting_content_from_transcript(
                client=client,
                full_transcript=full_transcript,
                job_id=job_id,
                summary_primary_model=summary_primary_model,
                summary_secondary_model=summary_secondary_model,
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
        meeting_content = _prepend_transcript_quality_notice(meeting_content, meeting_content)
        logger.info(f"[{job_id}] ✅ 會議記錄生成成功")

        # ------------------------------------------------------------------
        # 步驟 6：儲存 Markdown 輸出檔案
        # ------------------------------------------------------------------
        _raise_if_cancelled(job_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        title = meeting_title or audio_path.stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        date_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        output_filename = f"meeting_notes_{audio_path.stem}_{timestamp}.md"
        output_path = output_dir / output_filename

        seg_note = f"（分 {total_segs} 段處理）" if is_segmented else ""
        frontmatter = f"""---
title: 會議記錄 - {title}
date: {date_str}
source_audio: {audio_path.name}
generated_by: AI 語音會議助理 Backend{seg_note}
transcription_model: {model}
summary_model: {summary_model_used}
summary_fallback_model: {summary_secondary_model}
job_id: {job_id}
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
            date=datetime.now().strftime("%Y/%m/%d"),
            source_audio=audio_path.name,
            output_path=str(output_path),
            summary=summary_preview
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
        for seg_path in segment_paths:
            if seg_path != audio_path and seg_path.exists():
                try:
                    seg_path.unlink()
                    logger.info(f"[{job_id}] 🗑️  已清除分段暫存：{seg_path.name}")
                except Exception:
                    pass

        # 視呼叫端需求清理本地原始音檔；後端預設保留。
        try:
            if cleanup_source_audio and audio_path.exists():
                audio_path.unlink()
                logger.info(f"[{job_id}] 🗑️  已清除本地原始音檔：{audio_path.name}")
        except Exception as e:
            logger.warning(f"[{job_id}] ⚠️  本地音檔清理失敗：{e}")
