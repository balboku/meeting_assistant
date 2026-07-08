"""
=============================================================================
backend/main.py — FastAPI 應用入口與 API 路由定義
=============================================================================
啟動方式：
    uvicorn backend.main:app --reload --port 8000

Swagger UI：
    http://localhost:8000/docs

ReDoc：
    http://localhost:8000/redoc
=============================================================================
"""

import asyncio
import hashlib
import os
import uuid
import logging
import ipaddress
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import aiofiles
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 載入 .env 環境變數
load_dotenv()

from backend.exporter import export_meeting_to_docx
from backend.evidence import SUPPORTED_EVIDENCE_EXTENSIONS, analyze_and_append_evidence

from backend.database import (
    DB_PATH,
    init_db,
    get_job,
    list_jobs,
    count_jobs,
    count_jobs_by_status,
    average_completed_job_seconds,
    list_recent_failed_jobs,
    request_job_cancel,
    requeue_failed_job,
    delete_job,
    list_job_events,
    list_meetings,
    count_meetings,
    get_meeting,
    search_meetings,
    delete_meeting,
)
from backend.models import (
    JobResponse,
    JobStatus,
    JobStatusResponse,
    JobListResponse,
    JobEventsResponse,
    MeetingRecord,
    MeetingDetail,
    MeetingListResponse,
    MeetingEvidenceResponse,
    HealthResponse,
    JobMetrics,
    MetricsResponse,
    AppConfigResponse,
    ErrorResponse,
)
from backend.job_queue import enqueue_audio_job, enqueue_line_audio_job, job_worker
from backend.cleanup import cleanup_stale_temp_files_for_jobs, cleanup_terminal_jobs
from backend.maintenance import run_startup_health_checks, run_startup_maintenance
from backend.media_validation import validate_media_magic
from backend.ngrok_status import get_ngrok_status
from backend.source_audio import finalize_source_audio_upload
from backend.tasks import GEMINI_MODEL, SUMMARY_FALLBACK_MODEL, SUMMARY_MODEL, SUPPORTED_MEDIA_FORMATS
from backend.logging_utils import configure_utf8_logging

# =============================================================================
# 路徑常數
# =============================================================================
ROOT_DIR   = Path(__file__).parent.parent
TEMP_DIR   = Path(os.getenv("MEETING_TEMP_DIR") or ROOT_DIR / "temp")
OUTPUT_DIR = Path(os.getenv("MEETING_OUTPUT_DIR") or ROOT_DIR / "output")
SOURCE_AUDIO_DIR = Path(os.getenv("MEETING_SOURCE_AUDIO_DIR") or OUTPUT_DIR / "source_audio")
BACKUP_DIR = Path(os.getenv("MEETING_BACKUP_DIR") or ROOT_DIR / "backups")
APP_API_KEY = os.getenv("APP_API_KEY", "").strip()
API_KEY_COOKIE_NAME = "meeting_assistant_api_key"
TRUST_LOCAL_NETWORK = os.getenv(
    "MEETING_ASSISTANT_TRUST_LOCAL_NETWORK",
    "1",
).strip().lower() not in {"0", "false", "no", "off"}
TRUSTED_LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "fc00::/7",
    "fe80::/10",
))
SERVER_PORT = int(os.getenv("MEETING_ASSISTANT_PORT", "8001"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 1024 * 1024
JOB_RETENTION_DAYS = int(os.getenv("JOB_RETENTION_DAYS", "30"))
DB_BACKUP_KEEP = int(os.getenv("DB_BACKUP_KEEP", "5"))

# 確保目錄存在
TEMP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# 日誌設定
# =============================================================================
configure_utf8_logging(level=logging.INFO)
logger = logging.getLogger("MeetingAssistant.API")

# =============================================================================
# FastAPI 應用初始化（含 lifespan 啟動事件）
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """應用程式生命週期管理：啟動時初始化資料庫"""
    logger.info("🚀 AI 語音會議助理 Backend 啟動中...")
    init_db()
    run_startup_maintenance(DB_PATH, BACKUP_DIR, backup_keep=DB_BACKUP_KEEP)
    cleanup_stale_temp_files_for_jobs(TEMP_DIR)
    cleanup_terminal_jobs(max_age_days=JOB_RETENTION_DAYS)
    job_worker.start()
    logger.info("✅ 後端服務就緒")
    try:
        yield
    finally:
        job_worker.stop()
        logger.info("👋 後端服務關閉")


app = FastAPI(
    title="AI 語音會議助理 API",
    description="""
## 🎙️ AI Voice Meeting Assistant API

將音訊自動轉換為結構化會議記錄的後端服務。

### 主要功能
- 📤 **音檔上傳**：非同步處理，立即回傳任務 ID
- 📊 **狀態查詢**：即時追蹤處理進度
- 📚 **歷史記錄**：查詢與瀏覽過去的會議記錄
- 🤖 **AI 引擎**：Google Gemini 3.1 Flash Lite（語音辨識 + 摘要一體化）

### 支援格式
`.mp3` `.wav` `.m4a` `.aac` `.ogg` `.flac` `.webm` `.mp4` `.mov` `.avi` `.mkv` `.mpeg` `.mpg` `.wmv`
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

def _host_without_port(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    host = _host_without_port(host)
    if host in {"::1", "[::1]"}:
        return True
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_trusted_local_network_host(host: Optional[str]) -> bool:
    if not TRUST_LOCAL_NETWORK or not host:
        return False
    host = _host_without_port(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in TRUSTED_LOCAL_NETWORKS)


def _request_source_matches(request: Request, predicate) -> bool:
    client_host = request.client.host if request.client else None
    peer_is_loopback = _is_loopback_host(client_host)

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and peer_is_loopback:
        return all(predicate(part.strip()) for part in forwarded_for.split(","))

    real_ip = request.headers.get("x-real-ip")
    if real_ip and peer_is_loopback:
        return predicate(real_ip)

    return predicate(client_host)


def _request_from_loopback(request: Request) -> bool:
    return _request_source_matches(request, _is_loopback_host)


def _request_from_trusted_local_network(request: Request) -> bool:
    return _request_source_matches(request, _is_trusted_local_network_host)


def _valid_api_key(request: Request) -> bool:
    if not APP_API_KEY:
        return False
    supplied = (
        request.headers.get("x-api-key")
        or request.query_params.get("api_key")
        or request.cookies.get(API_KEY_COOKIE_NAME)
    )
    return supplied == APP_API_KEY


@app.middleware("http")
async def restrict_remote_access(request: Request, call_next):
    """Allow LINE publicly; protect management UI except local/trusted network."""
    if request.url.path == "/line-webhook":
        return await call_next(request)

    if (
        _request_from_loopback(request)
        or _request_from_trusted_local_network(request)
        or _valid_api_key(request)
    ):
        response = await call_next(request)
        if APP_API_KEY and request.query_params.get("api_key") == APP_API_KEY:
            response.set_cookie(
                API_KEY_COOKIE_NAME,
                APP_API_KEY,
                httponly=True,
                samesite="lax",
                max_age=7 * 24 * 60 * 60,
                path="/",
            )
        return response

    return JSONResponse(
        status_code=403,
        content={"detail": "此端點只允許本機存取，或需提供有效的 X-API-Key。"},
    )


allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:8001,http://localhost:8001",
    ).split(",")
    if origin.strip()
]

# CORS 設定（桌面工具與本機網頁介面使用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# 靜態檔案與前端介面 (Phase 4)
# =============================================================================
STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/history", summary="歷史記錄 Web 介面", tags=["網頁介面"])
async def history_page():
    """重定向至歷史記錄前端 SPA"""
    return RedirectResponse(url="/static/index.html")


# =============================================================================
# 健康檢查端點
# =============================================================================

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="健康檢查",
    tags=["系統"]
)
async def health_check():
    """確認服務是否正常運行"""
    checks = run_startup_health_checks(
        temp_dir=TEMP_DIR,
        output_dir=OUTPUT_DIR,
        source_audio_dir=SOURCE_AUDIO_DIR,
        static_vendor_dir=STATIC_DIR / "vendor",
        db_path=DB_PATH,
    )
    status = "ok" if all(check["status"] == "ok" for check in checks) else "degraded"
    return HealthResponse(
        status=status,
        version="1.0.0",
        model=GEMINI_MODEL,
        transcription_model=GEMINI_MODEL,
        summary_model=SUMMARY_MODEL,
        summary_fallback_model=SUMMARY_FALLBACK_MODEL,
        checks=checks,
    )


@app.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="系統統計",
    tags=["系統"],
)
async def metrics():
    """回傳本機維運用的任務與會議統計。"""
    by_status = count_jobs_by_status()
    return MetricsResponse(
        generated_at=datetime.now(),
        jobs=JobMetrics(
            total=sum(by_status.values()),
            by_status=by_status,
            average_completed_seconds=average_completed_job_seconds(),
        ),
        recent_errors=list_recent_failed_jobs(limit=5),
        meetings={"total": count_meetings()},
        ngrok=get_ngrok_status(expected_port=SERVER_PORT),
    )


@app.get(
    "/config",
    response_model=AppConfigResponse,
    summary="前端執行期設定",
    tags=["系統"],
)
async def app_config():
    """回傳前端需要顯示與預先驗證的執行期設定。"""
    return AppConfigResponse(
        model=GEMINI_MODEL,
        transcription_model=GEMINI_MODEL,
        summary_model=SUMMARY_MODEL,
        summary_fallback_model=SUMMARY_FALLBACK_MODEL,
        max_upload_mb=MAX_UPLOAD_MB,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        supported_extensions=sorted(SUPPORTED_MEDIA_FORMATS.keys()),
    )


# =============================================================================
# 音檔上傳端點
# =============================================================================

@app.post(
    "/upload-audio",
    response_model=JobResponse,
    summary="上傳音檔並觸發 AI 處理",
    tags=["音檔處理"],
    status_code=202  # 202 Accepted：已接收，處理中
)
async def upload_audio(
    file: UploadFile = File(..., description="要處理的音檔或影片（支援 mp3/wav/m4a/mp4/mov 等）"),
    model: Optional[str] = Form(default=None, description=f"指定 Gemini 模型（預設：{GEMINI_MODEL}）"),
    title: Optional[str] = Form(default=None, description="自訂會議標題（預設使用檔案名稱）"),
    content_length: Optional[int] = Header(default=None, alias="Content-Length"),
):
    """
    上傳音檔，後端立即回傳 `job_id`，並在背景非同步執行 AI 處理。

    請使用 `GET /status/{job_id}` 輪詢結果。
    """
    # --- 驗證副檔名 ---
    if not file.filename:
        raise HTTPException(status_code=400, detail="未提供檔案名稱")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_MEDIA_FORMATS:
        supported = ", ".join(SUPPORTED_MEDIA_FORMATS.keys())
        raise HTTPException(
            status_code=415,
            detail=f"不支援的媒體格式：'{suffix}'。支援格式：{supported}"
        )

    if (
        content_length is not None
        and content_length > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_ALLOWANCE_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail=f"檔案過大，請上傳 {MAX_UPLOAD_MB}MB 以內的媒體檔。"
        )

    # --- 生成唯一任務 ID ---
    job_id = str(uuid.uuid4())

    # --- 儲存至原始音檔保留資料夾 ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = f"{job_id[:8]}_{timestamp}{suffix}"
    source_audio_path = SOURCE_AUDIO_DIR / safe_filename
    temp_source_audio_path = SOURCE_AUDIO_DIR / f".upload_{job_id[:8]}_{timestamp}{suffix}.tmp"
    created_new_source_audio = False

    try:
        bytes_written = 0
        upload_hasher = hashlib.sha256()
        first_chunk = await file.read(4096)
        if len(first_chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"檔案過大，請上傳 {MAX_UPLOAD_MB}MB 以內的媒體檔。"
            )

        async with aiofiles.open(temp_source_audio_path, "wb") as f:
            if first_chunk:
                bytes_written += len(first_chunk)
                magic_error = validate_media_magic(suffix, first_chunk)
                if magic_error:
                    raise HTTPException(status_code=415, detail=magic_error)
                upload_hasher.update(first_chunk)
                await f.write(first_chunk)

            while chunk := await file.read(1024 * 1024):  # 每次讀 1MB
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"檔案過大，請上傳 {MAX_UPLOAD_MB}MB 以內的媒體檔。"
                )
                upload_hasher.update(chunk)
                await f.write(chunk)
        upload_sha256 = upload_hasher.hexdigest()
        source_audio_path, created_new_source_audio = await asyncio.to_thread(
            finalize_source_audio_upload,
            temp_source_audio_path,
            source_audio_path,
            upload_sha256,
            bytes_written,
            SUPPORTED_MEDIA_FORMATS.keys(),
        )
    except HTTPException:
        if temp_source_audio_path.exists():
            temp_source_audio_path.unlink()
        raise
    except Exception as e:
        if temp_source_audio_path.exists():
            temp_source_audio_path.unlink()
        raise HTTPException(status_code=500, detail=f"檔案儲存失敗：{e}")

    # --- 寫入持久化任務佇列 ---
    selected_model = model or GEMINI_MODEL
    try:
        enqueue_audio_job(
            job_id=job_id,
            audio_path=source_audio_path,
            output_dir=OUTPUT_DIR,
            model=selected_model,
            meeting_title=title,
        )
    except Exception as e:
        if created_new_source_audio and source_audio_path.exists():
            source_audio_path.unlink()
        raise HTTPException(status_code=500, detail=f"任務排入佇列失敗：{e}")

    logger.info(f"📥 已接收任務：{job_id}（檔案：{file.filename}，模型：{selected_model}）")

    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message="音檔已接收，已排入可靠處理佇列，請稍後查詢狀態..."
    )


# =============================================================================
# 任務狀態查詢端點
# =============================================================================

def _build_job_status_response(job: dict) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job["job_id"],
        status=JobStatus(job["status"]),
        message=job["message"],
        output_path=job.get("output_path"),
        error_detail=job.get("error_detail"),
        attempts=job.get("attempts"),
        max_attempts=job.get("max_attempts"),
        progress_current=job.get("progress_current"),
        progress_total=job.get("progress_total"),
        created_at=job.get("created_at"),
        completed_at=job.get("completed_at"),
    )


@app.get(
    "/status/{job_id}",
    response_model=JobStatusResponse,
    summary="查詢任務處理狀態",
    tags=["音檔處理"],
    responses={404: {"model": ErrorResponse}}
)
async def get_job_status(job_id: str):
    """查詢特定任務的即時狀態（pending / processing / done / failed）"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"找不到任務：{job_id}")

    return _build_job_status_response(job)


@app.get(
    "/jobs",
    response_model=JobListResponse,
    summary="列出最近的任務狀態",
    tags=["音檔處理"],
)
async def list_recent_jobs(
    status: Optional[JobStatus] = None,
    limit: int = 20,
    offset: int = 0,
):
    """列出任務清單，供本機儀表板快速查看 pending / processing / failed。"""
    safe_limit = min(max(limit, 1), 100)
    status_value = status.value if status else None
    return JobListResponse(
        total=count_jobs(status=status_value),
        jobs=list_jobs(limit=safe_limit, offset=max(offset, 0), status=status_value),
    )


@app.post(
    "/jobs/{job_id}/cancel",
    response_model=JobStatusResponse,
    summary="取消排隊或處理中的任務",
    tags=["音檔處理"],
    responses={404: {"model": ErrorResponse}},
)
async def cancel_job(job_id: str):
    """取消任務；pending 會立即結束，processing 會在下一個可中止檢查點停止。"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"找不到任務：{job_id}")

    request_job_cancel(job_id)
    updated = get_job(job_id)
    return _build_job_status_response(updated)


@app.post(
    "/jobs/{job_id}/retry",
    response_model=JobStatusResponse,
    summary="重新排入失敗任務",
    tags=["音檔處理"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def retry_job(job_id: str):
    """將 failed/cancelled 且 payload 可恢復的任務重新排入佇列。"""
    try:
        updated = requeue_failed_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not updated:
        raise HTTPException(status_code=404, detail=f"找不到任務：{job_id}")
    return _build_job_status_response(updated)


@app.get(
    "/jobs/{job_id}/events",
    response_model=JobEventsResponse,
    summary="查詢任務事件時間線",
    tags=["音檔處理"],
    responses={404: {"model": ErrorResponse}},
)
async def get_job_event_timeline(job_id: str):
    """列出單一任務建立、狀態轉換、重試與取消等事件。"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"找不到任務：{job_id}")

    return JobEventsResponse(job_id=job_id, events=list_job_events(job_id))


@app.delete(
    "/jobs/{job_id}",
    summary="刪除終態任務",
    tags=["音檔處理"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def delete_job_record(job_id: str):
    """刪除 done/failed/cancelled 任務；處理中或等待中的任務需先取消。"""
    deleted = delete_job(job_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail=f"找不到任務：{job_id}")
    if deleted is False:
        raise HTTPException(status_code=409, detail="只能刪除已完成、失敗或取消的任務。")
    return {"status": "success", "message": f"已刪除任務 {job_id}"}


# =============================================================================
# 會議記錄查詢端點
# =============================================================================

@app.get(
    "/meetings",
    response_model=MeetingListResponse,
    summary="列出所有歷史會議記錄",
    tags=["會議記錄"]
)
async def list_all_meetings(
    limit: int = 20,
    offset: int = 0
):
    """取得所有歷史會議記錄清單（依時間倒序，支援分頁）"""
    records = list_meetings(limit=limit, offset=offset)
    total = count_meetings()
    return MeetingListResponse(total=total, records=records)


@app.get(
    "/meetings/search",
    response_model=list[MeetingRecord],
    summary="全文搜尋會議記錄",
    tags=["會議記錄"]
)
async def api_search_meetings(q: str):
    """根據標題或摘要進行全文搜尋"""
    records = search_meetings(q)
    return records


@app.get(
    "/meetings/{meeting_id}",
    response_model=MeetingDetail,
    summary="取得特定會議的完整記錄",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}}
)
async def get_meeting_detail(meeting_id: int):
    """取得指定 ID 的完整會議記錄（含完整 Markdown 內容）"""
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    return MeetingDetail(
        id=record["id"],
        title=record["title"],
        date=record["date"],
        source_audio=record["source_audio"],
        output_path=record["output_path"],
        summary_preview=record.get("summary", "")[:200],
        created_at=record["created_at"],
        full_content=record["full_content"]
    )


@app.get(
    "/meetings/{meeting_id}/markdown",
    response_class=PlainTextResponse,
    summary="下載特定會議的 Markdown 原始內容",
    tags=["會議記錄"]
)
async def get_meeting_markdown(meeting_id: int):
    """直接回傳 Markdown 純文字，方便複製到 Notion / Obsidian"""
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")
    return record["full_content"]


@app.get(
    "/meetings/{meeting_id}/export/docx",
    summary="下載 4-QA-005 V01 格式的 Word 檔",
    tags=["會議記錄"]
)
async def export_meeting_docx(meeting_id: int):
    """將會議記錄填入 4-QA-005 V01 會議紀錄.docx 並下載"""
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    output_dir = Path("output/docx")
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_title = record["title"].replace("/", "_").replace("\\", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"4-QA-005_V01_{safe_title}_{timestamp}.docx"
    output_filepath = output_dir / output_filename

    success = export_meeting_to_docx(record, str(output_filepath))
    if not success:
        raise HTTPException(status_code=500, detail="匯出 Word 檔失敗，請確認範本是否存在。")

    return FileResponse(
        path=output_filepath,
        filename=output_filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@app.post(
    "/meetings/{meeting_id}/evidence",
    response_model=MeetingEvidenceResponse,
    summary="上傳補充資料並追加到會議記錄",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}}
)
async def add_meeting_evidence(
    meeting_id: int,
    file: UploadFile = File(..., description="補充資料（圖片、PDF、文字或 Word 文件）"),
    note: Optional[str] = Form(default=None, description="補充資料備註或希望 AI 特別檢查的問題"),
    model: Optional[str] = Form(default=None, description=f"指定 Gemini 模型（預設：{GEMINI_MODEL}）"),
    content_length: Optional[int] = Header(default=None, alias="Content-Length"),
):
    """分析補充資料，將判讀結果追加到指定會議 Markdown。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="未提供檔案名稱")

    original_filename = Path(file.filename).name
    suffix = Path(original_filename).suffix.lower()
    if suffix not in SUPPORTED_EVIDENCE_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EVIDENCE_EXTENSIONS))
        raise HTTPException(
            status_code=415,
            detail=f"不支援的補充資料格式：'{suffix}'。支援格式：{supported}"
        )

    if (
        content_length is not None
        and content_length > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_ALLOWANCE_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail=f"檔案過大，請上傳 {MAX_UPLOAD_MB}MB 以內的補充資料。"
        )

    temp_evidence_dir = TEMP_DIR / "evidence_uploads"
    temp_evidence_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_evidence_dir / f"{uuid.uuid4().hex}{suffix}"

    try:
        bytes_written = 0
        async with aiofiles.open(temp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"檔案過大，請上傳 {MAX_UPLOAD_MB}MB 以內的補充資料。"
                    )
                await f.write(chunk)

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="上傳的補充資料是空檔案")

        result = analyze_and_append_evidence(
            meeting_id=meeting_id,
            source_path=temp_path,
            original_filename=original_filename,
            note=note,
            model=model,
        )
        return MeetingEvidenceResponse(**result)
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("補充資料分析失敗")
        raise HTTPException(status_code=500, detail=f"補充資料分析失敗：{exc}") from exc
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("無法刪除補充資料暫存檔：%s", temp_path)


@app.delete(
    "/meetings/{meeting_id}",
    summary="刪除特定會議記錄",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}}
)
async def delete_meeting_record(meeting_id: int):
    """刪除指定 ID 的會議記錄（同時嘗試移除 Markdown 檔案）"""
    success = delete_meeting(meeting_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")
    return {"status": "success", "message": f"已刪除會議記錄 ID={meeting_id}"}


# =============================================================================
# LINE Bot Webhook 端點（Phase 3）
# =============================================================================

@app.post(
    "/line-webhook",
    summary="LINE Bot Webhook 接收端點",
    tags=["LINE Bot"],
    status_code=200,
)
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(..., alias="X-Line-Signature"),
    model: Optional[str] = None,
):
    """
    接收 LINE 平台的 Webhook 事件。
    - 自動驗證訊息簽章，防止偽造請求
    - 僅處理 **語音訊息（AudioMessage）**
    - 收到後立即回覆確認，AI 分析在背景執行
    """
    from backend.line_handler import (
        get_webhook_parser, get_line_api,
        reply_text,
        is_line_status_query,
        build_line_status_reply,
    )
    from linebot.v3.exceptions import InvalidSignatureError
    from linebot.v3.webhooks import MessageEvent, AudioMessageContent, FileMessageContent, TextMessageContent

    # ── 檢查環境變數 ──────────────────────────────────────────────
    parser = get_webhook_parser()
    if parser is None:
        raise HTTPException(
            status_code=503,
            detail="LINE Bot 尚未設定，請在 .env 中填入 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN。"
        )

    # ── 取得原始 body 進行簽章驗證 ───────────────────────────────
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        events = parser.parse(body_str, x_line_signature)
    except InvalidSignatureError:
        logger.warning("⚠️ LINE Webhook 簽章驗證失敗（可能為偽造請求）")
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    api = get_line_api()
    selected_model = model or GEMINI_MODEL

    # ── 遍歷並處理事件 ────────────────────────────────────────────
    for event in events:
        logger.info(f"🔍 收到 LINE 事件：{type(event)} - 內容：{event.to_dict() if hasattr(event, 'to_dict') else event}")

        if not isinstance(event, MessageEvent):
            logger.info("👉 這不是 MessageEvent，略過")
            continue

        user_id = event.source.user_id
        reply_token = event.reply_token

        logger.info(f"💬 訊息類型：{type(event.message)}")

        # ── 文字訊息：一般聊天對話 ────────────────────────────────
        if isinstance(event.message, TextMessageContent):
            logger.info("👉 是 TextMessageContent")
            user_text = event.message.text
            if is_line_status_query(user_text):
                if api:
                    reply_text(api, reply_token, build_line_status_reply(user_id))
                continue

            if api:
                # 為了避免超出 5 秒回應限制，先用 Reply API 給個快速回應
                reply_text(api, reply_token, "💬 正在為您思考中...")

            # 將使用者的文字丟給 AI 背景處理
            from backend.line_handler import process_line_text_in_background
            background_tasks.add_task(
                process_line_text_in_background,
                user_id=user_id,
                text=user_text,
                model=selected_model,
            )
            continue

        # ── 語音訊息：觸發 AI 分析流程 ───────────────────────────
        if isinstance(event.message, AudioMessageContent):
            logger.info("👉 是 AudioMessageContent")
            message_id = event.message.id
            job_id = str(uuid.uuid4())

            logger.info(f"📨 收到 LINE 語音訊息：user={user_id}, message_id={message_id}, job={job_id}")

            # ① 立即回覆確認，避免 webhook 逾時並用掉一次性的 Reply Token。
            if api:
                reply_text(
                    api, reply_token,
                    f"✅ 已收到語音訊息！"
                    f"\nGemini 正在分析中，完成後我會主動傳送會議記錄給您。"
                    f"\n（任務 ID：{job_id[:8]}）"
                )

            # ② 寫入持久化佇列，由本機 worker 下載 + AI 分析 + 推送
            enqueue_line_audio_job(
                job_id=job_id,
                message_id=message_id,
                user_id=user_id,
                model=selected_model,
            )
            continue

        # ── 檔案訊息：支援使用者直接上傳 mp3/m4a 等媒體檔 ─────────
        if isinstance(event.message, FileMessageContent):
            logger.info("👉 是 FileMessageContent")
            message_id = event.message.id
            file_name = event.message.file_name
            suffix = Path(file_name).suffix.lower()

            if suffix not in SUPPORTED_MEDIA_FORMATS:
                supported = ", ".join(sorted(SUPPORTED_MEDIA_FORMATS.keys()))
                if api:
                    reply_text(
                        api,
                        reply_token,
                        f"❌ 不支援的檔案格式：{suffix or '無副檔名'}\n支援格式：{supported}",
                    )
                continue

            job_id = str(uuid.uuid4())
            logger.info(
                f"📨 收到 LINE 檔案訊息：user={user_id}, file={file_name}, "
                f"message_id={message_id}, job={job_id}"
            )

            if api:
                reply_text(
                    api,
                    reply_token,
                    f"✅ 已收到檔案：{file_name}"
                    f"\nGemini 正在分析中，完成後我會主動傳送會議記錄給您。"
                    f"\n（任務 ID：{job_id[:8]}）"
                )

            enqueue_line_audio_job(
                job_id=job_id,
                message_id=message_id,
                user_id=user_id,
                model=selected_model,
                file_name=file_name,
            )
            continue

        # ── 其他訊息類型：提示使用語音 ───────────────────────────
        if api:
            reply_text(api, reply_token, "請傳送語音訊息，我會幫您轉成會議記錄。")

    return {"status": "ok"}
