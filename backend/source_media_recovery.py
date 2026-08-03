"""Restart processing for retained source media that has no meeting or active job."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.database import list_jobs, list_meeting_source_audio_refs
from backend.job_queue import enqueue_audio_job
from backend.media_validation import validate_media_magic
from backend.models import ErrorResponse, JobResponse, JobStatus
from backend.tasks import GEMINI_MODEL, SUPPORTED_MEDIA_FORMATS


logger = logging.getLogger("MeetingAssistant.SourceMediaRecovery")
ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(os.getenv("MEETING_OUTPUT_DIR") or ROOT_DIR / "output")
SOURCE_AUDIO_DIR = Path(
    os.getenv("MEETING_SOURCE_AUDIO_DIR") or OUTPUT_DIR / "source_audio"
)

router = APIRouter()


def _retained_source_path(filename: str) -> Path:
    clean_name = Path(str(filename or "")).name
    if not clean_name or clean_name != filename or clean_name.startswith((".", "_seg_", "_sub_")):
        raise HTTPException(status_code=400, detail="檔名不合法或不是可重新處理的原始檔。")
    suffix = Path(clean_name).suffix.lower()
    if suffix not in SUPPORTED_MEDIA_FORMATS:
        raise HTTPException(status_code=415, detail=f"不支援的原始媒體格式：{suffix or '<none>'}")
    try:
        source_root = SOURCE_AUDIO_DIR.resolve()
        source_path = (SOURCE_AUDIO_DIR / clean_name).resolve()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="找不到原始檔。") from exc
    if source_path.parent != source_root or not source_path.is_file():
        raise HTTPException(status_code=404, detail="找不到原始檔。")
    if source_path.stat().st_size <= 0:
        raise HTTPException(status_code=400, detail="原始檔沒有可處理的內容。")
    with source_path.open("rb") as source_file:
        magic_error = validate_media_magic(suffix, source_file.read(4096))
    if magic_error:
        raise HTTPException(status_code=415, detail=magic_error)
    return source_path


def _source_is_linked(source_path: Path) -> bool:
    return any(
        Path(str(record.get("source_audio") or "")).name == source_path.name
        for record in list_meeting_source_audio_refs()
    )


def _active_source_job(source_path: Path) -> Optional[dict]:
    for status in ("pending", "processing"):
        for job in list_jobs(limit=1000, status=status):
            payload_path = Path(str((job.get("payload") or {}).get("audio_path") or ""))
            try:
                matches = payload_path.resolve() == source_path
            except OSError:
                matches = payload_path.name == source_path.name
            if matches:
                return job
    return None


@router.post(
    "/source-media/inventory/{filename}/restart",
    response_model=JobResponse,
    status_code=202,
    summary="重新處理未連結原始媒體檔",
    tags=["媒體處理"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
    },
)
async def restart_unlinked_source_media(
    filename: str,
    title: Optional[str] = Query(default=None, max_length=200),
) -> JobResponse:
    """Queue one retained orphan without copying the full file through the browser."""
    source_path = _retained_source_path(filename)
    if _source_is_linked(source_path):
        raise HTTPException(status_code=409, detail="此原始檔已有會議紀錄，請從會議頁使用重新產生。")
    active_job = _active_source_job(source_path)
    if active_job:
        raise HTTPException(
            status_code=409,
            detail=f"此原始檔已有進行中的任務：{active_job.get('job_id')}",
        )

    job_id = str(uuid.uuid4())
    meeting_title = str(title or "").strip() or source_path.stem
    try:
        enqueue_audio_job(
            job_id=job_id,
            audio_path=source_path,
            output_dir=OUTPUT_DIR,
            model=GEMINI_MODEL,
            meeting_title=meeting_title,
            source="source_media_recovery",
            client_recording_warning="此原始媒體曾中斷或未連結會議，已由操作者從原始檔清單重新啟動處理。",
        )
    except Exception as exc:
        logger.exception("Unable to restart retained source media: %s", source_path)
        raise HTTPException(status_code=500, detail=f"重新排入處理失敗：{exc}") from exc

    logger.info("Restarted retained source media %s as job %s", source_path.name, job_id)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message="未連結原始媒體已重新排入可靠處理佇列。",
    )
