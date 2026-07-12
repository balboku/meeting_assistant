"""
SQLite-backed durable job queue for local audio processing.

FastAPI BackgroundTasks are tied to the current process. This worker persists
enough task metadata in meetings.db so uploads survive a backend restart and can
be retried without asking the user to upload again.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from backend.database import (
    claim_next_pending_job,
    create_job,
    find_line_job_by_message_id,
    get_job,
    is_job_cancel_requested,
    retry_or_fail_job,
    requeue_interrupted_jobs,
    update_job_status,
)
from backend.tasks import (
    GEMINI_MODEL,
    SUMMARY_FALLBACK_MODEL,
    SUMMARY_MODEL,
    SUMMARY_VERIFIER_MODEL,
    process_audio_task,
)

logger = logging.getLogger("MeetingAssistant.JobQueue")

POLL_INTERVAL_SECONDS = float(os.getenv("JOB_QUEUE_POLL_SECONDS", "2"))
DEFAULT_MAX_ATTEMPTS = int(os.getenv("JOB_QUEUE_MAX_ATTEMPTS", "5"))


def enqueue_audio_job(
    job_id: str,
    audio_path: Path,
    output_dir: Path,
    model: str = GEMINI_MODEL,
    meeting_title: Optional[str] = None,
    source: str = "upload",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    summary_model: Optional[str] = None,
    summary_fallback_model: Optional[str] = None,
    summary_verifier_model: Optional[str] = None,
    force_segment_indices: Optional[list[int]] = None,
    summary_source_path: Optional[Path] = None,
    transcript_reuse_source_path: Optional[Path] = None,
    high_quality_summary: bool = False,
) -> None:
    """Persist an uploaded audio job for the local worker."""
    selected_summary_model = summary_model or SUMMARY_MODEL
    selected_summary_fallback_model = summary_fallback_model or SUMMARY_FALLBACK_MODEL
    selected_summary_verifier_model = summary_verifier_model or SUMMARY_VERIFIER_MODEL
    create_job(
        job_id,
        task_type="audio_processing",
        source=source,
        payload={
            "audio_path": str(audio_path),
            "output_dir": str(output_dir),
            "model": model,
            "summary_model": selected_summary_model,
            "summary_fallback_model": selected_summary_fallback_model,
            "summary_verifier_model": selected_summary_verifier_model,
            "meeting_title": meeting_title,
            "force_segment_indices": sorted(set(force_segment_indices or [])),
            "summary_source_path": str(summary_source_path) if summary_source_path else None,
            "transcript_reuse_source_path": str(transcript_reuse_source_path) if transcript_reuse_source_path else None,
            "high_quality_summary": bool(high_quality_summary),
        },
        max_attempts=max_attempts,
        message="音檔已接收，已排入可靠處理佇列。",
    )


def enqueue_line_audio_job(
    job_id: str,
    message_id: str,
    user_id: str,
    model: str = GEMINI_MODEL,
    file_name: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Persist a LINE audio job for the local worker."""
    existing_job = find_line_job_by_message_id(message_id)
    if existing_job:
        logger.info(
            "↩️  LINE message_id=%s 已有任務 %s，略過重複排程",
            message_id,
            existing_job["job_id"],
        )
        return

    payload = {
        "message_id": message_id,
        "user_id": user_id,
        "model": model,
    }
    if file_name:
        payload["file_name"] = file_name

    create_job(
        job_id,
        task_type="line_audio_processing",
        source="line",
        payload=payload,
        max_attempts=max_attempts,
        message="LINE 媒體已接收，已排入可靠處理佇列。",
    )


class JobQueueWorker:
    """Small single-process polling worker for the SQLite job table."""

    def __init__(self, poll_interval: float = POLL_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            requeued = requeue_interrupted_jobs()
            if requeued:
                logger.info("🔁 已重新排入 %s 個中斷任務", requeued)

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="MeetingAssistantJobQueue",
                daemon=True,
            )
            self._thread.start()
            logger.info("✅ 任務佇列 worker 已啟動")

    def stop(self, timeout: float = 10) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("👋 任務佇列 worker 已停止")

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = claim_next_pending_job()
                if job is None:
                    self._stop_event.wait(self.poll_interval)
                    continue

                self.process_job(job)
            except Exception:
                logger.exception("❌ 任務佇列 worker 發生未預期錯誤")
                self._stop_event.wait(self.poll_interval)

    def process_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        task_type = job.get("task_type")
        logger.info("[%s] ▶️ 開始執行佇列任務：%s", job_id, task_type)

        if is_job_cancel_requested(job_id):
            update_job_status(job_id, "cancelled", "任務已取消。")
            return

        try:
            if task_type == "audio_processing":
                self._process_audio_job(job)
                return

            if task_type == "line_audio_processing":
                self._process_line_audio_job(job)
                return

            raise RuntimeError(f"未知任務類型：{task_type}")
        except Exception as exc:
            resulting_status = retry_or_fail_job(job_id, str(exc))
            self._log_source_audio_retention(job, resulting_status)
            logger.exception("[%s] ❌ 任務執行失敗，狀態：%s", job_id, resulting_status)

    def _process_audio_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        payload = job.get("payload") or {}
        audio_path = Path(payload["audio_path"])
        output_dir = Path(payload["output_dir"])
        model = payload.get("model") or GEMINI_MODEL
        summary_model = payload.get("summary_model") or SUMMARY_MODEL
        summary_fallback_model = payload.get("summary_fallback_model") or SUMMARY_FALLBACK_MODEL
        summary_verifier_model = payload.get("summary_verifier_model") or SUMMARY_VERIFIER_MODEL
        meeting_title = payload.get("meeting_title")
        force_segment_indices = payload.get("force_segment_indices") or []
        summary_source_path = payload.get("summary_source_path")
        transcript_reuse_source_path = payload.get("transcript_reuse_source_path")
        high_quality_summary = bool(payload.get("high_quality_summary"))

        output_path = process_audio_task(
            job_id=job_id,
            audio_path=audio_path,
            output_dir=output_dir,
            model=model,
            meeting_title=meeting_title,
            cleanup_source_audio=False,
            summary_model=summary_model,
            summary_fallback_model=summary_fallback_model,
            summary_verifier_model=summary_verifier_model,
            force_segment_indices=force_segment_indices,
            summary_source_path=Path(summary_source_path) if summary_source_path else None,
            transcript_reuse_source_path=(
                Path(transcript_reuse_source_path) if transcript_reuse_source_path else None
            ),
            high_quality_summary=high_quality_summary,
        )
        if output_path is not None:
            self._log_source_audio_retention(job, "done")
            return

        current = get_job(job_id) or {}
        current_status = current.get("status")
        if current_status == "cancelled":
            self._log_source_audio_retention(job, "cancelled")
            return

        detail = (
            current.get("error_detail")
            or current.get("message")
            or "任務未產生輸出檔案"
        )
        resulting_status = retry_or_fail_job(job_id, detail)
        self._log_source_audio_retention(job, resulting_status)

    def _process_line_audio_job(self, job: dict[str, Any]) -> None:
        payload = job.get("payload") or {}

        from backend.line_handler import process_line_audio_in_background

        process_line_audio_in_background(
            job_id=job["job_id"],
            message_id=payload["message_id"],
            user_id=payload["user_id"],
            model=payload.get("model") or GEMINI_MODEL,
            file_name=payload.get("file_name"),
        )

        current = get_job(job["job_id"]) or {}
        if current.get("status") == "failed":
            retry_or_fail_job(
                job["job_id"],
                current.get("error_detail") or current.get("message") or "LINE 任務失敗",
            )

    def _log_source_audio_retention(self, job: dict[str, Any], status: str) -> None:
        if status in {"failed", "cancelled", "done"}:
            payload = job.get("payload") or {}
            audio_path = payload.get("audio_path")
            if audio_path:
                path = Path(audio_path)
                if path.exists():
                    logger.info("📦 已保留原始音檔：%s", path)
                else:
                    logger.warning("⚠️  原始音檔紀錄存在，但檔案目前不存在：%s", path)


job_worker = JobQueueWorker()
