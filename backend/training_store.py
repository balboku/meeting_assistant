"""Course lifecycle and durable recording parts, independent of AI clients."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import uuid

from backend import database
from backend.processing_policy import meeting_is_private
from backend.training_schema import ensure_schema
from backend.transcript_sections import _extract_transcript_section_body


def _course(conn, course_id):
    row = conn.execute("SELECT * FROM training_courses WHERE id=?", (course_id,)).fetchone()
    if not row:
        raise KeyError("找不到課程，請返回課程清單。")
    return dict(row)


def _parts(conn, course_id):
    return [dict(row) for row in conn.execute("""SELECT p.*, j.status AS job_status,
        j.message AS job_message, j.progress_current, j.progress_total
        FROM training_parts p LEFT JOIN jobs j ON j.job_id=p.job_id
        WHERE course_id=? ORDER BY position""", (course_id,))]


def _touch(conn, course_id):
    conn.execute("UPDATE training_courses SET version=version+1,updated_at=? WHERE id=?",
                 (database._now(), course_id))


def create_course(title, processing_mode):
    title = str(title).strip()
    if not title or len(title) > 200 or processing_mode not in {"cloud", "local"}:
        raise ValueError("請輸入 1–200 字課程名稱並選擇資料處理方式。")
    with database.get_db() as conn:
        ensure_schema(conn)
        now = database._now()
        cursor = conn.execute("""INSERT INTO training_courses
            (title,processing_mode,created_at,updated_at) VALUES (?,?,?,?)""",
            (title, processing_mode, now, now))
        return cursor.lastrowid


def list_courses():
    with database.get_db() as conn:
        ensure_schema(conn)
        return [dict(row) for row in conn.execute("""SELECT c.*,
            (SELECT count(*) FROM training_parts p WHERE p.course_id=c.id) AS part_count,
            (SELECT count(*) FROM training_parts p WHERE p.course_id=c.id AND p.transcript<>'') AS completed_parts
            FROM training_courses c ORDER BY updated_at DESC,id DESC LIMIT 200""")]


def get_course(course_id, *, include_transcripts=False):
    with database.get_db() as conn:
        ensure_schema(conn)
        course = _course(conn, course_id)
        parts = _parts(conn, course_id)
        job = conn.execute("SELECT status,message FROM jobs WHERE job_id=?",
                           (course["notes_job_id"],)).fetchone()
    for part in parts:
        part["warnings"] = json.loads(part.pop("warnings_json"))
        part["has_transcript"] = bool(part["transcript"])
        part["state"] = "done" if part["has_transcript"] else part.get("job_status") or "saved"
        if not include_transcripts:
            part.pop("transcript")
    course["parts"] = parts
    course["notes_job"] = dict(job) if job else None
    course["notes_current"] = course["notes_version"] == course["version"]
    return course


def reserve_part(course_id, client_id, title=""):
    if not client_id or len(client_id) > 100:
        raise ValueError("段落識別碼無效，請重新整理再試。")
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        course = _course(conn, course_id)
        existing = conn.execute("SELECT * FROM training_parts WHERE course_id=? AND client_id=?",
                                (course_id, client_id)).fetchone()
        if existing:
            return dict(existing)
        if course["status"] != "open":
            raise ValueError("課程已結束，請先重新開課再加入新段落。")
        position = conn.execute("SELECT coalesce(max(position),0)+1 FROM training_parts WHERE course_id=?",
                                (course_id,)).fetchone()[0]
        part_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute("""INSERT INTO training_parts
            (id,course_id,client_id,position,title,job_id,created_at) VALUES (?,?,?,?,?,?,?)""",
            (part_id, course_id, client_id, position, title.strip()[:200] or f"第 {position} 段", job_id, database._now()))
        _touch(conn, course_id)
        return dict(conn.execute("SELECT * FROM training_parts WHERE id=?", (part_id,)).fetchone())


def upload_target(part_id, mode):
    with database.get_db() as conn:
        ensure_schema(conn)
        row = conn.execute("""SELECT p.*,c.processing_mode FROM training_parts p
            JOIN training_courses c ON c.id=p.course_id WHERE p.id=?""", (part_id,)).fetchone()
        if not row:
            raise KeyError("找不到課程段落，請返回課程重新送出。")
        part = dict(row)
        if part["processing_mode"] != mode:
            raise ValueError("資料處理方式與課程不一致，請返回課程再錄製。")
        if part["meeting_id"]:
            raise ValueError("此段已使用既有紀錄，請建立新段落。")
        return part


def bind_upload(part_id, path, digest, *, mode, warning=""):
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        part = conn.execute("SELECT * FROM training_parts WHERE id=?", (part_id,)).fetchone()
        if not part:
            raise KeyError("找不到課程段落。")
        course = _course(conn, part["course_id"])
        if course["processing_mode"] != mode:
            raise ValueError("此來源須本地處理，請改加入機密課程。")
        if part["source_sha256"] and part["source_sha256"] != digest:
            raise ValueError("此段已保存不同檔案；請建立下一段，避免覆蓋。")
        conn.execute("UPDATE training_parts SET source_audio=?,source_sha256=?,warnings_json=? WHERE id=?",
                     (Path(path).name, digest, json.dumps([warning] if warning else [], ensure_ascii=False), part_id))


def attach_meeting(course_id, meeting_id):
    record = database.get_meeting(meeting_id)
    if not record:
        raise KeyError("找不到原始紀錄。")
    transcript = _extract_transcript_section_body(record.get("full_content") or "")
    if not transcript:
        raise ValueError("此紀錄缺少完整逐字稿，無法加入課程。")
    private = meeting_is_private(record)
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        course = _course(conn, course_id)
        if course["status"] != "open":
            raise ValueError("請先重新開課，再加入紀錄。")
        if private and course["processing_mode"] != "local":
            raise ValueError("機密紀錄僅能加入本地處理課程。")
        client_id = f"meeting:{meeting_id}"
        existing = conn.execute("SELECT id FROM training_parts WHERE course_id=? AND client_id=?",
                                (course_id, client_id)).fetchone()
        if existing:
            return existing["id"]
        position = conn.execute("SELECT coalesce(max(position),0)+1 FROM training_parts WHERE course_id=?",
                                (course_id,)).fetchone()[0]
        part_id, now = str(uuid.uuid4()), database._now()
        warnings = (record.get("quality_report") or {}).get("warnings") or []
        conn.execute("""INSERT INTO training_parts
            (id,course_id,client_id,position,title,meeting_id,source_audio,transcript,warnings_json,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (part_id, course_id, client_id, position, record["title"], meeting_id,
                Path(record["source_audio"]).name, transcript, json.dumps(warnings, ensure_ascii=False), now, now))
        _touch(conn, course_id)
        return part_id


def reorder(course_id, part_ids, version):
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        course = _course(conn, course_id)
        actual = [row["id"] for row in _parts(conn, course_id)]
        if course["version"] != version:
            raise ValueError("課程已變更，請重新整理後再調整順序。")
        if course["status"] != "open" or len(part_ids) != len(set(part_ids)) or set(part_ids) != set(actual):
            raise ValueError("請在開課狀態提供全部段落，不能省略或重複。")
        if part_ids == actual:
            return
        conn.execute("UPDATE training_parts SET position=-position WHERE course_id=?", (course_id,))
        for position, part_id in enumerate(part_ids, 1):
            conn.execute("UPDATE training_parts SET position=? WHERE id=? AND course_id=?", (position, part_id, course_id))
        _touch(conn, course_id)


def _queue_notes(conn, course_id):
    course = _course(conn, course_id)
    parts = _parts(conn, course_id)
    if course["status"] != "closed" or not parts or any(not p["transcript"] for p in parts):
        return
    existing = conn.execute("SELECT status,payload_json FROM jobs WHERE job_id=?", (course["notes_job_id"],)).fetchone()
    if existing and json.loads(existing["payload_json"])["version"] == course["version"]:
        if existing["status"] in {"pending", "processing"} or course["notes_version"] == course["version"]:
            return
    job_id, now = str(uuid.uuid4()), database._now()
    payload = json.dumps({"course_id": course_id, "version": course["version"]})
    conn.execute("""INSERT INTO jobs (job_id,status,message,task_type,source,payload_json,max_attempts,queued_at,updated_at)
        VALUES (?,'pending',?,'training_notes','training',?,5,?,?)""",
        (job_id, "課程已收齊逐字稿，等待產生課程筆記。", payload, now, now))
    database._record_job_event(conn, job_id, "created", "課程筆記已排入背景佇列。")
    conn.execute("UPDATE training_courses SET notes_job_id=?,updated_at=? WHERE id=?", (job_id, now, course_id))


def set_course_status(course_id, status, version):
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        course = _course(conn, course_id)
        if course["version"] != version:
            raise ValueError("課程已變更，請重新整理後再操作。")
        if status not in {"open", "closed"}:
            raise ValueError("課程狀態無效。")
        if status == "closed" and not _parts(conn, course_id):
            raise ValueError("請先錄製或加入至少一段，再結束課程。")
        if status != course["status"]:
            conn.execute("UPDATE training_courses SET status=?,version=version+1,updated_at=? WHERE id=?",
                         (status, database._now(), course_id))
        _queue_notes(conn, course_id)


def finish_transcript(job_id, transcript, audio_path, output_dir, audio_report=None, segment_report=None, check_active=lambda: None):
    """Return None for ordinary meetings; course parts stop before D/R/A generation."""
    with database.get_db() as conn:
        try:
            part = conn.execute("SELECT * FROM training_parts WHERE job_id=?", (job_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
    if not part:
        return None
    if not transcript.strip():
        raise ValueError("此段沒有可用逐字稿，請確認收音後重試。")
    check_active()
    output = Path(output_dir) / f"training_{job_id}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f".{uuid.uuid4().hex}.tmp")
    content = f"# {part['title']}\n\n## 完整逐字稿 (Verbatim Transcript)\n\n{transcript}\n"
    try:
        temporary.write_text(content, encoding="utf-8")
        check_active()
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    warnings = [*json.loads(part["warnings_json"]), *((audio_report or {}).get("warnings") or [])]
    for segment in segment_report or []:
        warnings.extend(segment.get("issues") or [])
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_active()
        conn.execute("""UPDATE training_parts SET transcript=?,output_path=?,completed_at=?,warnings_json=?
            WHERE job_id=?""", (transcript, str(output), database._now(),
                json.dumps(list(dict.fromkeys(warnings)), ensure_ascii=False), job_id))
        _queue_notes(conn, part["course_id"])
    database.update_job_status(job_id, "done", "課程本段逐字稿已完成；可繼續下一段。", output_path=str(output))
    return output


def completed_output(job_id):
    with database.get_db() as conn:
        try:
            row = conn.execute("SELECT transcript,output_path FROM training_parts WHERE job_id=?", (job_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
    if row and row["transcript"] and row["output_path"]:
        path = Path(row["output_path"])
        if path.is_file():
            database.update_job_status(job_id, "done", "已找到本段逐字稿，略過重複轉錄。", output_path=str(path))
            return path
    return None


def source_references():
    with database.get_db() as conn:
        try:
            return {row["source_audio"]: dict(row) for row in conn.execute("""SELECT p.source_audio,
                p.course_id,c.title FROM training_parts p JOIN training_courses c ON c.id=p.course_id
                WHERE p.source_audio<>''""")}
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise


def cached_notes(course_id, fingerprint, value=None):
    with database.get_db() as conn:
        ensure_schema(conn)
        if value is not None:
            conn.execute("INSERT OR REPLACE INTO training_note_cache VALUES (?,?,?,?)",
                         (course_id, fingerprint, json.dumps(value, ensure_ascii=False), database._now()))
        row = conn.execute("SELECT notes_json FROM training_note_cache WHERE course_id=? AND fingerprint=?",
                           (course_id, fingerprint)).fetchone()
        return json.loads(row[0]) if row else None


def publish_notes(course_id, version, job_id, markdown, check_active):
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        check_active()
        course = _course(conn, course_id)
        if course["version"] != version or course["notes_job_id"] != job_id or course["status"] != "closed":
            return False
        conn.execute("UPDATE training_courses SET notes_markdown=?,notes_version=?,updated_at=? WHERE id=?",
                     (markdown, version, database._now(), course_id))
        return True
