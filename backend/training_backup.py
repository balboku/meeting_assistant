"""Include course sources in existing snapshots and relocate them on restore."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def snapshot_parts(db_path, archive, source_dir, entries, missing, archived_sources):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='training_parts'").fetchone():
            return []
        parts = [dict(row) for row in conn.execute("SELECT id,source_audio,source_sha256,job_id FROM training_parts")]
    finally:
        conn.close()
    result = []
    for part in parts:
        if not part["source_audio"]:
            continue
        name = Path(part["source_audio"]).name
        source = Path(source_dir) / name if source_dir else None
        if source is None or not source.is_file():
            missing.append({"category":"source_media","training_part_id":part["id"],"source_path":name})
            continue
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if part["source_sha256"] and digest != part["source_sha256"]:
            missing.append({"category":"source_media","training_part_id":part["id"],"source_path":name,"reason":"sha256 mismatch"})
            continue
        arcname = archived_sources.get(digest)
        if arcname is None:
            arcname = f"source_media/{digest[:2]}/{digest}{source.suffix.lower()}"
            archive.write(source, arcname)
            archived_sources[digest] = arcname
            entries.append({"category":"source_media","training_part_id":part["id"],
                            "archive_path":arcname,"sha256":digest,"bytes":source.stat().st_size,"source_path":str(source)})
        result.append({"id":part["id"],"source_media_archive_path":arcname})
    return result


def restore_parts(conn, manifest, target, output_dir, source_dir):
    restored = 0
    for entry in manifest.get("training_parts") or []:
        part = conn.execute("SELECT title,transcript,job_id FROM training_parts WHERE id=?", (entry["id"],)).fetchone()
        if not part:
            continue
        archived = Path(target) / entry["source_media_archive_path"]
        destination = Path(source_dir) / archived.name
        if not destination.exists():
            shutil.copy2(archived, destination)
            restored += 1
        output_path = ""
        if part[1] and part[2]:
            output = Path(output_dir) / f"training_{part[2]}.md"
            output.write_text(f"# {part[0]}\n\n## 完整逐字稿 (Verbatim Transcript)\n\n{part[1]}\n", encoding="utf-8")
            output_path = str(output)
            restored += 1
        conn.execute("UPDATE training_parts SET source_audio=?,output_path=? WHERE id=?",
                     (destination.name, output_path, entry["id"]))
        if part[2]:
            job = conn.execute("SELECT payload_json FROM jobs WHERE job_id=?", (part[2],)).fetchone()
            if job:
                payload = json.loads(job[0] or "{}")
                payload.update(audio_path=str(destination), output_dir=str(output_dir))
                conn.execute("UPDATE jobs SET output_path=?,payload_json=? WHERE job_id=?",
                             (output_path or None, json.dumps(payload, ensure_ascii=False), part[2]))
    return restored
