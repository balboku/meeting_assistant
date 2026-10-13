"""Central outbound policy, including source reuse and background retries."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from backend import database

LOCAL_ONLY = ContextVar("meeting_local_only", default=False)


def cloud_enabled():
    return os.getenv("MEETING_ALLOW_CLOUD", "1").strip().lower() in {"1", "true", "yes"}


def require_cloud():
    if LOCAL_ONLY.get() or not cloud_enabled():
        raise PermissionError("此會議設定為本地處理，禁止外送至雲端。")


def require_cloud_for_meeting(record):
    require_cloud()
    if meeting_is_private(record):
        raise PermissionError("此機密會議僅允許本地處理；請使用人工編輯與本地品質檢查。")


def source_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_source(digest, *, mark=False):
    with database.get_db() as conn:
        if mark:
            conn.execute("""CREATE TABLE IF NOT EXISTS private_sources (
                sha256 TEXT PRIMARY KEY, created_at TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO private_sources VALUES (?,?)", (digest, database._now()))
        try:
            return conn.execute("SELECT 1 FROM private_sources WHERE sha256=?", (digest,)).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return False
            raise


def source_mode(path, requested="cloud"):
    if requested not in {"cloud", "local"}:
        raise ValueError("不支援的處理模式。")
    digest = source_sha(path)
    is_private = private_source(digest, mark=requested == "local")
    return "local" if is_private or not cloud_enabled() else "cloud"


def meeting_is_private(record):
    report = record.get("quality_report") or {}
    if not report and record.get("quality_report_json"):
        try:
            report = json.loads(record["quality_report_json"])
        except (ValueError, TypeError):
            report = {}
    return report.get("processing_policy", {}).get("mode") == "local" or private_source(
        report.get("recording", {}).get("source_audio_sha256", ""))


@contextmanager
def outbound_context(local_only):
    token = LOCAL_ONLY.set(bool(local_only))
    try:
        yield
    finally:
        LOCAL_ONLY.reset(token)
