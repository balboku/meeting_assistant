"""Confidential transcription: local-only output with explicit human-summary status."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import uuid

from backend import database
from backend.company_vocabulary import ACTIVE_VOCABULARY, apply_approved_aliases
from backend.local_speech import speech_report, transcribe_locally
from backend.processing_policy import source_sha
from backend.transcript_sections import _extract_transcript_section_body, _replace_transcript_section


class LocalProcessingStopped(RuntimeError):
    """Cancellation/lease loss is terminal for this worker, not a retry failure."""


def process_local(values):
    job_id, audio = values["job_id"], Path(values["audio_path"])
    output = Path(values["output_dir"])

    def check():
        owner = values.get("worker_id")
        if owner and not database.job_lease_is_current(job_id, owner, int(values.get("worker_generation") or 0)):
            raise LocalProcessingStopped("本地工作已失去處理權限。")
        if database.is_job_cancel_requested(job_id):
            database.update_job_status(job_id, "cancelled", "本地轉錄已取消，原始錄音已保留。")
            raise LocalProcessingStopped("本地轉錄已取消。")

    check()
    database.update_job_status(job_id, "processing", "正在本機轉錄機密會議，資料不會外送。")
    reuse = values.get("summary_source_path")
    transcript = _extract_transcript_section_body(Path(reuse).read_text(encoding="utf-8")) if reuse else None
    if not transcript:
        transcript = transcribe_locally(audio, check_cancelled=check)
    transcript = apply_approved_aliases(transcript)
    check()
    title = str(values.get("meeting_title") or audio.stem).replace("\n", " ")
    content = f"# {title}\n\n> 本地轉錄草稿：請人工核對逐字稿、整理議題並確認決議與待辦。\n\n"
    content += "## 一、討論摘要 (Discussion Summary)\n待人工整理，請參照下方原始逐字稿。\n\n"
    content += "## 二、最終決議 (Final Decisions)\n尚未經人工確認。\n\n"
    content += "## 三、待辦事項 (Action Items)\n尚未經人工確認。\n"
    content = _replace_transcript_section(content, transcript)
    report = {"score": 50, "label": "本地轉錄待複核", "warnings": ["本地逐字稿需核對；議題、決議與待辦尚待人工整理。"],
              "blocking_segment_indices": [0], "processing_policy": {"mode": "local", "cloud_requests": 0},
              "diarization": speech_report(), "vocabulary_snapshot": ACTIVE_VOCABULARY.get(),
              "recording": {"profile": values.get("recording_profile") or "local_upload", "source_audio_name": audio.name,
                            "source_audio_size_bytes": audio.stat().st_size, "source_audio_sha256": source_sha(audio),
                            "custom_vocabulary": values.get("custom_vocabulary") or []}}
    if speech_report().get("message"):
        report["warnings"].append(speech_report()["message"])
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"local_{job_id}.md"
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        check()
        temporary.replace(path)
        check()
        database.save_meeting(title, datetime.now().strftime("%Y/%m/%d"), audio.name, str(path),
                              summary="本地轉錄完成，待人工整理與複核。", job_id=job_id, quality_report=report)
        database.update_job_status(job_id, "done", "本地逐字稿已完成，請人工整理議題、決議與待辦。", output_path=str(path))
    finally:
        temporary.unlink(missing_ok=True)
    return path
