"""Relocated Markdown must be included without rewriting production records."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend import database
from backend.maintenance import backup_database, create_record_snapshot, verify_record_snapshot


class RelocatedMarkdownBackupTests(unittest.TestCase):
    def snapshot(self, root, recorded, markdown_dir):
        db_path = root / "meetings.db"
        with mock.patch.object(database, "DB_PATH", db_path):
            database.init_db()
            database.save_meeting(title="relocated", date="2026/10/13",
                                  source_audio="", output_path=str(recorded))
            backup = backup_database(db_path, root / "backups")
            snapshot = create_record_snapshot(backup, root / "backups", markdown_dir=markdown_dir)
        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(connection.execute("SELECT output_path FROM meetings").fetchone()[0], str(recorded))
        return snapshot

    def test_relocated_file_is_included_from_configured_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "configured-output"
            output.mkdir()
            relocated = output / "meeting.md"
            relocated.write_text("# Original meeting", encoding="utf-8")
            snapshot = self.snapshot(root, root / "old-user" / "meeting.md", output)
            self.assertTrue(verify_record_snapshot(snapshot)["recoverability_complete"])
            with zipfile.ZipFile(snapshot) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                markdown = next(entry for entry in manifest["entries"] if entry["category"] == "markdown")
                self.assertEqual(archive.read(markdown["archive_path"]), relocated.read_bytes())

    def test_existing_original_path_has_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded = root / "meeting.md"
            recorded.write_text("original", encoding="utf-8")
            output = root / "configured-output"
            output.mkdir()
            (output / "meeting.md").write_text("different", encoding="utf-8")
            snapshot = self.snapshot(root, recorded, output)
            with zipfile.ZipFile(snapshot) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                markdown = next(entry for entry in manifest["entries"] if entry["category"] == "markdown")
                self.assertEqual(archive.read(markdown["archive_path"]), b"original")

    def test_no_configured_match_remains_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meeting.md").write_text("must not guess", encoding="utf-8")
            snapshot = self.snapshot(root, root / "old-user" / "meeting.md", root / "configured-output")
            self.assertFalse(verify_record_snapshot(snapshot)["recoverability_complete"])

    def test_empty_record_path_does_not_backup_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self.snapshot(root, "", root)
            self.assertFalse(verify_record_snapshot(snapshot)["recoverability_complete"])


if __name__ == "__main__":
    unittest.main()
