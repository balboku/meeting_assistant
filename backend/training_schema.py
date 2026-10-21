"""Additive, lazy schema for courses; existing meetings remain unchanged."""


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS training_courses (
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, processing_mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', version INTEGER NOT NULL DEFAULT 1,
        notes_job_id TEXT, notes_version INTEGER, notes_markdown TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS training_parts (
        id TEXT PRIMARY KEY, course_id INTEGER NOT NULL, client_id TEXT NOT NULL,
        position INTEGER NOT NULL, title TEXT NOT NULL, job_id TEXT UNIQUE,
        meeting_id INTEGER, source_audio TEXT NOT NULL DEFAULT '',
        source_sha256 TEXT NOT NULL DEFAULT '', output_path TEXT NOT NULL DEFAULT '',
        transcript TEXT NOT NULL DEFAULT '', warnings_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL, completed_at TEXT,
        UNIQUE(course_id, client_id), UNIQUE(course_id, position),
        FOREIGN KEY(course_id) REFERENCES training_courses(id),
        FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS training_note_cache (
        course_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, notes_json TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(course_id, fingerprint),
        FOREIGN KEY(course_id) REFERENCES training_courses(id)
    )""")
