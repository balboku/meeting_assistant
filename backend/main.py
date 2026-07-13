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
import json
import os
import re
import shutil
import subprocess
import uuid
import logging
import ipaddress
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import aiofiles
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks, HTTPException, Header, Request, Query
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
    list_meeting_source_audio_refs,
    get_meeting,
    search_meetings,
    delete_meeting,
    update_meeting_content_with_revision,
    list_meeting_revisions,
)
from backend.models import (
    JobResponse,
    JobStatus,
    JobStatusResponse,
    JobListResponse,
    JobEventsResponse,
    MeetingRecord,
    MeetingDetail,
    MeetingRerunRequest,
    MeetingSummaryUpdateRequest,
    MeetingTranscriptUpdateRequest,
    MeetingSummaryUpdateResponse,
    MeetingRevisionRecord,
    MeetingListResponse,
    MeetingEvidenceResponse,
    HealthResponse,
    JobMetrics,
    StorageFileMetric,
    StorageMetrics,
    SourceMediaArchiveRecord,
    SourceMediaArchiveResponse,
    SourceMediaDeleteResponse,
    SourceMediaInventoryResponse,
    SourceMediaRestoreResponse,
    MetricsResponse,
    AppConfigResponse,
    ErrorResponse,
)
from backend.job_queue import enqueue_audio_job, enqueue_line_audio_job, job_worker
from backend.auth import auth_config_payload
from backend.cleanup import cleanup_stale_temp_files_for_jobs, cleanup_terminal_jobs
from backend.maintenance import run_startup_health_checks, run_startup_maintenance
from backend.media_validation import validate_media_magic
from backend.ngrok_status import get_ngrok_status
from backend.source_audio import finalize_source_audio_upload
from backend.tasks import (
    GEMINI_MODEL,
    SUMMARY_FALLBACK_MODEL,
    SUMMARY_MODEL,
    SUMMARY_VERIFIER_MODEL,
    SUPPORTED_MEDIA_FORMATS,
    _extract_transcript_section_body,
    _extract_post_transcript_sections,
    _full_transcript_quality_issues,
    _extract_summary_preview,
    _meeting_content_quality_issues,
    _replace_transcript_section,
    _transcript_integrity_issues,
    _transcript_segment_metadata,
)
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
VIDEO_RECORDING_PROFILES = {"video_balanced"}
AUDIO_RECORDING_PROFILES = {"audio_standard", "audio_compact"}
VIDEO_SOURCE_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mpeg", ".mpg", ".wmv"}
FFPROBE_STREAM_CACHE_MAX = 256
FFPROBE_STREAM_CACHE: dict[tuple[str, int, int], set[str]] = {}
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


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


RECORDING_PROFILES = {
    "audio_standard": {
        "label": "標準語音",
        "audio_bps": _positive_int_env("RECORDING_AUDIO_BITRATE", 48_000),
        "audio_sample_rate": _positive_int_env("RECORDING_AUDIO_SAMPLE_RATE", 24_000),
        "audio_channels": _positive_int_env("RECORDING_AUDIO_CHANNELS", 1),
        "video_bps": 0,
        "video_fps": 0,
    },
    "audio_compact": {
        "label": "省容量語音",
        "audio_bps": _positive_int_env("RECORDING_COMPACT_AUDIO_BITRATE", 32_000),
        "audio_sample_rate": _positive_int_env("RECORDING_COMPACT_AUDIO_SAMPLE_RATE", 16_000),
        "audio_channels": 1,
        "video_bps": 0,
        "video_fps": 0,
    },
    "video_balanced": {
        "label": "螢幕/視訊平衡",
        "audio_bps": _positive_int_env("RECORDING_AUDIO_BITRATE", 48_000),
        "audio_sample_rate": _positive_int_env("RECORDING_AUDIO_SAMPLE_RATE", 24_000),
        "audio_channels": _positive_int_env("RECORDING_AUDIO_CHANNELS", 1),
        "video_bps": _positive_int_env("RECORDING_VIDEO_BITRATE", 1_000_000),
        "video_fps": _positive_int_env("RECORDING_VIDEO_FPS", 15),
    },
}

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
    if request.url.path in {"/line-webhook", "/favicon.ico"}:
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

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the browser tab icon from the bundled static assets."""
    favicon_path = STATIC_DIR / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="favicon not found")
    return FileResponse(favicon_path, media_type="image/x-icon")


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
        summary_verifier_model=SUMMARY_VERIFIER_MODEL,
        auth=auth_config_payload(),
        recording_profiles=RECORDING_PROFILES,
        checks=checks,
    )


def _directory_file_stats(
    path: Path,
    allowed_suffixes: set[str],
    *,
    largest_limit: int = 0,
) -> tuple[int, int, list[StorageFileMetric], int, int]:
    """Return file count, bytes, and optional largest files for a shallow scan."""
    count = 0
    total_bytes = 0
    unlinked_count = 0
    unlinked_bytes = 0
    files: list[StorageFileMetric] = []
    try:
        entries = list(path.iterdir())
    except OSError:
        return 0, 0, [], 0, 0

    normalized_suffixes = {suffix.lower() for suffix in allowed_suffixes}
    source_refs = _source_audio_refs_by_name() if path == SOURCE_AUDIO_DIR else None
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
            if normalized_suffixes and entry.suffix.lower() not in normalized_suffixes:
                continue
            stat = entry.stat()
        except OSError:
            continue
        count += 1
        total_bytes += int(stat.st_size)
        linked_ref = source_refs.get(entry.name) if source_refs is not None else None
        if source_refs is not None and linked_ref is None:
            unlinked_count += 1
            unlinked_bytes += int(stat.st_size)
        if largest_limit > 0:
            files.append(
                StorageFileMetric(
                    name=entry.name,
                    bytes=int(stat.st_size),
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    source_media_type=_storage_source_media_type(entry, linked_ref) if source_refs is not None else None,
                    linked_meeting_id=int(linked_ref["id"]) if linked_ref else None,
                    linked_meeting_title=str(linked_ref["title"]) if linked_ref else None,
                )
            )
    largest_files = sorted(files, key=lambda item: item.bytes, reverse=True)[:largest_limit]
    return count, total_bytes, largest_files, unlinked_count, unlinked_bytes


def _source_audio_refs_by_name() -> dict[str, dict]:
    refs: dict[str, dict] = {}
    for row in list_meeting_source_audio_refs():
        source_name = Path(str(row.get("source_audio") or "")).name
        if source_name:
            refs.setdefault(source_name, row)
    return refs


def _storage_source_media_type(entry: Path, linked_ref: Optional[dict] = None) -> Optional[str]:
    if entry.suffix.lower() not in SUPPORTED_MEDIA_FORMATS:
        return None
    record = {
        "source_audio": str(linked_ref.get("source_audio") if linked_ref else entry),
        "quality_report": linked_ref.get("quality_report") if linked_ref else {},
    }
    try:
        return _source_media_type(record, entry)
    except Exception as exc:
        logger.debug("Unable to detect source media type for %s: %s", entry, exc)
        return None


def _source_media_file_by_name(filename: str) -> Path:
    clean_name = Path(str(filename or "")).name
    if not clean_name or clean_name != filename or clean_name.startswith("."):
        raise HTTPException(status_code=400, detail="檔名不合法")
    if Path(clean_name).suffix.lower() not in SUPPORTED_MEDIA_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_MEDIA_FORMATS))
        raise HTTPException(status_code=415, detail=f"不支援的原始檔格式，支援格式：{supported}")

    try:
        source_root = SOURCE_AUDIO_DIR.resolve()
        candidate = (SOURCE_AUDIO_DIR / clean_name).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="找不到原始檔")
    if candidate.parent != source_root:
        raise HTTPException(status_code=400, detail="檔案路徑不合法")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="找不到原始檔")
    return candidate


def _source_media_inventory(limit: int = 100) -> SourceMediaInventoryResponse:
    count = 0
    total_bytes = 0
    unlinked_count = 0
    unlinked_bytes = 0
    files: list[StorageFileMetric] = []
    source_refs = _source_audio_refs_by_name()
    normalized_suffixes = {suffix.lower() for suffix in SUPPORTED_MEDIA_FORMATS}

    try:
        entries = list(SOURCE_AUDIO_DIR.iterdir())
    except OSError:
        entries = []

    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in normalized_suffixes:
                continue
            stat = entry.stat()
        except OSError:
            continue

        count += 1
        file_bytes = int(stat.st_size)
        total_bytes += file_bytes
        linked_ref = source_refs.get(entry.name)
        if linked_ref is None:
            unlinked_count += 1
            unlinked_bytes += file_bytes
        files.append(
            StorageFileMetric(
                name=entry.name,
                bytes=file_bytes,
                modified_at=datetime.fromtimestamp(stat.st_mtime),
                source_media_type=_storage_source_media_type(entry, linked_ref),
                linked_meeting_id=int(linked_ref["id"]) if linked_ref else None,
                linked_meeting_title=str(linked_ref["title"]) if linked_ref else None,
            )
        )

    sorted_files = sorted(files, key=lambda item: item.bytes, reverse=True)
    if limit > 0:
        sorted_files = sorted_files[:limit]

    return SourceMediaInventoryResponse(
        generated_at=datetime.now(),
        total_files=count,
        total_bytes=total_bytes,
        unlinked_files=unlinked_count,
        unlinked_bytes=unlinked_bytes,
        files=sorted_files,
    )


def _source_media_delete_backup_path(source_path: Path) -> Path:
    deleted_dir = _source_media_archive_root() / datetime.now().strftime("%Y%m%d")
    deleted_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S_%f")
    candidate = deleted_dir / f"{timestamp}_{source_path.name}"
    counter = 1
    while candidate.exists():
        candidate = deleted_dir / f"{timestamp}_{counter}_{source_path.name}"
        counter += 1
    return candidate


def _source_media_archive_root() -> Path:
    return BACKUP_DIR / "source_media_deleted"


def _archive_original_name(archived_name: str) -> str:
    parts = Path(archived_name).name.split("_")
    if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return "_".join(parts[3:])
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
        return "_".join(parts[2:])
    return Path(archived_name).name


def _source_media_archive_id(path: Path) -> str:
    return path.relative_to(_source_media_archive_root()).as_posix()


def _source_media_archive_file_by_id(archive_id: str) -> Path:
    raw_id = str(archive_id or "").replace("\\", "/").strip("/")
    parts = [part for part in raw_id.split("/") if part]
    if len(parts) != 2 or not re.fullmatch(r"\d{8}", parts[0]):
        raise HTTPException(status_code=400, detail="備份檔案代碼不正確。")
    archive_name = Path(parts[1]).name
    original_name = _archive_original_name(archive_name)
    if not archive_name or archive_name.startswith(".") or Path(original_name).suffix.lower() not in SUPPORTED_MEDIA_FORMATS:
        raise HTTPException(status_code=400, detail="備份檔案代碼不正確。")
    try:
        archive_root = _source_media_archive_root().resolve()
        candidate = (archive_root / parts[0] / archive_name).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="找不到備份原始檔。")
    if candidate.parent.parent != archive_root:
        raise HTTPException(status_code=400, detail="備份檔案代碼不正確。")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="找不到備份原始檔。")
    return candidate


def _source_media_archive(limit: int = 100) -> SourceMediaArchiveResponse:
    archive_root = _source_media_archive_root()
    files: list[SourceMediaArchiveRecord] = []
    total_files = 0
    total_bytes = 0
    try:
        date_dirs = [entry for entry in archive_root.iterdir() if entry.is_dir() and re.fullmatch(r"\d{8}", entry.name)]
    except OSError:
        date_dirs = []

    for date_dir in date_dirs:
        try:
            entries = list(date_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            original_name = _archive_original_name(entry.name)
            if Path(original_name).suffix.lower() not in SUPPORTED_MEDIA_FORMATS:
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            total_files += 1
            file_bytes = int(stat.st_size)
            total_bytes += file_bytes
            files.append(
                SourceMediaArchiveRecord(
                    archive_id=_source_media_archive_id(entry),
                    name=original_name,
                    archived_name=entry.name,
                    bytes=file_bytes,
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    source_media_type=_storage_source_media_type(entry),
                    backup_path=str(entry),
                )
            )

    sorted_files = sorted(files, key=lambda item: (item.modified_at or datetime.min, item.bytes), reverse=True)[:limit]
    return SourceMediaArchiveResponse(
        generated_at=datetime.now(),
        total_files=total_files,
        total_bytes=total_bytes,
        files=sorted_files,
    )


def _restore_source_media_archive(archive_id: str) -> SourceMediaRestoreResponse:
    archive_path = _source_media_archive_file_by_id(archive_id)
    original_name = _archive_original_name(archive_path.name)
    try:
        source_root = SOURCE_AUDIO_DIR.resolve()
        target_path = (SOURCE_AUDIO_DIR / original_name).resolve()
    except OSError:
        raise HTTPException(status_code=500, detail="無法準備還原路徑。")
    if target_path.parent != source_root:
        raise HTTPException(status_code=400, detail="備份檔案代碼不正確。")
    if target_path.exists():
        raise HTTPException(status_code=409, detail=f"原始檔清單已存在同名檔案：{original_name}")
    try:
        SOURCE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        file_bytes = archive_path.stat().st_size
        shutil.move(str(archive_path), str(target_path))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"還原備份原始檔失敗：{exc}")
    return SourceMediaRestoreResponse(
        restored=True,
        archive_id=archive_id,
        name=original_name,
        bytes=int(file_bytes),
        restored_path=str(target_path),
    )


def _delete_unlinked_source_media(filename: str) -> SourceMediaDeleteResponse:
    source_path = _source_media_file_by_name(filename)
    linked_ref = _source_audio_refs_by_name().get(source_path.name)
    if linked_ref:
        raise HTTPException(
            status_code=409,
            detail=f"原始檔仍連結到會議 #{linked_ref['id']}，不可從維運清單刪除。",
        )
    try:
        file_bytes = source_path.stat().st_size
        backup_path = _source_media_delete_backup_path(source_path)
        shutil.move(str(source_path), str(backup_path))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"刪除原始檔失敗：{exc}")
    return SourceMediaDeleteResponse(
        deleted=True,
        name=source_path.name,
        bytes=int(file_bytes),
        backup_path=str(backup_path),
    )


def _storage_metrics() -> StorageMetrics:
    (
        source_count,
        source_bytes,
        source_largest_files,
        source_unlinked_count,
        source_unlinked_bytes,
    ) = _directory_file_stats(
        SOURCE_AUDIO_DIR,
        set(SUPPORTED_MEDIA_FORMATS),
        largest_limit=5,
    )
    markdown_count, markdown_bytes, _, _, _ = _directory_file_stats(OUTPUT_DIR, {".md"})
    return StorageMetrics(
        source_media_files=source_count,
        source_media_bytes=source_bytes,
        source_media_unlinked_files=source_unlinked_count,
        source_media_unlinked_bytes=source_unlinked_bytes,
        source_media_largest_files=source_largest_files,
        meeting_markdown_files=markdown_count,
        meeting_markdown_bytes=markdown_bytes,
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
        meetings={
            "total": count_meetings(),
            "needs_review": count_meetings(needs_review=True),
        },
        storage=_storage_metrics(),
        ngrok=get_ngrok_status(expected_port=SERVER_PORT),
    )


@app.get(
    "/source-media/inventory",
    response_model=SourceMediaInventoryResponse,
    summary="原始媒體檔案清單",
    tags=["系統"],
)
async def source_media_inventory(limit: int = Query(100, ge=1, le=500)):
    """回傳保留原始錄音/錄影的唯讀維運清單。"""
    return _source_media_inventory(limit=limit)


@app.get(
    "/source-media/archive",
    response_model=SourceMediaArchiveResponse,
    summary="列出已移除原始媒體備份",
    tags=["蝟餌絞"],
)
async def source_media_archive(limit: int = Query(100, ge=1, le=500)):
    """List source media files that were removed from inventory and archived."""
    return _source_media_archive(limit=limit)


@app.post(
    "/source-media/archive/restore",
    response_model=SourceMediaRestoreResponse,
    summary="還原已移除原始媒體備份",
    tags=["蝟餌絞"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def restore_source_media_archive(archive_id: str = Query(..., description="GET /source-media/archive 回傳的 archive_id。")):
    """Restore an archived source media file back to the live source media directory."""
    return _restore_source_media_archive(archive_id)


@app.get(
    "/source-media/inventory/{filename}",
    summary="播放或下載原始媒體檔",
    tags=["系統"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 415: {"model": ErrorResponse}},
)
async def get_source_media_inventory_file(
    filename: str,
    download: bool = Query(False, description="設為 true 時以下載附件形式回傳原始檔。"),
):
    """Return a retained source media file from the maintenance inventory."""
    source_path = _source_media_file_by_name(filename)
    media_type = _source_media_content_type({"source_audio": source_path.name, "quality_report": {}}, source_path)
    return FileResponse(
        path=source_path,
        filename=source_path.name,
        media_type=media_type,
        headers={"Accept-Ranges": "bytes"},
        content_disposition_type="attachment" if download else "inline",
    )


@app.delete(
    "/source-media/inventory/{filename}",
    response_model=SourceMediaDeleteResponse,
    summary="刪除未連結原始媒體檔",
    tags=["系統"],
)
async def delete_unlinked_source_media(filename: str):
    """只允許刪除未連結任何會議的保留原始錄音/錄影檔。"""
    return _delete_unlinked_source_media(filename)


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
        summary_verifier_model=SUMMARY_VERIFIER_MODEL,
        auth=auth_config_payload(),
        recording_profiles=RECORDING_PROFILES,
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
    recording_profile: Optional[str] = Form(default=None, description="瀏覽器錄音品質 profile"),
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
    selected_recording_profile = (recording_profile or "").strip()
    if selected_recording_profile not in RECORDING_PROFILES:
        selected_recording_profile = None
    try:
        enqueue_audio_job(
            job_id=job_id,
            audio_path=source_audio_path,
            output_dir=OUTPUT_DIR,
            model=selected_model,
            meeting_title=title,
            recording_profile=selected_recording_profile,
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


def _resolve_meeting_source_audio(record: dict) -> Path:
    """Find the retained source audio file for an existing meeting record."""
    source_audio = str(record.get("source_audio") or "").strip()
    if not source_audio:
        raise HTTPException(status_code=409, detail="此會議紀錄沒有原始音檔資訊，無法重跑。")

    source_name = Path(source_audio).name
    candidates: list[Path] = []
    raw_path = Path(source_audio)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    if source_name:
        candidates.append(SOURCE_AUDIO_DIR / source_name)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)

        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in SUPPORTED_MEDIA_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_MEDIA_FORMATS))
            raise HTTPException(
                status_code=415,
                detail=f"原始音檔格式不支援：{candidate.suffix or '無副檔名'}。支援格式：{supported}",
            )
        return candidate

    raise HTTPException(
        status_code=409,
        detail=f"找不到保留的原始音檔：{source_name or source_audio}，請重新上傳音檔。",
    )


def _recording_profile(record: dict) -> str:
    quality_report = record.get("quality_report") or {}
    if not isinstance(quality_report, dict):
        return ""
    recording = quality_report.get("recording") or {}
    if not isinstance(recording, dict):
        return ""
    return str(recording.get("profile") or "").strip()


def _optional_source_media_path(record: dict) -> Optional[Path]:
    source_audio = str(record.get("source_audio") or "").strip()
    if not source_audio:
        return None

    raw_path = Path(source_audio)
    candidates = [raw_path] if raw_path.is_absolute() else []
    source_name = raw_path.name
    if source_name:
        candidates.append(SOURCE_AUDIO_DIR / source_name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _ffprobe_stream_types(path: Path) -> set[str]:
    try:
        stat = path.stat()
    except OSError:
        return set()
    cache_key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    cached = FFPROBE_STREAM_CACHE.get(cache_key)
    if cached is not None:
        return set(cached)

    ffprobe = (
        os.getenv("FFPROBE_PATH")
        or os.getenv("FFPROBE_BINARY")
        or shutil.which("ffprobe")
    )
    if not ffprobe:
        return set()

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    if result.returncode != 0:
        return set()
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    stream_types = {
        str(stream.get("codec_type") or "").strip().lower()
        for stream in payload.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type")
    }
    FFPROBE_STREAM_CACHE[cache_key] = set(stream_types)
    if len(FFPROBE_STREAM_CACHE) > FFPROBE_STREAM_CACHE_MAX:
        FFPROBE_STREAM_CACHE.pop(next(iter(FFPROBE_STREAM_CACHE)))
    return stream_types


def _source_media_type(record: dict, source_path: Optional[Path] = None) -> str:
    profile = _recording_profile(record)
    if profile in VIDEO_RECORDING_PROFILES:
        return "video"
    if profile in AUDIO_RECORDING_PROFILES:
        return "audio"

    path = source_path or _optional_source_media_path(record)
    suffix = (path.suffix if path else Path(str(record.get("source_audio") or "")).suffix).lower()
    if path and suffix in VIDEO_SOURCE_EXTENSIONS | {".webm"}:
        stream_types = _ffprobe_stream_types(path)
        if "video" in stream_types:
            return "video"
        if "audio" in stream_types:
            return "audio"

    media_type = SUPPORTED_MEDIA_FORMATS.get(suffix, "")
    if media_type.startswith("video/"):
        return "video"
    return "audio"


def _source_media_detail_metadata(record: dict, source_path: Optional[Path] = None) -> dict[str, object]:
    quality_report = record.get("quality_report") or {}
    recording = quality_report.get("recording") if isinstance(quality_report, dict) else {}
    if not isinstance(recording, dict):
        recording = {}

    profile = str(recording.get("profile") or "").strip() or None
    sha256 = str(recording.get("source_audio_sha256") or "").strip() or None
    if sha256 == "unavailable":
        sha256 = None

    size_bytes: Optional[int]
    raw_size = recording.get("source_audio_size_bytes")
    try:
        size_bytes = int(raw_size) if raw_size not in (None, "") else None
    except (TypeError, ValueError):
        size_bytes = None

    path = source_path or _optional_source_media_path(record)
    if size_bytes is None and path:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None

    return {
        "recording_profile": profile,
        "source_media_size_bytes": size_bytes,
        "source_media_sha256": sha256,
    }


def _source_media_content_type(record: dict, source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    media_kind = _source_media_type(record, source_path)
    if suffix == ".webm":
        return "video/webm" if media_kind == "video" else "audio/webm"
    return SUPPORTED_MEDIA_FORMATS.get(suffix, "application/octet-stream")


def _meeting_records_with_source_media_type(records: list[dict]) -> list[dict]:
    resolved_records: list[dict] = []
    for record in records:
        item = dict(record)
        if not item.get("source_media_type"):
            item["source_media_type"] = _source_media_type(item)
        resolved_records.append(item)
    return resolved_records


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


def _normalize_detail_turn_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return re.sub(r"[，。,.、；;：:！!？?\-—~「」『』（）()\[\]【】\"'`*_]+", "", normalized)


def _detail_transcript_repeated_turn_warning(
    transcript: str,
    *,
    limit: int = 3,
) -> Optional[str]:
    pattern = re.compile(
        r"\[\d{1,3}:[0-5]\d\]\s*(?:\*\*\[[^\]]+\]\*\*|\[[^\]]+\])?\s*[：:]?\s*(?P<text>.+)"
    )
    max_run = 0
    max_text = ""
    current_text = ""
    current_run = 0
    for line in (transcript or "").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        normalized = _normalize_detail_turn_text(match.group("text"))
        if len(normalized) < 8:
            current_text = ""
            current_run = 0
            continue
        if normalized == current_text:
            current_run += 1
        else:
            current_text = normalized
            current_run = 1
        if current_run > max_run:
            max_run = current_run
            max_text = normalized

    if max_run > limit:
        preview = max_text[:24]
        return (
            "逐字稿品質警示：疑似連續重複轉錄"
            f"（同一句連續重複 {max_run} 次：{preview}），建議重跑或複核相關分段。"
        )
    return None


def _detail_section(text: str, heading_terms: tuple[str, ...], next_terms: tuple[str, ...]) -> str:
    heading_pattern = "|".join(re.escape(term) for term in heading_terms)
    next_pattern = "|".join(re.escape(term) for term in next_terms)
    if next_pattern:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*?)(?=^##\s*[^\n]*(?:{next_pattern})|\Z)"
    else:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*)\Z"
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group("body").strip() if match else ""


def _detail_ids(text: str, prefix: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(prefix)}\d+\b", text or ""))


def _detail_summary_quality_issues(full_content: str) -> list[str]:
    summary = _detail_section(
        full_content,
        ("討論摘要", "Discussion Summary"),
        ("最終決議", "Final Decisions"),
    )
    decisions = _detail_section(
        full_content,
        ("最終決議", "Final Decisions"),
        ("待辦事項", "Action Items"),
    )
    actions = _detail_section(
        full_content,
        ("待辦事項", "Action Items"),
        ("完整逐字稿", "Verbatim Transcript"),
    )

    issues: list[str] = []
    summary_ids = _detail_ids(summary, "D")
    decision_ids = _detail_ids(decisions, "R")
    action_ids = _detail_ids(actions, "A")
    decision_discussion_refs = _detail_ids(decisions, "D")
    action_discussion_refs = _detail_ids(actions, "D")
    action_decision_refs = _detail_ids(actions, "R")

    if summary.strip() and not summary_ids:
        issues.append("討論摘要未使用 D 編號，較難與決議及待辦事項串聯")
    if decisions.strip() and not decision_ids:
        issues.append("最終決議未使用 R 編號，較難被待辦事項引用")
    if actions.strip() and not action_ids:
        issues.append("待辦事項未使用 A 編號，後續追蹤較不清楚")

    missing_d_refs = sorted((decision_discussion_refs | action_discussion_refs) - summary_ids)
    if missing_d_refs:
        issues.append(f"決議或待辦引用不存在的討論編號：{', '.join(missing_d_refs)}")

    missing_r_refs = sorted(action_decision_refs - decision_ids)
    if missing_r_refs:
        issues.append(f"待辦事項引用不存在的決議編號：{', '.join(missing_r_refs)}")

    return issues


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
    offset: int = 0,
    needs_review: bool = False
):
    """取得所有歷史會議記錄清單（依時間倒序，支援分頁）"""
    records = _meeting_records_with_source_media_type(
        list_meetings(limit=limit, offset=offset, needs_review=needs_review)
    )
    total = count_meetings(needs_review=needs_review)
    return MeetingListResponse(total=total, records=records)


@app.get(
    "/meetings/search",
    response_model=list[MeetingRecord],
    summary="全文搜尋會議記錄",
    tags=["會議記錄"]
)
async def api_search_meetings(q: str, needs_review: bool = False, limit: int = 50):
    """搜尋標題、音檔、摘要與完整 Markdown 逐字稿內容"""
    records = _meeting_records_with_source_media_type(
        search_meetings(q, limit=limit, needs_review=needs_review)
    )
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

    full_content = record.get("full_content") or ""
    transcript = _extract_transcript_section_body(full_content) or ""
    quality_report = dict(record.get("quality_report") or {})
    if not quality_report.get("segments"):
        recovered_segments = _transcript_segment_metadata(transcript)
        if recovered_segments:
            quality_report.update({
                "score": quality_report.get("score"),
                "label": quality_report.get("label") or "舊紀錄，已重建分段",
                "warnings": quality_report.get("warnings") or [],
                "segments": recovered_segments,
                "timestamp_count": quality_report.get("timestamp_count") or len(re.findall(r"\[\d{1,3}:[0-5]\d\]", transcript)),
                "speaker_labels": quality_report.get("speaker_labels") or [],
            })

    if transcript:
        transcript_warnings = [
            f"逐字稿品質警示：{issue}"
            for issue in _full_transcript_quality_issues(transcript)
        ]
        repeated_turn_warning = _detail_transcript_repeated_turn_warning(transcript)
        if repeated_turn_warning:
            transcript_warnings.append(repeated_turn_warning)
        if transcript_warnings:
            warnings = [
                *list(quality_report.get("warnings") or []),
                *transcript_warnings,
            ]
            quality_report.update({
                "score": quality_report.get("score"),
                "label": quality_report.get("label") or "需複核",
                "warnings": list(dict.fromkeys(warnings)),
                "timestamp_count": quality_report.get("timestamp_count") or len(re.findall(r"\[\d{1,3}:[0-5]\d\]", transcript)),
                "speaker_labels": quality_report.get("speaker_labels") or [],
            })

    summary_warnings = [
        f"摘要品質警示：{issue}"
        for issue in _detail_summary_quality_issues(full_content)
    ]
    if summary_warnings:
        warnings = [
            *list(quality_report.get("warnings") or []),
            *summary_warnings,
        ]
        quality_report.update({
            "score": quality_report.get("score"),
            "label": quality_report.get("label") or "需複核",
            "warnings": list(dict.fromkeys(warnings)),
            "timestamp_count": quality_report.get("timestamp_count") or len(re.findall(r"\[\d{1,3}:[0-5]\d\]", transcript)),
            "speaker_labels": quality_report.get("speaker_labels") or [],
        })

    detail_quality_report = quality_report or record.get("quality_report")
    source_path = _optional_source_media_path(record)
    source_media_type = _source_media_type({
        **record,
        "quality_report": detail_quality_report,
    }, source_path)
    source_media_metadata = _source_media_detail_metadata({
        **record,
        "quality_report": detail_quality_report,
    }, source_path)

    return MeetingDetail(
        id=record["id"],
        title=record["title"],
        date=record["date"],
        source_audio=record["source_audio"],
        output_path=record["output_path"],
        summary_preview=record.get("summary", "")[:200],
        job_id=record.get("job_id"),
        quality_score=record.get("quality_score"),
        quality_label=record.get("quality_label"),
        created_at=record["created_at"],
        full_content=record["full_content"],
        quality_report=quality_report or None,
        source_media_type=source_media_type,
        recording_profile=source_media_metadata["recording_profile"],
        source_media_size_bytes=source_media_metadata["source_media_size_bytes"],
        source_media_sha256=source_media_metadata["source_media_sha256"],
    )


@app.get(
    "/meetings/{meeting_id}/source-media",
    summary="播放或下載會議原始媒體檔",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
@app.get(
    "/meetings/{meeting_id}/source-audio",
    summary="播放或下載會議原始音檔",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def get_meeting_source_media(
    meeting_id: int,
    download: bool = Query(False, description="設為 true 時以下載附件形式回傳原始檔。"),
):
    """Return the retained source audio/video file for evidence review."""
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    audio_path = _resolve_meeting_source_audio(record)
    media_type = _source_media_content_type(record, audio_path)
    return FileResponse(
        path=audio_path,
        filename=audio_path.name,
        media_type=media_type,
        headers={"Accept-Ranges": "bytes"},
        content_disposition_type="attachment" if download else "inline",
    )


@app.put(
    "/meetings/{meeting_id}/summary",
    response_model=MeetingSummaryUpdateResponse,
    summary="人工修訂摘要、決議與待辦並保留版本",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def update_meeting_summary(meeting_id: int, request_body: MeetingSummaryUpdateRequest):
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    original_content = record.get("full_content") or ""
    transcript = _extract_transcript_section_body(original_content)
    if not transcript:
        raise HTTPException(status_code=409, detail="原會議紀錄缺少完整逐字稿，無法安全編輯。")

    summary_markdown = request_body.summary_markdown.strip()
    if re.search(r"(?:Verbatim Transcript|完整逐字稿)", summary_markdown, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="編輯內容不可包含完整逐字稿區塊。")

    frontmatter_match = re.match(
        r"\A---\s*\r?\n.*?\r?\n---\s*(?:\r?\n)?",
        original_content,
        flags=re.DOTALL,
    )
    frontmatter = frontmatter_match.group(0).strip() if frontmatter_match else ""
    edited_body = _replace_transcript_section(summary_markdown, transcript)
    post_transcript_sections = _extract_post_transcript_sections(original_content)
    if post_transcript_sections:
        edited_body = f"{edited_body.rstrip()}\n\n{post_transcript_sections}\n"
    edited_content = f"{frontmatter}\n\n{edited_body}" if frontmatter else edited_body

    issues = [
        *_meeting_content_quality_issues(edited_content),
        *_transcript_integrity_issues(edited_content, transcript),
    ]
    if issues:
        raise HTTPException(
            status_code=400,
            detail="修訂內容格式不完整：" + "；".join(dict.fromkeys(issues)),
        )

    try:
        revision_id = update_meeting_content_with_revision(
            meeting_id,
            edited_content,
            _extract_summary_preview(edited_content),
            source="manual_edit",
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return MeetingSummaryUpdateResponse(
        status="success",
        meeting_id=meeting_id,
        revision_id=revision_id,
        full_content=edited_content,
    )


@app.put(
    "/meetings/{meeting_id}/transcript",
    response_model=MeetingSummaryUpdateResponse,
    summary="人工修訂完整逐字稿並保留版本",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def update_meeting_transcript(meeting_id: int, request_body: MeetingTranscriptUpdateRequest):
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    original_content = record.get("full_content") or ""
    original_transcript = _extract_transcript_section_body(original_content)
    if not original_transcript:
        raise HTTPException(status_code=409, detail="原會議紀錄缺少完整逐字稿，無法安全編輯。")

    transcript_markdown = request_body.transcript_markdown.strip()
    if re.search(
        r"^##\s*[^\n]*(?:討論摘要|最終決議|待辦事項|Discussion Summary|Final Decisions|Action Items)",
        transcript_markdown,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        raise HTTPException(status_code=400, detail="逐字稿編輯內容不可包含摘要、決議或待辦區塊。")

    edited_content = _replace_transcript_section(original_content, transcript_markdown)
    issues = [
        *_meeting_content_quality_issues(edited_content),
        *_transcript_integrity_issues(edited_content, transcript_markdown),
    ]
    if issues:
        raise HTTPException(
            status_code=400,
            detail="逐字稿內容格式不完整：" + "；".join(dict.fromkeys(issues)),
        )

    try:
        revision_id = update_meeting_content_with_revision(
            meeting_id,
            edited_content,
            _extract_summary_preview(edited_content),
            source="manual_transcript_edit",
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return MeetingSummaryUpdateResponse(
        status="success",
        meeting_id=meeting_id,
        revision_id=revision_id,
        full_content=edited_content,
    )


@app.get(
    "/meetings/{meeting_id}/revisions",
    response_model=list[MeetingRevisionRecord],
    summary="查看人工修訂前的歷史版本",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}},
)
async def get_meeting_revisions(meeting_id: int):
    if not get_meeting(meeting_id):
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")
    return list_meeting_revisions(meeting_id)


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
    "/meetings/{meeting_id}/rerun",
    response_model=JobResponse,
    summary="使用原始音檔重跑會議紀錄",
    tags=["會議記錄"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def rerun_meeting_record(
    meeting_id: int,
    request_body: Optional[MeetingRerunRequest] = None,
):
    """用已保留的原始音檔建立新的背景任務，重新產生一筆會議紀錄。"""
    record = get_meeting(meeting_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到會議記錄：ID={meeting_id}")

    audio_path = _resolve_meeting_source_audio(record)
    quality_report = record.get("quality_report") or {}
    known_segments = quality_report.get("segments") or []
    if not known_segments:
        transcript = _extract_transcript_section_body(record.get("full_content") or "") or ""
        known_segments = _transcript_segment_metadata(transcript)
    summary_only = bool(request_body and request_body.summary_only)
    high_quality = bool(request_body and request_body.high_quality)
    summary_source_path = None
    transcript_reuse_source_path = None
    if high_quality and not summary_only:
        raise HTTPException(status_code=400, detail="高品質模式只能用於重整摘要。")
    if summary_only and request_body is not None and request_body.segments is not None:
        raise HTTPException(status_code=400, detail="只重整摘要時不可同時指定重跑分段。")
    if summary_only:
        summary_source_path = Path(record.get("output_path") or "")
        if not summary_source_path.is_file():
            raise HTTPException(status_code=409, detail="原會議紀錄檔不存在，無法沿用逐字稿重整摘要。")
        force_segment_indices = []
    elif request_body is not None and request_body.segments is not None:
        force_segment_indices = sorted(set(request_body.segments))
        if any(index < 0 or index >= len(known_segments) for index in force_segment_indices):
            raise HTTPException(status_code=400, detail="指定的重跑分段不存在。")
        if not force_segment_indices:
            raise HTTPException(status_code=400, detail="請至少指定一個要重跑的分段。")
        transcript_reuse_source_path = Path(record.get("output_path") or "")
        if not transcript_reuse_source_path.is_file():
            raise HTTPException(status_code=409, detail="原會議紀錄檔不存在，無法沿用其他分段逐字稿。")
    else:
        force_segment_indices = list(range(len(known_segments)))

    job_id = str(uuid.uuid4())
    try:
        enqueue_audio_job(
            job_id=job_id,
            audio_path=audio_path,
            output_dir=OUTPUT_DIR,
            model=GEMINI_MODEL,
            meeting_title=record["title"],
            source=(
                "meeting_summary_high_quality"
                if high_quality
                else "meeting_summary_rerun"
                if summary_only
                else "meeting_rerun"
            ),
            force_segment_indices=force_segment_indices,
            summary_source_path=summary_source_path,
            transcript_reuse_source_path=transcript_reuse_source_path,
            high_quality_summary=high_quality,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重跑任務排入佇列失敗：{exc}") from exc

    logger.info(
        "🔁 已建立會議紀錄重跑任務：meeting_id=%s job_id=%s source_audio=%s",
        meeting_id,
        job_id,
        audio_path,
    )
    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message=(
            "已建立高品質摘要重整任務，會由第二模型再做一次證據查核。"
            if high_quality
            else "已沿用既有逐字稿建立摘要重整任務，完成後會產生新的會議紀錄。"
            if summary_only
            else (
                f"已建立指定分段重跑任務：第 {', '.join(str(index + 1) for index in force_segment_indices)} 段。"
                if request_body is not None and request_body.segments is not None
                else "已用原始音檔建立完整重跑任務，完成後會產生新的會議紀錄。"
            )
        ),
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
