"""
=============================================================================
backend/line_handler.py — LINE Bot Webhook 訊息處理邏輯 (Phase 3)
=============================================================================
負責：
  1. 驗證 LINE Webhook 簽章
  2. 解析傳入事件（語音訊息 / 文字訊息）
  3. 下載 LINE 語音音檔（m4a）
  4. 觸發 AI 背景任務
  5. 完成後透過 Push API 主動推送會議記錄給使用者
=============================================================================
"""

import hashlib
import os
import logging
import time
import traceback
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# LINE Bot SDK v3
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    AudioMessageContent,
    TextMessageContent,
)

from backend.tasks import process_audio_task, SUPPORTED_MEDIA_FORMATS
from backend.database import create_job, get_job, list_line_jobs_for_user, update_job_status
from backend.source_audio import finalize_source_audio_upload

logger = logging.getLogger("MeetingAssistant.LINE")

# =============================================================================
# 讀取 LINE 環境變數
# =============================================================================
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CONTENT_READY_TIMEOUT_SECONDS = float(os.getenv("LINE_CONTENT_READY_TIMEOUT_SECONDS", "120"))
LINE_CONTENT_READY_POLL_SECONDS = float(os.getenv("LINE_CONTENT_READY_POLL_SECONDS", "2"))
LINE_TEXT_CHUNK_CODE_UNITS = int(os.getenv("LINE_TEXT_CHUNK_CODE_UNITS", "4900"))
LINE_MAX_MESSAGES_PER_REQUEST = 5
LINE_STATUS_QUERY_TEXTS = {"狀態", "進度", "任務狀態", "查詢狀態", "status", "progress"}

# 輸出目錄（與 FastAPI main.py 一致，可用 MEETING_OUTPUT_DIR 覆寫）
OUTPUT_DIR = Path(os.getenv("MEETING_OUTPUT_DIR") or Path(__file__).parent.parent / "output")
SOURCE_AUDIO_DIR = Path(os.getenv("MEETING_SOURCE_AUDIO_DIR") or OUTPUT_DIR / "source_audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _check_env() -> bool:
    """確認 LINE 環境變數是否已設定"""
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error(
            "❌ 缺少 LINE 環境變數！"
            "請在 .env 中設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN。"
        )
        return False
    return True


def get_webhook_parser() -> Optional[WebhookParser]:
    """建立並回傳 WebhookParser（用於驗證簽章與解析事件）"""
    if not _check_env():
        return None
    return WebhookParser(LINE_CHANNEL_SECRET)


def get_line_api() -> Optional[MessagingApi]:
    """建立並回傳 LINE Messaging API 客戶端"""
    if not _check_env():
        return None
    config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    return MessagingApi(ApiClient(config))


# =============================================================================
# 核心訊息處理函式
# =============================================================================

def reply_text(api: MessagingApi, reply_token: str, text: str):
    """向 LINE 平台發送 Reply 訊息（Reply Token 僅可使用一次，需快速使用）"""
    try:
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
    except Exception as e:
        logger.warning(f"⚠️ Reply 訊息發送失敗：{e}")


def _utf16_code_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _split_text_for_line(text: str, max_code_units: int = LINE_TEXT_CHUNK_CODE_UNITS) -> list[str]:
    """Split text under LINE's text-message limit, counted as UTF-16 code units."""
    if text == "":
        return [""]

    chunks: list[str] = []
    current: list[str] = []
    current_units = 0

    for char in text:
        char_units = _utf16_code_units(char)
        if current and current_units + char_units > max_code_units:
            chunks.append("".join(current))
            current = [char]
            current_units = char_units
        else:
            current.append(char)
            current_units += char_units

    if current:
        chunks.append("".join(current))
    return chunks


def push_text(api: MessagingApi, user_id: str, text: str):
    """
    向指定 user_id 主動推送訊息（Push Message，不受 Reply Token 限制）。
    Push/API 訊息會受 LINE 官方帳號方案額度影響。
    """
    try:
        # LINE 單則文字 5000 UTF-16 code units，上限一次最多 5 個 message objects。
        chunks = _split_text_for_line(text)
        sent_count = 0
        for start in range(0, len(chunks), LINE_MAX_MESSAGES_PER_REQUEST):
            batch = chunks[start:start + LINE_MAX_MESSAGES_PER_REQUEST]
            messages = [TextMessage(text=chunk) for chunk in batch]
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=messages,
                )
            )
            sent_count += len(messages)
        logger.info(f"✅ 成功推送訊息給 {user_id}（{sent_count} 則）")
    except Exception as e:
        logger.error(f"❌ Push 訊息發送失敗（user: {user_id}）：{e}")


def is_line_status_query(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in LINE_STATUS_QUERY_TEXTS


def _line_status_label(status: str) -> str:
    return {
        "pending": "排隊中",
        "processing": "處理中",
        "done": "完成",
        "failed": "失敗",
        "cancelled": "已取消",
    }.get(status, status or "未知")


def build_line_status_reply(user_id: str) -> str:
    jobs = list_line_jobs_for_user(user_id, limit=3)
    if not jobs:
        return "目前找不到您的 LINE 會議處理任務。傳送語音或音訊檔後，可以再傳「狀態」查詢進度。"

    job = jobs[0]
    status = job.get("status") or "unknown"
    lines = [
        f"📌 最近任務 {job.get('job_id', '')[:8]}：{_line_status_label(status)}",
    ]

    current = job.get("progress_current")
    total = job.get("progress_total")
    if current is not None and total:
        lines.append(f"進度：{current}/{total}")

    message = job.get("message")
    if message:
        lines.append(f"目前狀態：{message}")

    if status == "done" and job.get("output_path"):
        lines.append(f"完整檔案：{job['output_path']}")

    if status == "failed":
        detail = job.get("error_detail") or "請稍後重新傳送音訊。"
        lines.append(f"失敗原因：{detail}")

    if len(jobs) > 1:
        lines.append(f"另外還有 {len(jobs) - 1} 筆較早的 LINE 任務。")

    return "\n".join(lines)


def _strip_markdown_frontmatter(markdown: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, count=1, flags=re.DOTALL).strip()


def _summary_sections_for_line(markdown: str) -> str:
    body = _strip_markdown_frontmatter(markdown)
    transcript_match = re.search(r"\n##\s*📝\s*四、", body)
    if transcript_match:
        return body[:transcript_match.start()].strip()
    return body.strip()


def build_line_meeting_delivery_message(markdown: str, output_path: Path) -> str:
    summary_sections = _summary_sections_for_line(markdown)
    if not summary_sections:
        summary_sections = "會議記錄已完成，但摘要內容為空，請直接查看完整 Markdown 檔。"

    return (
        "✅ 會議記錄已生成完畢！\n\n"
        f"{summary_sections}\n\n"
        "---\n"
        f"完整逐字稿已保存：{output_path}\n"
        "請到網頁歷史記錄查看完整逐字稿，或匯出 Word 檔。"
    )


def _line_content_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}


def _line_content_url(message_id: str) -> str:
    return f"https://api-data.line.me/v2/bot/message/{message_id}/content"


def _line_transcoding_url(message_id: str) -> str:
    return f"{_line_content_url(message_id)}/transcoding"


def _wait_line_content_ready(message_id: str) -> bool:
    """Wait for LINE to finish preparing a large audio/video content download."""
    deadline = time.monotonic() + LINE_CONTENT_READY_TIMEOUT_SECONDS
    while time.monotonic() <= deadline:
        resp = requests.get(
            _line_transcoding_url(message_id),
            headers=_line_content_headers(),
            timeout=30,
        )
        if resp.status_code in {404, 410}:
            logger.error(f"❌ LINE 內容已不存在（message_id={message_id}, status={resp.status_code}）")
            return False

        resp.raise_for_status()
        status = (resp.json().get("status") or "").lower()
        if status == "succeeded":
            return True
        if status == "failed":
            logger.error(f"❌ LINE 內容準備失敗（message_id={message_id}）")
            return False

        time.sleep(LINE_CONTENT_READY_POLL_SECONDS)

    logger.error(f"❌ 等待 LINE 內容可下載逾時（message_id={message_id}）")
    return False


def download_line_audio(message_id: str) -> Optional[bytes]:
    """
    透過 LINE Content API 下載語音訊息的音訊位元組。

    Args:
        message_id: LINE 音訊訊息的 Message ID

    Returns:
        音訊位元組（bytes），下載失敗則回傳 None
    """
    url = _line_content_url(message_id)
    try:
        for _ in range(2):
            resp = requests.get(url, headers=_line_content_headers(), timeout=60, stream=True)
            if resp.status_code == 200:
                return resp.content

            if resp.status_code == 202:
                logger.info(f"⏳ LINE 內容尚在準備中，等待可下載（message_id={message_id}）")
                if not _wait_line_content_ready(message_id):
                    return None
                continue

            if resp.status_code in {404, 410}:
                logger.error(f"❌ LINE 內容已不存在（message_id={message_id}, status={resp.status_code}）")
                return None

            resp.raise_for_status()
            return resp.content

        logger.error(f"❌ LINE 內容準備完成後仍無法下載（message_id={message_id}）")
        return None
    except Exception as e:
        logger.error(f"❌ 音檔下載失敗（message_id={message_id}）：{e}")
        return None


# =============================================================================
# 背景任務：下載 + AI 分析 + 推送結果
# =============================================================================

def process_line_audio_in_background(
    job_id: str,
    message_id: str,
    user_id: str,
    model: str,
    file_name: Optional[str] = None,
):
    """
    在 FastAPI BackgroundTasks 中執行的主要處理流程：
      1. 下載 LINE 音訊
      2. 暫存為本機 .m4a 檔案
      3. 呼叫 process_audio_task 進行 AI 分析
      4. 讀取生成的 Markdown，推送給使用者
    """
    api = get_line_api()
    if get_job(job_id) is None:
        try:
            create_job(job_id, task_type="line_audio_processing", source="line")
        except Exception as e:
            logger.warning(f"[{job_id}] ⚠️ 建立 LINE 任務記錄失敗：{e}")

    # ── ① 下載音檔 ──────────────────────────────────────────────────────────
    logger.info(f"[{job_id}] 📥 開始下載 LINE 語音（message_id={message_id}）")
    audio_bytes = download_line_audio(message_id)

    if audio_bytes is None:
        update_job_status(
            job_id,
            "failed",
            "❌ LINE 音檔下載失敗",
            error_detail="LINE 音檔下載失敗",
        )
        if api:
            push_text(api, user_id, "❌ 音檔下載失敗，請稍後再試或重新傳送語音訊息。")
        return

    # ── ② 儲存為暫存檔案 ─────────────────────────────────────────────────────
    suffix = ".m4a"
    meeting_title = None
    if file_name:
        suffix = Path(file_name).suffix.lower()
        meeting_title = Path(file_name).stem
        if suffix not in SUPPORTED_MEDIA_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_MEDIA_FORMATS.keys()))
            update_job_status(
                job_id,
                "failed",
                f"❌ 不支援的 LINE 檔案格式：{suffix or '無副檔名'}",
                error_detail=f"支援格式：{supported}",
            )
            if api:
                push_text(api, user_id, f"❌ 不支援的檔案格式：{suffix or '無副檔名'}\n支援格式：{supported}")
            return

    # LINE 語音訊息固定為 m4a 格式；LINE 檔案訊息保留原始副檔名。
    # 檔名保留 job_id 前綴，讓下方的 glob 搜尋 `meeting_notes_{job_id[:8]}*.md` 正確命中。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_path = SOURCE_AUDIO_DIR / f"{job_id[:8]}_{timestamp}{suffix}"
    temp_audio_path = SOURCE_AUDIO_DIR / f".upload_{job_id[:8]}_{timestamp}{suffix}.tmp"
    try:
        temp_audio_path.write_bytes(audio_bytes)
        audio_path, _ = finalize_source_audio_upload(
            temp_audio_path,
            audio_path,
            hashlib.sha256(audio_bytes).hexdigest(),
            len(audio_bytes),
            SUPPORTED_MEDIA_FORMATS.keys(),
        )
    except OSError as e:
        if temp_audio_path.exists():
            temp_audio_path.unlink()
        logger.error(f"[{job_id}] ❌ 原始音檔保存失敗：{e}")
        update_job_status(
            job_id,
            "failed",
            "❌ 原始音檔保存失敗",
            error_detail=str(e),
        )
        if api:
            push_text(api, user_id, "❌ 原始音檔保存失敗，請稍後再試或重新傳送語音訊息。")
        return

    logger.info(f"[{job_id}] 💾 原始音檔已保存：{audio_path}（{len(audio_bytes)/1024:.1f} KB）")

    # ── ③ 呼叫 AI 背景任務 ───────────────────────────────────────────────────
    try:
        output_path = process_audio_task(
            job_id=job_id,
            audio_path=audio_path,
            output_dir=OUTPUT_DIR,
            model=model,
            meeting_title=meeting_title,
            cleanup_source_audio=False,
        )
    except Exception as e:
        logger.error(f"[{job_id}] ❌ AI 處理失敗：{e}\n{traceback.format_exc()}")
        update_job_status(
            job_id,
            "failed",
            "❌ AI 分析失敗",
            error_detail=str(e),
        )
        if api:
            push_text(api, user_id, f"❌ AI 分析失敗：{e}\n請稍後再試。")
        return

    if output_path is None:
        job = get_job(job_id) or {}
        detail = job.get("error_detail") or job.get("message") or "未知錯誤"
        logger.error(f"[{job_id}] ❌ AI 處理未產生輸出：{detail}")
        if api:
            push_text(api, user_id, f"❌ AI 分析失敗：{detail}\n請稍後再試。")
        return

    # ── ④ 讀取輸出 Markdown，推送給使用者 ───────────────────────────────────
    output_path = Path(output_path)
    if not output_path.exists():
        logger.error(f"[{job_id}] ❌ 找不到輸出 Markdown 檔案：{output_path}")
        update_job_status(
            job_id,
            "failed",
            "⚠️ 會議記錄生成完成，但找不到輸出檔案。",
            error_detail=f"找不到輸出 Markdown 檔案：{output_path}",
        )
        if api:
            push_text(api, user_id, "⚠️ 會議記錄生成完成，但找不到輸出檔案，請聯絡管理員。")
        return

    md_content = output_path.read_text(encoding="utf-8")
    logger.info(f"[{job_id}] ✅ 讀取 Markdown 完成（{len(md_content)} 字元），推送中...")

    if api:
        push_text(api, user_id, build_line_meeting_delivery_message(md_content, output_path))

def process_line_text_in_background(
    user_id: str,
    text: str,
    model: str,
):
    """
    接收 LINE 的文字訊息並呼叫 Gemini 進行聊天回應，然後推播回 LINE。
    """
    api = get_line_api()
    if not api:
        return

    logger.info(f"💬 準備處理文字訊息（user_id={user_id}）")

    try:
        from google import genai
        from google.genai import types
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("找不到 GEMINI_API_KEY 環境變數")

        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=model,
            contents=[text],
            config=types.GenerateContentConfig(
                temperature=0.7,
            )
        )

        reply_msg = response.text or "抱歉，我現在無法回答這個問題。"
        push_text(api, user_id, reply_msg)

    except Exception as e:
        logger.error(f"❌ AI 文字回覆失敗：{e}\n{traceback.format_exc()}")
        push_text(api, user_id, f"❌ AI 回覆失敗：{e}\n請稍後再試。")
