from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from backend import database, maintenance
from backend.backup_policy import local_snapshot_keep


class BackupRetentionTests(unittest.TestCase):
    def test_unset_override_keeps_legacy_policy(self):
        self.assertEqual(local_snapshot_keep(4, {}), 4)
        self.assertEqual(local_snapshot_keep(1, {"MEETING_LOCAL_SNAPSHOT_KEEP":""}), 1)

    def test_explicit_override_never_reduces_below_two(self):
        for raw, expected in [("2",2),("1",2),("0",2),("-3",2),("bad",4),(" 3 ",3)]:
            with self.subTest(raw=raw):
                self.assertEqual(local_snapshot_keep(4, {"MEETING_LOCAL_SNAPSHOT_KEEP":raw}), expected)

    def fixture(self, root):
        source = root / "sources"
        source.mkdir()
        (source / "source.wav").write_bytes(b"retained original audio")
        output = root / "meeting.md"
        output.write_text("# Original meeting\n", encoding="utf-8")
        database.init_db()
        database.save_meeting("Original", "2026/10/22", "source.wav", str(output))
        return source, output

    def test_local_two_does_not_reduce_database_offsite_or_pinned_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backups, offsite = root / "backups", root / "offsite"
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                source, output = self.fixture(root)
                for day in range(1,5):
                    now = datetime(2025,1,day)
                    backup = maintenance.backup_database(database.DB_PATH, backups, now=now, keep=4)
                    snapshot = maintenance.create_record_snapshot(backup, backups, source_media_dir=source, now=now)
                    maintenance.replicate_record_snapshot(snapshot, offsite, keep=4)
                pinned = backups / "release_pinned" / "meeting_records_20240101_000000.zip"
                pinned.parent.mkdir()
                pinned.write_bytes(b"pinned history must not be pruned")
                before = maintenance.record_state_fingerprint(database.DB_PATH, immutable=False)
                with mock.patch.dict(os.environ, {"MEETING_LOCAL_SNAPSHOT_KEEP":"2"}):
                    result = maintenance.run_startup_maintenance(database.DB_PATH, backups,
                        source_media_dir=source, markdown_dir=root, offsite_backup_dir=offsite,
                        backup_keep=4, force_backup=True)
                after = maintenance.record_state_fingerprint(database.DB_PATH, immutable=False)
                self.assertEqual(before, after)
                self.assertEqual(result["retention"], {"database":4,"local_snapshot":2,"offsite_snapshot":4})
                self.assertEqual(len(list(backups.glob("meetings_*.db"))), 4)
                self.assertEqual(len(list(backups.glob("meeting_records_*.zip"))), 2)
                self.assertEqual(len(list(offsite.glob("meeting_records_*.zip"))), 4)
                self.assertTrue(pinned.is_file())
                self.assertEqual((source / "source.wav").read_bytes(), b"retained original audio")
                self.assertEqual(output.read_text(encoding="utf-8"), "# Original meeting\n")
                self.assertTrue(result["snapshot_verification"]["recoverability_complete"])

    def test_incomplete_new_snapshot_cannot_evict_complete_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backups, offsite = root / "backups", root / "offsite"
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                source, _ = self.fixture(root)
                for day in (1,2):
                    now = datetime(2025,1,day)
                    backup = maintenance.backup_database(database.DB_PATH, backups, now=now, keep=4)
                    snapshot = maintenance.create_record_snapshot(backup, backups, source_media_dir=source, now=now)
                    maintenance.replicate_record_snapshot(snapshot, offsite, keep=2)
                complete = set(backups.glob("meeting_records_*.zip"))
                offsite_complete = set(offsite.glob("meeting_records_*.zip"))
                (source / "source.wav").unlink()  # Disposable fixture simulates an unavailable source.
                backup = maintenance.backup_database(database.DB_PATH, backups, keep=4)
                incomplete = maintenance.create_record_snapshot(backup, backups, source_media_dir=source, keep=2)
                self.assertFalse(maintenance.verify_record_snapshot(incomplete)["recoverability_complete"])
                maintenance.replicate_record_snapshot(incomplete, offsite, keep=2)
                self.assertTrue(all(path.is_file() for path in complete | offsite_complete))
                self.assertEqual(len(list(backups.glob("meeting_records_*.zip"))), 3)
                self.assertEqual(len(list(offsite.glob("meeting_records_*.zip"))), 3)


if __name__ == "__main__":
    unittest.main()
