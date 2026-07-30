"""
SQLite-backed durable job queue for local audio processing.

FastAPI BackgroundTasks are tied to the current process. This worker persists
enough task metadata in meetings.db so uploads survive a backend restart and can
be retried without asking the user to upload again.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from backend.database import (
    claim_next_pending_job,
    create_job,
    expire_abandoned_runtime_lease,
    find_line_job_by_message_id,
    get_meeting_by_job_id,
    get_job,
    get_runtime_lease,
    is_job_cancel_requested,
    job_lease_is_current,
    release_runtime_lease,
    renew_job_lease,
    renew_runtime_lease,
    retry_or_fail_job,
    requeue_interrupted_jobs,
    try_acquire_runtime_lease,
    update_job_status,
)
from backend.tasks import (
    GEMINI_MODEL,
    SUMMARY_FALLBACK_MODEL,
    SUMMARY_MODEL,
    SUMMARY_VERIFIER_MODEL,
    TRANSCRIPT_SEMANTIC_REVIEW_MODEL,
    TRANSCRIPT_SEMANTIC_REVIEW_FALLBACK_MODEL,
    process_audio_task,
    recheck_all_saved_meeting_quality_reports,
    review_saved_meeting_transcript_semantics,
)

logger = logging.getLogger("MeetingAssistant.JobQueue")

POLL_INTERVAL_SECONDS = float(os.getenv("JOB_QUEUE_POLL_SECONDS", "2"))
DEFAULT_MAX_ATTEMPTS = int(os.getenv("JOB_QUEUE_MAX_ATTEMPTS", "5"))
WORKER_LEASE_NAME = os.getenv(
    "JOB_QUEUE_LEASE_NAME",
    "meeting-assistant-job-queue",
).strip() or "meeting-assistant-job-queue"
WORKER_LEASE_SECONDS = max(15, int(os.getenv("JOB_QUEUE_LEASE_SECONDS", "90")))
WORKER_HEARTBEAT_SECONDS = max(
    2,
    min(
        int(os.getenv("JOB_QUEUE_HEARTBEAT_SECONDS", "15")),
        max(2, WORKER_LEASE_SECONDS // 3),
    ),
)


def local_worker_process_alive(owner_id: object) -> Optional[bool]:
    """Return local PID liveness; remote/legacy owner formats are unknown."""
    parts = str(owner_id or "").split(":", 2)
    if len(parts) != 3 or parts[0].casefold() != socket.gethostname().casefold():
        return None
    try:
        process_id = int(parts[1])
    except (TypeError, ValueError):
        return None
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    recording_profile: Optional[str] = None,
    client_recording_warning: Optional[str] = None,
    custom_vocabulary: Optional[list[str]] = None,
    force_segment_indices: Optional[list[int]] = None,
    force_full_segment_indices: Optional[list[int]] = None,
    force_full_segment_ranges: Optional[list[dict[str, Any]]] = None,
    force_full_meeting_rerun: bool = False,
    force_all_segments_full_rerun: bool = False,
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
            "recording_profile": recording_profile,
            "client_recording_warning": client_recording_warning,
            "custom_vocabulary": list(custom_vocabulary or []),
            "meeting_title": meeting_title,
            "force_segment_indices": sorted(set(force_segment_indices or [])),
            "force_full_segment_indices": sorted(set(force_full_segment_indices or [])),
            "force_full_segment_ranges": list(force_full_segment_ranges or []),
            "force_full_meeting_rerun": bool(force_full_meeting_rerun),
            "force_all_segments_full_rerun": bool(force_all_segments_full_rerun),
            "summary_source_path": str(summary_source_path) if summary_source_path else None,
            "transcript_reuse_source_path": str(transcript_reuse_source_path) if transcript_reuse_source_path else None,
            "high_quality_summary": bool(high_quality_summary),
        },
        max_attempts=max_attempts,
        message="媒體檔已接收，已排入可靠處理佇列。",
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


def enqueue_meeting_quality_recheck_job(
    job_id: str,
    *,
    source_audio_dir: Path,
) -> None:
    """Queue a no-model quality refresh for all saved meeting transcripts."""
    create_job(
        job_id,
        task_type="meeting_quality_recheck",
        source="quality_recheck",
        payload={"source_audio_dir": str(source_audio_dir)},
        max_attempts=1,
        message="已排入完整逐字稿品質檢核；僅使用本機音訊分析。",
    )


def enqueue_meeting_semantic_review_job(
    job_id: str,
    *,
    meeting_id: int,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
) -> None:
    """Queue a manual text-only semantic review for one meeting transcript."""
    selected_model = str(model or TRANSCRIPT_SEMANTIC_REVIEW_MODEL).strip()
    selected_fallback = str(
        fallback_model or TRANSCRIPT_SEMANTIC_REVIEW_FALLBACK_MODEL
    ).strip()
    create_job(
        job_id,
        task_type="meeting_semantic_review",
        source="semantic_review",
        payload={
            "meeting_id": int(meeting_id),
            "model": selected_model,
            "fallback_model": selected_fallback,
        },
        max_attempts=2,
        message="已排入逐字稿語意品質檢核；只標示疑似失真位置，不會改寫逐字稿。",
    )


class JobQueueWorker:
    """Cross-process singleton worker with fenced leadership and job leases."""

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: int = WORKER_LEASE_SECONDS,
        heartbeat_interval: int = WORKER_HEARTBEAT_SECONDS,
    ):
        self.poll_interval = poll_interval
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self.lease_seconds = max(15, int(lease_seconds))
        self.heartbeat_interval = max(
            2,
            min(int(heartbeat_interval), max(2, self.lease_seconds // 3)),
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._leader_generation: Optional[int] = None
        self._active_job_id: Optional[str] = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="MeetingAssistantJobQueue",
                daemon=True,
            )
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="MeetingAssistantJobHeartbeat",
                daemon=True,
            )
            self._thread.start()
            self._heartbeat_thread.start()
            logger.info("✅ 任務佇列 worker 協調器已啟動：%s", self.worker_id)

    def stop(self, timeout: float = 10) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        heartbeat_thread = self._heartbeat_thread
        if heartbeat_thread and heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=min(timeout, self.heartbeat_interval + 1))

        with self._state_lock:
            generation = self._leader_generation
            active_job_id = self._active_job_id
        if generation is not None and not active_job_id and not (thread and thread.is_alive()):
            try:
                release_runtime_lease(
                    WORKER_LEASE_NAME,
                    self.worker_id,
                    generation,
                )
            except Exception:
                logger.exception("⚠️ 無法釋放 worker leadership lease")
        logger.info("👋 任務佇列 worker 已停止：%s", self.worker_id)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def is_leader(self) -> bool:
        with self._state_lock:
            return self._leader_generation is not None

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": self.is_running(),
                "leader": self._leader_generation is not None,
                "worker_id": self.worker_id,
                "generation": self._leader_generation,
                "active_job_id": self._active_job_id,
                "lease_seconds": self.lease_seconds,
                "heartbeat_seconds": self.heartbeat_interval,
            }

    def _ensure_leadership(self) -> Optional[int]:
        with self._state_lock:
            current = self._leader_generation
        if current is not None:
            return current

        existing_lease = get_runtime_lease(WORKER_LEASE_NAME)
        if (
            existing_lease
            and local_worker_process_alive(existing_lease.get("owner_id")) is False
        ):
            expired = expire_abandoned_runtime_lease(
                WORKER_LEASE_NAME,
                str(existing_lease["owner_id"]),
                int(existing_lease["generation"]),
            )
            if expired:
                logger.warning(
                    "🧹 已確認同主機舊 worker PID 不存在，提前失效 lease：%s",
                    existing_lease["owner_id"],
                )

        generation = try_acquire_runtime_lease(
            WORKER_LEASE_NAME,
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if generation is None:
            return None

        with self._state_lock:
            self._leader_generation = generation
        requeued = requeue_interrupted_jobs(
            legacy_grace_seconds=self.lease_seconds,
        )
        if requeued:
            logger.info("🔁 已重新排入 %s 個 lease 過期任務", requeued)
        logger.info(
            "🗳️ 取得任務 worker leadership：%s generation=%s",
            self.worker_id,
            generation,
        )
        return generation

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                generation = self._ensure_leadership()
                if generation is None:
                    self._stop_event.wait(self.poll_interval)
                    continue

                job = claim_next_pending_job(
                    worker_id=self.worker_id,
                    worker_generation=generation,
                    lease_seconds=self.lease_seconds,
                )
                if job is None:
                    self._stop_event.wait(self.poll_interval)
                    continue

                with self._state_lock:
                    self._active_job_id = str(job["job_id"])
                try:
                    self.process_job(job)
                finally:
                    with self._state_lock:
                        self._active_job_id = None
            except Exception:
                logger.exception("❌ 任務佇列 worker 發生未預期錯誤")
                self._stop_event.wait(self.poll_interval)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval):
            with self._state_lock:
                generation = self._leader_generation
                active_job_id = self._active_job_id
            if generation is None:
                continue
            try:
                renewed = renew_runtime_lease(
                    WORKER_LEASE_NAME,
                    self.worker_id,
                    generation,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    with self._state_lock:
                        if self._leader_generation == generation:
                            self._leader_generation = None
                    logger.error(
                        "🚫 worker leadership 已失效：%s generation=%s",
                        self.worker_id,
                        generation,
                    )
                    continue
                if active_job_id:
                    job_renewed = renew_job_lease(
                        active_job_id,
                        self.worker_id,
                        generation,
                        lease_seconds=self.lease_seconds,
                    )
                    if not job_renewed:
                        logger.warning(
                            "⚠️ 任務 lease 未續期，可能已完成或失去 ownership：%s",
                            active_job_id,
                        )
            except Exception:
                # Keep the current generation during a transient SQLite error.
                # The fencing lease will expire naturally if renewals cannot
                # recover before the configured deadline.
                logger.exception("⚠️ worker heartbeat 更新失敗")

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

            if task_type == "meeting_quality_recheck":
                self._process_meeting_quality_recheck_job(job)
                return

            if task_type == "meeting_semantic_review":
                self._process_meeting_semantic_review_job(job)
                return

            raise RuntimeError(f"未知任務類型：{task_type}")
        except Exception as exc:
            resulting_status = retry_or_fail_job(job_id, str(exc))
            self._log_source_audio_retention(job, resulting_status)
            logger.exception("[%s] ❌ 任務執行失敗，狀態：%s", job_id, resulting_status)

    def _process_audio_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        payload = job.get("payload") or {}
        existing_meeting = get_meeting_by_job_id(job_id)
        if existing_meeting:
            existing_output_path = Path(
                str(existing_meeting.get("output_path") or "")
            )
            if existing_output_path.is_file():
                update_job_status(
                    job_id,
                    "done",
                    "✅ 已找到此任務既有的會議結果，略過重複處理。",
                    output_path=str(existing_output_path),
                )
                logger.info(
                    "[%s] ↩️ 任務已有會議記錄 ID=%s，已冪等完成",
                    job_id,
                    existing_meeting.get("id"),
                )
                return
        audio_path = Path(payload["audio_path"])
        output_dir = Path(payload["output_dir"])
        model = payload.get("model") or GEMINI_MODEL
        summary_model = payload.get("summary_model") or SUMMARY_MODEL
        summary_fallback_model = payload.get("summary_fallback_model") or SUMMARY_FALLBACK_MODEL
        summary_verifier_model = payload.get("summary_verifier_model") or SUMMARY_VERIFIER_MODEL
        recording_profile = payload.get("recording_profile")
        client_recording_warning = payload.get("client_recording_warning")
        custom_vocabulary = payload.get("custom_vocabulary") or []
        meeting_title = payload.get("meeting_title")
        force_segment_indices = payload.get("force_segment_indices") or []
        force_full_segment_indices = payload.get("force_full_segment_indices") or []
        force_full_segment_ranges = payload.get("force_full_segment_ranges") or []
        force_full_meeting_rerun = bool(payload.get("force_full_meeting_rerun"))
        force_all_segments_full_rerun = bool(payload.get("force_all_segments_full_rerun"))
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
            recording_profile=recording_profile,
            client_recording_warning=client_recording_warning,
            custom_vocabulary=custom_vocabulary,
            force_segment_indices=force_segment_indices,
            force_full_segment_indices=force_full_segment_indices,
            force_full_segment_ranges=force_full_segment_ranges,
            force_full_meeting_rerun=force_full_meeting_rerun,
            force_all_segments_full_rerun=force_all_segments_full_rerun,
            summary_source_path=Path(summary_source_path) if summary_source_path else None,
            transcript_reuse_source_path=(
                Path(transcript_reuse_source_path) if transcript_reuse_source_path else None
            ),
            high_quality_summary=high_quality_summary,
            worker_id=job.get("worker_id"),
            worker_generation=job.get("worker_generation"),
        )
        if output_path is not None:
            self._log_source_audio_retention(job, "done")
            return

        current = get_job(job_id) or {}
        current_status = current.get("status")
        if current_status == "cancelled":
            self._log_source_audio_retention(job, "cancelled")
            return
        if current_status == "failed":
            detail = (
                current.get("error_detail")
                or current.get("message")
                or "任務未產生輸出檔案"
            )
            resulting_status = retry_or_fail_job(job_id, detail)
            self._log_source_audio_retention(job, resulting_status)
            return
        claimed_worker_id = str(job.get("worker_id") or "")
        claimed_generation = int(job.get("worker_generation") or 0)
        if claimed_worker_id and not job_lease_is_current(
            job_id,
            claimed_worker_id,
            claimed_generation,
        ):
            logger.warning(
                "[%s] 🚫 原 worker 已失去 lease，不得覆寫 successor 狀態",
                job_id,
            )
            return

        detail = current.get("message") or "任務未產生輸出檔案"
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

    def _process_meeting_quality_recheck_job(self, job: dict[str, Any]) -> None:
        payload = job.get("payload") or {}
        source_audio_dir_value = str(payload.get("source_audio_dir") or "").strip()
        if not source_audio_dir_value:
            raise RuntimeError("品質檢核任務缺少原始媒體保留路徑")
        source_audio_dir = Path(source_audio_dir_value)
        recheck_all_saved_meeting_quality_reports(
            job["job_id"],
            source_audio_dir=source_audio_dir,
        )

    def _process_meeting_semantic_review_job(self, job: dict[str, Any]) -> None:
        payload = job.get("payload") or {}
        try:
            meeting_id = int(payload.get("meeting_id"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("語意品質檢核任務缺少有效會議記錄 ID") from exc
        review_saved_meeting_transcript_semantics(
            job["job_id"],
            meeting_id=meeting_id,
            model=payload.get("model") or TRANSCRIPT_SEMANTIC_REVIEW_MODEL,
            fallback_model=(
                payload.get("fallback_model")
                or TRANSCRIPT_SEMANTIC_REVIEW_FALLBACK_MODEL
            ),
        )

    def _log_source_audio_retention(self, job: dict[str, Any], status: str) -> None:
        if status in {"failed", "cancelled", "done"}:
            payload = job.get("payload") or {}
            audio_path = payload.get("audio_path")
            if audio_path:
                path = Path(audio_path)
                if path.exists():
                    logger.info("📦 已保留原始媒體檔：%s", path)
                else:
                    logger.warning("⚠️  原始媒體檔紀錄存在，但檔案目前不存在：%s", path)


job_worker = JobQueueWorker()
