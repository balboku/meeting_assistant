"""
=============================================================================
backend/models.py — Pydantic 資料模型定義
=============================================================================
定義所有 API 的請求 (Request) 與回應 (Response) 資料結構，
確保型別安全與自動化的 OpenAPI 文件生成。
=============================================================================
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# =============================================================================
# 列舉型別
# =============================================================================

class JobStatus(str, Enum):
    """任務處理狀態列舉"""
    PENDING    = "pending"     # 已接收，等待處理
    PROCESSING = "processing"  # 處理中（上傳媒體檔 / 呼叫 AI）
    DONE       = "done"        # 完成
    FAILED     = "failed"      # 失敗
    CANCELLED  = "cancelled"   # 已取消


# =============================================================================
# 任務相關模型
# =============================================================================

class JobResponse(BaseModel):
    """POST /upload-media（或舊路徑 /upload-audio）的回應格式"""
    job_id: str = Field(..., description="任務唯一識別碼 (UUID)")
    status: JobStatus = Field(..., description="當前任務狀態")
    message: str = Field(..., description="人類可讀的狀態描述")

    model_config = {"json_schema_extra": {
        "example": {
            "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "status": "pending",
            "message": "媒體檔已接收，處理中請稍候..."
        }
    }}


class JobStatusResponse(BaseModel):
    """GET /status/{job_id} 的回應格式"""
    job_id: str
    status: JobStatus
    message: str
    output_path: Optional[str] = Field(None, description="完成後的 Markdown 檔案路徑")
    error_detail: Optional[str] = Field(None, description="失敗時的錯誤訊息")
    attempts: Optional[int] = Field(None, description="已嘗試處理次數")
    max_attempts: Optional[int] = Field(None, description="最多重試次數")
    progress_current: Optional[int] = Field(None, description="目前進度")
    progress_total: Optional[int] = Field(None, description="總進度")
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class JobRecord(BaseModel):
    """任務清單列項目"""
    job_id: str
    status: JobStatus
    message: Optional[str] = None
    source: Optional[str] = None
    task_type: Optional[str] = None
    output_path: Optional[str] = None
    error_detail: Optional[str] = None
    attempts: Optional[int] = None
    max_attempts: Optional[int] = None
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    created_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class JobListResponse(BaseModel):
    """GET /jobs 的回應格式"""
    total: int
    jobs: list[JobRecord]


class JobEventRecord(BaseModel):
    """單一任務事件時間線項目"""
    id: int
    job_id: str
    event_type: str
    message: Optional[str] = None
    detail: Optional[str] = None
    created_at: datetime


class JobEventsResponse(BaseModel):
    """GET /jobs/{job_id}/events 的回應格式"""
    job_id: str
    events: list[JobEventRecord]


class JobMetrics(BaseModel):
    """任務統計摘要"""
    total: int
    by_status: dict[str, int]
    average_completed_seconds: Optional[float] = None


class NgrokStatus(BaseModel):
    """本機 ngrok tunnel 狀態摘要"""
    running: bool
    public_url: Optional[str] = None
    webhook_url: Optional[str] = None
    message: str
    error: Optional[str] = None
    api_url: Optional[str] = None


class StorageFileMetric(BaseModel):
    """單一檔案容量摘要"""
    name: str
    bytes: int
    modified_at: Optional[datetime] = None
    source_media_type: Optional[str] = None
    source_media_sha256: Optional[str] = None
    duplicate_source_media_count: int = 0
    duplicate_source_media_names: list[str] = Field(default_factory=list)
    linked_meeting_id: Optional[int] = None
    linked_meeting_title: Optional[str] = None
    active_job_id: Optional[str] = None
    active_job_status: Optional[str] = None
    active_job_count: int = 0


class StorageMetrics(BaseModel):
    """本機檔案容量摘要"""
    source_media_files: int
    source_media_bytes: int
    source_media_unlinked_files: int = 0
    source_media_unlinked_bytes: int = 0
    source_media_active_job_files: int = 0
    source_media_active_job_bytes: int = 0
    source_media_archived_files: int = 0
    source_media_archived_bytes: int = 0
    source_media_largest_files: list[StorageFileMetric] = Field(default_factory=list)
    meeting_markdown_files: int
    meeting_markdown_bytes: int


class SourceMediaInventoryResponse(BaseModel):
    """GET /source-media/inventory 的回應格式"""
    generated_at: datetime
    total_files: int
    total_bytes: int
    limit: int = 100
    offset: int = 0
    unlinked_files: int = 0
    unlinked_bytes: int = 0
    active_job_files: int = 0
    active_job_bytes: int = 0
    files: list[StorageFileMetric] = Field(default_factory=list)


class SourceMediaDeleteResponse(BaseModel):
    """DELETE /source-media/inventory/{filename} 的回應格式"""
    deleted: bool
    name: str
    bytes: int = 0
    backup_path: Optional[str] = None


class SourceMediaBulkArchiveResponse(BaseModel):
    """POST /source-media/inventory/archive-unlinked response payload."""
    archived: int
    archived_bytes: int = 0
    files: list[SourceMediaDeleteResponse] = Field(default_factory=list)
    skipped: int = 0
    skipped_files: list[str] = Field(default_factory=list)


class SourceMediaArchiveRecord(BaseModel):
    """A source media file that was removed from the live inventory and archived."""
    archive_id: str
    name: str
    archived_name: str
    bytes: int
    metadata_bytes: int = 0
    modified_at: Optional[datetime] = None
    source_media_type: Optional[str] = None
    backup_path: Optional[str] = None


class SourceMediaArchiveResponse(BaseModel):
    """GET /source-media/archive response payload."""
    generated_at: datetime
    total_files: int
    total_bytes: int
    limit: int = 100
    offset: int = 0
    files: list[SourceMediaArchiveRecord] = Field(default_factory=list)


class SourceMediaRestoreResponse(BaseModel):
    """POST /source-media/archive/restore response payload."""
    restored: bool
    archive_id: str
    name: str
    bytes: int = 0
    restored_path: Optional[str] = None


class RecentJobError(BaseModel):
    """最近失敗任務摘要"""
    job_id: str
    status: JobStatus
    message: Optional[str] = None
    error_detail: Optional[str] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class MetricsResponse(BaseModel):
    """GET /metrics 的回應格式"""
    generated_at: datetime
    jobs: JobMetrics
    recent_errors: list[RecentJobError]
    meetings: dict[str, int]
    storage: StorageMetrics
    ngrok: NgrokStatus


class AppConfigResponse(BaseModel):
    """GET /config 的回應格式"""
    model: str
    transcription_model: str
    summary_model: str
    summary_fallback_model: str
    summary_verifier_model: str
    auth: dict[str, Any] = Field(default_factory=dict)
    recording_profiles: dict[str, dict[str, Any]]
    max_upload_mb: int
    max_upload_bytes: int
    supported_extensions: list[str]
    source_media_archive_retention_days: int


# =============================================================================
# 帳號 / 稽核管理模型（預設停用，待未來啟用 RBAC）
# =============================================================================

class AppUserUpsertRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=120, description="同仁顯示名稱")
    role: str = Field("viewer", description="角色：admin、editor 或 viewer")
    is_active: bool = Field(True, description="是否啟用帳號")


class AppUserRecord(BaseModel):
    id: int
    email: str
    display_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AuditLogRecord(BaseModel):
    id: int
    actor_user_id: Optional[int] = None
    actor_email: Optional[str] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    request_method: Optional[str] = None
    request_path: Optional[str] = None
    client_host: Optional[str] = None
    detail: Optional[dict[str, Any]] = None
    created_at: datetime


# =============================================================================
# 會議記錄相關模型
# =============================================================================

class MeetingRecord(BaseModel):
    """會議記錄摘要（用於清單顯示）"""
    id: int
    title: str = Field(..., description="會議標題（來自媒體檔名）")
    date: str = Field(..., description="會議日期 YYYY/MM/DD")
    source_audio: str = Field(..., description="原始媒體檔名")
    output_path: str = Field(..., description="Markdown 檔案路徑")
    summary_preview: Optional[str] = Field(None, description="摘要前 200 字預覽")
    job_id: Optional[str] = Field(None, description="產生此會議記錄的任務 ID")
    quality_score: Optional[int] = Field(None, description="本機品質檢查分數")
    quality_label: Optional[str] = Field(None, description="本機品質檢查結果")
    quality_effective_score: Optional[int] = Field(None, description="依品質警示調整後的顯示分數")
    quality_effective_label: Optional[str] = Field(None, description="依品質警示調整後的顯示結果")
    quality_warning_count: int = Field(0, description="已儲存品質警示數量")
    quality_warning_preview: Optional[str] = Field(None, description="第一個品質警示摘要")
    quality_warning_text: Optional[str] = Field(None, description="完整品質警示文字")
    quality_review_segments: list[str] = Field(default_factory=list, description="需複核的分段標籤")
    quality_review_segment_details: list[dict] = Field(default_factory=list, description="需複核分段與時間範圍")
    quality_review_segment_summary: Optional[str] = Field(None, description="需複核分段、時間與原因摘要")
    quality_review_segment_count: int = Field(0, description="需複核分段數量")
    quality_review_rerunnable_segments: list[int] = Field(default_factory=list, description="可直接指定重跑的零起算分段索引")
    source_media_type: Optional[str] = Field(None, description="原始檔媒體類型：audio 或 video")
    recording_profile: Optional[str] = Field(None, description="錄製或轉檔設定代號")
    source_media_size_bytes: Optional[int] = Field(None, description="原始媒體檔大小")
    source_media_sha256: Optional[str] = Field(None, description="原始媒體 SHA256")
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingDetail(MeetingRecord):
    """會議記錄完整內容（用於單筆查詢）"""
    full_content: str = Field(..., description="完整 Markdown 會議記錄")
    quality_report: Optional[dict] = Field(None, description="音訊與逐段品質報告")


class MeetingRerunRequest(BaseModel):
    """Optionally rerun only selected zero-based transcript segments."""
    segments: Optional[list[int]] = Field(None, description="要強制重跑的零起算分段索引；省略代表全部重跑")
    summary_only: bool = Field(False, description="沿用既有逐字稿，只重新產生摘要、決議與待辦")
    high_quality: bool = Field(False, description="摘要完成後，再用第二模型做一次證據查核")


class MeetingSummaryUpdateRequest(BaseModel):
    summary_markdown: str = Field(
        ...,
        min_length=20,
        max_length=200_000,
        description="只包含討論摘要、最終決議與待辦事項的 Markdown",
    )


class MeetingTranscriptUpdateRequest(BaseModel):
    transcript_markdown: str = Field(
        ...,
        min_length=20,
        max_length=500_000,
        description="只包含完整逐字稿區塊內文，不含摘要、決議與待辦。",
    )


class MeetingSummaryUpdateResponse(BaseModel):
    status: str
    meeting_id: int
    revision_id: int
    full_content: str


class MeetingRevisionRecord(BaseModel):
    id: int
    meeting_id: int
    source: str
    content: str
    created_at: datetime


class MeetingListResponse(BaseModel):
    """GET /meetings 的回應格式"""
    total: int
    records: list[MeetingRecord]


class MeetingEvidenceResponse(BaseModel):
    """POST /meetings/{meeting_id}/evidence 的回應格式"""
    status: str
    meeting_id: int
    file_name: str
    attachment_path: str
    evidence_markdown: str
    full_content: str


# =============================================================================
# 通用回應模型
# =============================================================================

class HealthResponse(BaseModel):
    """GET /health 的回應格式"""
    status: str = "ok"
    version: str = "1.0.0"
    model: str
    transcription_model: str
    summary_model: str
    summary_fallback_model: str
    summary_verifier_model: str
    auth: dict[str, Any] = Field(default_factory=dict)
    recording_profiles: dict[str, dict[str, Any]]
    checks: list[dict[str, str]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """錯誤回應格式"""
    error: str
    detail: Optional[str] = None
