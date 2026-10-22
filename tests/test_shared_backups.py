from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from backend import database, maintenance
from backend.backup_storage import backup_storage_metrics
from backend.snapshot_media import object_path, shared_objects, copy_object
from scripts.export_record_snapshot import export_snapshot


class SharedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups, self.offsite = self.root / "backups", self.root / "offsite"
        patch = mock.patch.object(database, "DB_PATH", self.root / "meetings.db")
        patch.start()
        self.addCleanup(patch.stop)
        database.init_db()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.audio = self.sources / "speech.wav"
        self.audio.write_bytes(bytes(range(256)) * 4096)
        self.minutes = self.root / "meeting.md"
        self.minutes.write_text("# 會議\n\n原始會議紀錄", encoding="utf-8")
        database.save_meeting("Original", "2026/10/22", self.audio.name, str(self.minutes))
        self.original = (self.audio.read_bytes(), self.minutes.read_bytes(), maintenance.record_state_fingerprint(database.DB_PATH, immutable=False))

    def snapshot(self, day=1, shared=True, keep=5):
        now = datetime(2026, 1, day)
        db = maintenance.backup_database(database.DB_PATH, self.backups, now=now)
        return maintenance.create_record_snapshot(db, self.backups, source_media_dir=self.sources, now=now, shared_media=shared, keep=keep)

    def manifest(self, path):
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("manifest.json"))

    def media(self, path):
        return next(iter(shared_objects(path, self.manifest(path)).values()))

    def test_same_media_is_stored_once_across_changed_snapshots(self):
        one = self.snapshot()
        self.minutes.write_text("# 新版會議", encoding="utf-8")
        two = self.snapshot(2)
        self.assertEqual(self.media(one), self.media(two))
        self.assertEqual(len(list((self.backups / "media_objects").glob("*/*"))), 1)
        self.assertLess(one.stat().st_size + two.stat().st_size, self.audio.stat().st_size // 4)
        self.assertEqual(self.audio.read_bytes(), self.original[0])
        self.assertTrue(maintenance.verify_record_snapshot(two)["recoverability_complete"])

    def test_changed_media_adds_object_but_keeps_previous_version(self):
        one = self.snapshot()
        self.audio.write_bytes(b"new recording")
        two = self.snapshot(2)
        self.assertNotEqual(self.media(one), self.media(two))
        self.assertEqual(self.media(one).read_bytes(), self.original[0])
        self.assertTrue(maintenance.verify_record_snapshot(one)["ok"])

    def test_restore_shared_and_legacy_without_live_files(self):
        shared = self.snapshot()
        legacy = self.snapshot(2, shared=False)
        self.audio.unlink()  # Disposable fixture only: prove backup independence.
        self.minutes.unlink()
        for index, snapshot in enumerate((shared, legacy)):
            target = self.root / f"restored{index}"
            maintenance.restore_record_snapshot(snapshot, target)
            self.assertIn(self.original[0], [p.read_bytes() for p in (target / "runtime/output/source_audio").iterdir()])
            with self.assertRaises(FileExistsError):
                maintenance.restore_record_snapshot(snapshot, target)

    def test_offsite_has_independent_objects_and_repairs_missing_copy(self):
        snapshot = self.snapshot()
        copied = maintenance.replicate_record_snapshot(snapshot, self.offsite)
        media = self.media(copied)
        self.assertNotEqual(media, self.media(snapshot))
        media.unlink()
        self.assertFalse(maintenance.verify_record_snapshot(copied)["ok"])
        maintenance.replicate_record_snapshot(snapshot, self.offsite)
        self.assertTrue(maintenance.verify_record_snapshot(copied)["ok"])
        self.media(snapshot).unlink()
        self.assertTrue(maintenance.verify_record_snapshot(copied)["ok"])

    def test_corruption_invalidates_cached_health_and_never_overwrites_object(self):
        snapshot = self.snapshot()
        self.assertTrue(maintenance._cached_record_snapshot_verification(snapshot)["ok"])
        media = self.media(snapshot)
        corrupted = b"!" * media.stat().st_size
        media.write_bytes(corrupted)
        self.assertFalse(maintenance._cached_record_snapshot_verification(snapshot)["ok"])
        with self.assertRaises(ValueError):
            self.snapshot(2)
        self.assertEqual(list(self.backups.glob("meeting_records_*.zip")), [snapshot])
        self.assertFalse(list(self.backups.glob(".*.tmp")))
        self.assertEqual(media.read_bytes(), corrupted)
        self.assertEqual(self.audio.read_bytes(), self.original[0])

    def test_offsite_corrupt_object_blocks_publication_and_pruning(self):
        one = self.snapshot()
        old = maintenance.replicate_record_snapshot(one, self.offsite)
        self.media(old).write_bytes(b"corrupt")
        two = self.snapshot(2)
        with self.assertRaises(ValueError):
            maintenance.replicate_record_snapshot(two, self.offsite, keep=1)
        self.assertTrue(old.exists())
        self.assertFalse((self.offsite / two.name).exists())

    def test_portable_export_is_standalone_and_refuses_overwrite(self):
        snapshot = self.snapshot()
        exported = export_snapshot(snapshot, self.root / "portable/full.zip")
        self.assertEqual(self.manifest(exported)["format_version"], 2)
        self.media(snapshot).unlink()
        self.assertTrue(maintenance.verify_record_snapshot(exported)["recoverability_complete"])
        with self.assertRaises(FileExistsError):
            export_snapshot(snapshot, exported)
        maintenance.restore_record_snapshot(exported, self.root / "portable-restore")

    def test_rotation_does_not_remove_shared_objects_or_original_records(self):
        for day in (1, 2, 3):
            snapshot = self.snapshot(day, keep=2)
        self.assertEqual(len(list(self.backups.glob("meeting_records_*.zip"))), 2)
        self.assertTrue(self.media(snapshot).is_file())
        after = (self.audio.read_bytes(), self.minutes.read_bytes(), maintenance.record_state_fingerprint(database.DB_PATH, immutable=False))
        self.assertEqual(after, self.original)

    def test_path_and_manifest_tampering_rejected(self):
        snapshot = self.snapshot()
        manifest = self.manifest(snapshot)
        mapping = manifest["shared_media"]
        name = next(iter(mapping))
        for invalid in ("../outside", "source_media/../../outside", "C:/outside", "source_media\\outside"):
            changed = {**manifest, "shared_media": {invalid: mapping[name]}}
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                shared_objects(snapshot, changed)
        mapping[name]["sha256"] = "../outside"
        with self.assertRaises(ValueError):
            shared_objects(snapshot, manifest)

    def test_failed_copy_never_publishes_partial_object(self):
        digest = hashlib.sha256(self.original[0]).hexdigest()
        target = object_path(self.backups / "snapshot.zip", digest)
        with self.assertRaises(ValueError):
            copy_object(self.audio, target, "0" * 64, len(self.original[0]))
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob("*.tmp")))

    def test_repeated_timestamp_never_overwrites_snapshot(self):
        one = self.snapshot()
        before = one.read_bytes()
        two = self.snapshot()
        self.assertNotEqual(one, two)
        self.assertEqual(one.read_bytes(), before)

    def test_invalid_new_zip_never_becomes_visible_to_rotation(self):
        with mock.patch.object(maintenance, "verify_record_snapshot", return_value={"ok": False, "detail": "injected failure"}):
            with self.assertRaises(ValueError):
                self.snapshot()
        self.assertFalse(list(self.backups.glob("meeting_records_*.zip")))
        self.assertFalse(list(self.backups.glob(".*.tmp")))
        self.assertEqual(self.audio.read_bytes(), self.original[0])


class BackupStorageTests(unittest.TestCase):
    def test_metrics_integrates_backup_scope_without_merging_source_bytes(self):
        from backend import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meeting_records_fixture.zip").write_bytes(b"backup")
            with mock.patch.object(main, "BACKUP_DIR", root), \
                 mock.patch.object(main, "_directory_file_stats", return_value=(1, 100, [], 0, 0, 0, 0)), \
                 mock.patch.object(main, "_source_media_archive_storage_stats", return_value=(0, 0)):
                storage = main._storage_metrics().model_dump()
            self.assertEqual(storage["source_media_bytes"], 100)
            self.assertEqual(storage["backup_storage"]["total_bytes"], 6)

    def test_unreadable_root_is_not_reported_as_empty(self):
        with mock.patch.object(Path, "stat", side_effect=PermissionError("denied")):
            result = backup_storage_metrics(Path("unreadable"))
        self.assertFalse(result["complete"])
        self.assertIsNone(result["total_bytes"])

    def test_categories_sum_without_double_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = {"meeting_records_1.zip": 11, "media_objects/ab/object": 13,
                        "release_1/a.zip": 17, "hotfix_1/b.db": 19,
                        "meetings_1.db": 23, "degraded/c.zip": 29}
            for name, size in fixtures.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * size)
            result = backup_storage_metrics(root)
            self.assertTrue(result["complete"])
            self.assertEqual(result["total_bytes"], sum(fixtures.values()))
            self.assertEqual(result["categories"], {"snapshots": 11, "shared_media": 13, "release": 36, "database": 23, "other": 29})

    def test_scan_error_is_not_zero_or_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(*args, **kwargs):
                kwargs["onerror"](PermissionError("denied"))
                return iter(())
            with mock.patch("backend.backup_storage.os.walk", side_effect=fail):
                result = backup_storage_metrics(Path(tmp))
            self.assertFalse(result["complete"])
            self.assertIsNone(result["total_bytes"])


if __name__ == "__main__":
    unittest.main()
