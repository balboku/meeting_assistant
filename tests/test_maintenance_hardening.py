from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from backend import database
from backend.maintenance import (
    backup_database,
    create_record_snapshot,
    current_backup_state,
    directory_inventory_fingerprint,
    latest_backup_health,
    latest_record_snapshot_health,
    record_state_fingerprint,
    replicate_record_snapshot,
    run_startup_maintenance,
    verify_database_backup,
    verify_record_snapshot,
)


class MaintenanceHardeningTests(unittest.TestCase):
    def test_backup_pruning_removes_sqlite_sidecars_and_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "meetings.db"
            backup_dir = root / "backups"
            backup_dir.mkdir()
            conn = sqlite3.connect(source)
            try:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()

            stale = backup_dir / "meetings_20250101_000000.db"
            stale.write_bytes(b"stale")
            stale_wal = stale.with_name(f"{stale.name}-wal")
            stale_shm = stale.with_name(f"{stale.name}-shm")
            stale_wal.write_bytes(b"wal")
            stale_shm.write_bytes(b"shm")
            orphan = backup_dir / "meetings_20240101_000000.db-wal"
            orphan.write_bytes(b"orphan")
            retained = backup_dir / "meetings_20990101_000000.db"
            retained.write_bytes(b"retained")
            retained_wal = retained.with_name(f"{retained.name}-wal")
            retained_shm = retained.with_name(f"{retained.name}-shm")
            retained_wal.write_bytes(b"wal")
            retained_shm.write_bytes(b"shm")

            created = backup_database(source, backup_dir, keep=2)

            self.assertTrue(created.is_file())
            self.assertTrue(verify_database_backup(created)["ok"])
            self.assertFalse(stale.exists())
            self.assertFalse(stale_wal.exists())
            self.assertFalse(stale_shm.exists())
            self.assertFalse(orphan.exists())
            self.assertTrue(retained.exists())
            self.assertFalse(retained_wal.exists())
            self.assertFalse(retained_shm.exists())
            self.assertFalse(created.with_name(f"{created.name}-wal").exists())
            self.assertFalse(created.with_name(f"{created.name}-shm").exists())

    def test_snapshot_with_missing_source_is_valid_but_health_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backup_dir = root / "backups"
            output = root / "meeting.md"
            output.write_text("# meeting", encoding="utf-8")
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                database.init_db()
                database.save_meeting(
                    title="missing source",
                    date="2026/07/30",
                    source_audio="missing.webm",
                    output_path=str(output),
                )
                backup = backup_database(database.DB_PATH, backup_dir)
                snapshot = create_record_snapshot(
                    backup,
                    backup_dir,
                    source_media_dir=root / "source_audio",
                )

            verification = verify_record_snapshot(snapshot)
            health = latest_record_snapshot_health(backup_dir)

            self.assertTrue(verification["ok"])
            self.assertTrue(verification["container_valid"])
            self.assertFalse(verification["recoverability_complete"])
            self.assertEqual(verification["status"], "degraded")
            self.assertEqual(verification["missing"], 1)
            self.assertFalse(health["ok"])
            self.assertIn("1 個來源檔案缺漏", health["detail"])

            with mock.patch(
                "backend.maintenance.verify_record_snapshot",
                side_effect=AssertionError("unchanged snapshot should use cache"),
            ):
                cached_health = latest_record_snapshot_health(backup_dir)
            self.assertFalse(cached_health["ok"])
            self.assertTrue(cached_health["container_valid"])

    def test_replicating_unchanged_snapshot_reuses_verified_offsite_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backup_dir = root / "backups"
            offsite_dir = root / "offsite"
            output = root / "meeting.md"
            output.write_text("# meeting", encoding="utf-8")
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                database.init_db()
                database.save_meeting(
                    title="replication reuse",
                    date="2026/07/30",
                    source_audio="",
                    output_path=str(output),
                )
                backup = backup_database(database.DB_PATH, backup_dir)
                snapshot = create_record_snapshot(backup, backup_dir)

            first = replicate_record_snapshot(snapshot, offsite_dir)
            first_mtime = first.stat().st_mtime_ns
            with mock.patch(
                "backend.maintenance.shutil.copy2",
                side_effect=AssertionError("unchanged snapshot must not be copied"),
            ):
                second = replicate_record_snapshot(snapshot, offsite_dir)

            self.assertEqual(first, second)
            self.assertEqual(second.stat().st_mtime_ns, first_mtime)
            self.assertTrue(latest_record_snapshot_health(offsite_dir)["ok"])

    def test_recent_changed_state_waits_for_weekly_backup_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "meetings.db"
            backup_dir = root / "backups"
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                meeting_id = database.save_meeting(
                    title="before",
                    date="2026/07/30",
                    source_audio="",
                    output_path="",
                )
                backup = backup_database(db_path, backup_dir)
                snapshot = create_record_snapshot(backup, backup_dir)
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE meetings SET title='after' WHERE id=?",
                        (meeting_id,),
                    )
                result = run_startup_maintenance(
                    db_path,
                    backup_dir,
                    backup_min_interval_hours=168,
                )

            self.assertFalse(result["backup_created"])
            self.assertTrue(result["snapshot_reused"])
            self.assertTrue(result["data_changed"])
            self.assertFalse(result["interval_due"])
            self.assertEqual(Path(result["backup_path"]), backup)
            self.assertEqual(Path(result["snapshot_path"]), snapshot)

    def test_old_changed_state_creates_weekly_backup_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "meetings.db"
            backup_dir = root / "backups"
            old = datetime.now() - timedelta(days=8)
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                meeting_id = database.save_meeting(
                    title="before",
                    date="2026/07/30",
                    source_audio="",
                    output_path="",
                )
                backup = backup_database(db_path, backup_dir, now=old)
                snapshot = create_record_snapshot(
                    backup,
                    backup_dir,
                    now=old,
                )
                for path in (backup, snapshot):
                    os.utime(path, (old.timestamp(), old.timestamp()))
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE meetings SET title='after' WHERE id=?",
                        (meeting_id,),
                    )
                result = run_startup_maintenance(
                    db_path,
                    backup_dir,
                    backup_min_interval_hours=168,
                )

            self.assertTrue(result["backup_created"])
            self.assertFalse(result["snapshot_reused"])
            self.assertTrue(result["data_changed"])
            self.assertTrue(result["interval_due"])
            self.assertNotEqual(Path(result["backup_path"]), backup)
            self.assertNotEqual(Path(result["snapshot_path"]), snapshot)

    def test_stale_unchanged_backups_remain_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "meetings.db"
            backup_dir = root / "backups"
            old = datetime.now() - timedelta(days=9)
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                backup = backup_database(db_path, backup_dir, now=old)
                snapshot = create_record_snapshot(backup, backup_dir, now=old)
            for path in (backup, snapshot):
                os.utime(path, (old.timestamp(), old.timestamp()))
            state = current_backup_state(db_path)

            database_health = latest_backup_health(
                backup_dir,
                max_age_hours=192,
                current_state=state,
            )
            snapshot_health = latest_record_snapshot_health(
                backup_dir,
                max_age_hours=192,
                current_state=state,
            )

            self.assertFalse(database_health["fresh"])
            self.assertTrue(database_health["state_current"])
            self.assertTrue(database_health["ok"])
            self.assertFalse(snapshot_health["fresh"])
            self.assertTrue(snapshot_health["state_current"])
            self.assertTrue(snapshot_health["ok"])

    def test_force_backup_ignores_recent_unchanged_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "meetings.db"
            backup_dir = root / "backups"
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                backup = backup_database(
                    db_path,
                    backup_dir,
                    now=datetime(2025, 1, 1),
                )
                snapshot = create_record_snapshot(
                    backup,
                    backup_dir,
                    now=datetime(2025, 1, 1),
                )
                result = run_startup_maintenance(
                    db_path,
                    backup_dir,
                    force_backup=True,
                )

            self.assertTrue(result["backup_created"])
            self.assertFalse(result["snapshot_reused"])
            self.assertTrue(result["force_backup"])
            self.assertNotEqual(Path(result["backup_path"]), backup)
            self.assertNotEqual(Path(result["snapshot_path"]), snapshot)

    def test_record_fingerprint_tracks_content_but_not_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "meetings.db"
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                meeting_id = database.save_meeting(
                    title="before",
                    date="2026/07/30",
                    source_audio="source.webm",
                    output_path=str(root / "meeting.md"),
                )
                initial = record_state_fingerprint(db_path)
                generation = database.try_acquire_runtime_lease(
                    "test-runtime",
                    "worker-a",
                    lease_seconds=60,
                )
                after_lease = record_state_fingerprint(db_path)
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE meetings SET title='after' WHERE id=?",
                        (meeting_id,),
                    )
                after_record_change = record_state_fingerprint(db_path)

            self.assertIsNotNone(generation)
            self.assertEqual(initial, after_lease)
            self.assertNotEqual(initial, after_record_change)

    def test_media_inventory_fingerprint_changes_when_file_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            initial = directory_inventory_fingerprint(root)
            media = root / "source.webm"
            media.write_bytes(b"restored")
            after_restore = directory_inventory_fingerprint(root)

            self.assertIsNotNone(initial)
            self.assertNotEqual(initial, after_restore)


if __name__ == "__main__":
    unittest.main()
