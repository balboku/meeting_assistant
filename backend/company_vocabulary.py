"""Human-approved terminology; immutable per-job snapshots, no model self-learning."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import uuid

from backend import database

ACTIVE_VOCABULARY = ContextVar("meeting_vocabulary", default=None)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS company_vocabulary (
        id INTEGER PRIMARY KEY, canonical TEXT NOT NULL, aliases_json TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'proposed',
        version INTEGER NOT NULL DEFAULT 1, meeting_id INTEGER, local_only INTEGER NOT NULL DEFAULT 0,
        proposed_by TEXT NOT NULL, reviewed_by TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vocabulary_events (
        id INTEGER PRIMARY KEY, term_id INTEGER NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def list_terms():
    with database.get_db() as conn:
        try:
            rows = conn.execute("SELECT * FROM company_vocabulary ORDER BY id DESC").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
    return [dict(row, aliases=json.loads(row["aliases_json"])) for row in rows]


def _term(value):
    result = re.sub(r"\s+", " ", str(value or "")).strip()
    if not 1 <= len(result) <= 80 or re.search(r"[\n\r\[\]`<>]", result):
        raise ValueError("詞彙須為 1–80 字，不可包含標記或換行。")
    return result


def propose(canonical, aliases, note, actor, meeting_id=None):
    canonical = _term(canonical)
    aliases = list(dict.fromkeys(_term(value) for value in aliases))
    if len(aliases) > 20 or canonical in aliases:
        raise ValueError("誤聽詞最多 20 個，且不可與正確詞彙相同。")
    from backend.processing_policy import meeting_is_private
    source = database.get_meeting(meeting_id) if meeting_id else None
    local_only = int(bool(source and meeting_is_private(source)))
    with database.get_db() as conn:
        ensure_schema(conn)
        if meeting_id and not conn.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone():
            raise KeyError("找不到來源會議。")
        cursor = conn.execute("""INSERT INTO company_vocabulary
            (canonical, aliases_json, note, meeting_id, local_only, proposed_by, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", (canonical, json.dumps(aliases, ensure_ascii=False),
                                       note[:1000], meeting_id, local_only, actor, database._now(), database._now()))
        _event(conn, cursor.lastrowid, actor, "proposed", {"canonical": canonical, "aliases": aliases})
        return cursor.lastrowid


def _event(conn, term_id, actor, action, payload):
    conn.execute("""INSERT INTO vocabulary_events(term_id,actor,action,payload_json,created_at)
        VALUES (?,?,?,?,?)""", (term_id, actor, action, json.dumps(payload, ensure_ascii=False), database._now()))


def review(term_id, status, version, actor):
    if status not in {"approved", "disabled"}:
        raise ValueError("請選擇核准或停用。")
    with database.get_db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM company_vocabulary WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise KeyError("找不到詞彙。")
        if row["version"] != version:
            raise ValueError("詞彙已被更新，請重新整理再試。")
        if status == "approved":
            proposed = {row["canonical"].casefold(), *(a.casefold() for a in json.loads(row["aliases_json"]))}
            for other in conn.execute("SELECT * FROM company_vocabulary WHERE status='approved' AND id<>?", (term_id,)):
                used = {other["canonical"].casefold(), *(a.casefold() for a in json.loads(other["aliases_json"]))}
                if proposed & used:
                    raise ValueError("與已核准詞彙重複或衝突，請先停用舊詞彙。")
        conn.execute("""UPDATE company_vocabulary SET status=?,version=version+1,
            reviewed_by=?,updated_at=? WHERE id=?""", (status, actor, database._now(), term_id))
        _event(conn, term_id, actor, status, {"version": version + 1})


def snapshot(*, local_only=False):
    terms = [{"id": row["id"], "version": row["version"], "canonical": row["canonical"], "aliases": row["aliases"]}
             for row in list_terms() if row["status"] == "approved" and (local_only or not row["local_only"])]
    terms.sort(key=lambda row: row["id"])
    digest = hashlib.sha256(json.dumps(terms, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"terms": terms, "sha256": digest}


def prompt_terms(custom, frozen):
    # Explicit per-meeting hints take precedence; pin the same list for retries.
    return list(dict.fromkeys([*(custom or []), *(row["canonical"] for row in frozen["terms"])]))[:40]


@contextmanager
def vocabulary_context(frozen):
    token = ACTIVE_VOCABULARY.set(frozen)
    try:
        yield
    finally:
        ACTIVE_VOCABULARY.reset(token)


def apply_approved_aliases(text):
    frozen = ACTIVE_VOCABULARY.get()
    if not frozen:
        return text
    replacements = {alias: row["canonical"] for row in frozen["terms"] for alias in row["aliases"]}
    if not replacements:
        return text
    # A single pass prevents cascading corrections. ASCII terms require word boundaries.
    alternatives = []
    for alias in sorted(replacements, key=len, reverse=True):
        left = r"(?<![A-Za-z0-9_])" if alias[0].isascii() and alias[0].isalnum() else ""
        right = r"(?![A-Za-z0-9_])" if alias[-1].isascii() and alias[-1].isalnum() else ""
        alternatives.append(left + re.escape(alias) + right)
    return re.sub("|".join(alternatives), lambda match: replacements[match[0]], text)


def preserve_approved_terms(normalize):
    """Explicit human-approved spelling wins over legacy automatic corrections."""
    @wraps(normalize)
    def run(text):
        frozen = ACTIVE_VOCABULARY.get()
        if not text or not frozen or not frozen.get("terms"):
            return normalize(text)
        corrected = apply_approved_aliases(text)
        terms = sorted({row["canonical"] for row in frozen["terms"]}, key=len, reverse=True)
        prefix = "\ue000" + uuid.uuid4().hex + "\ue001"
        markers = {}
        def protect(match):
            marker = prefix + str(len(markers)) + "\ue002"
            markers[marker] = match[0]
            return marker
        protected = re.sub("|".join(re.escape(term) for term in terms), protect, corrected)
        result = normalize(protected)
        for marker, canonical in markers.items():
            result = result.replace(marker, canonical)
        return result
    return run
