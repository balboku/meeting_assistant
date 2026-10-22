from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backend import database, maintenance
from backend.snapshot_media import object_path
from scripts.deduplicate_record_snapshot import convert_snapshot, assert_equivalent


class SnapshotDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        context = patch.object(database, "DB_PATH", self.root / "live.db")
        context.start()
        self.addCleanup(context.stop)
        database.init_db()
        self.audio = self.root / "audio.wav"
        self.audio.write_bytes(bytes(range(256)) * 4096)
        self.minutes = self.root / "minutes.md"
        self.minutes.write_text("# Original meeting", encoding="utf-8")
        database.save_meeting("Fixture", "2026/10/22", str(self.audio), str(self.minutes))
        db = maintenance.backup_database(database.DB_PATH, self.root / "old")
        self.source = maintenance.create_record_snapshot(db, self.root / "old", shared_media=False)
        self.original = self.source.read_bytes()
        self.destination = self.root / "new/pinned_fixture.zip"

    def rewrite(self, change=None, extra=None):
        candidate = self.root / "old/modified.zip"
        with zipfile.ZipFile(self.source) as old, zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED) as new:
            for info in old.infolist():
                content = old.read(info.filename)
                if info.filename == "manifest.json" and change:
                    manifest = json.loads(content)
                    change(manifest)
                    content = json.dumps(manifest).encode()
                new.writestr(info, content)
            if extra:
                new.writestr(*extra)
        return candidate

    def test_conversion_preserves_history_and_restores_without_originals(self):
        result = convert_snapshot(self.source, self.destination)
        self.assertTrue(result["equivalent"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.destination.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        self.assertTrue(assert_equivalent(self.source, self.destination)["equivalent"])
        self.audio.unlink()
        self.minutes.unlink()
        target = self.root / "restored"
        report = maintenance.restore_record_snapshot(self.destination, target)
        self.assertTrue(report["restored"])
        self.assertEqual(next((target / "runtime/output/source_audio").iterdir()).read_bytes(), bytes(range(256)) * 4096)

    def test_two_historical_snapshots_share_one_independent_object(self):
        convert_snapshot(self.source, self.destination)
        self.minutes.write_text("# Newer meeting content", encoding="utf-8")
        db = maintenance.backup_database(database.DB_PATH, self.root / "old", now=datetime(2026, 1, 1))
        newer = maintenance.create_record_snapshot(db, self.root / "old", shared_media=False, now=datetime(2026, 1, 1))
        two = self.destination.with_name("two.zip")
        convert_snapshot(newer, two)
        objects = list((two.parent / "media_objects").glob("*/*"))
        self.assertEqual(len(objects), 1)
        self.audio.write_bytes(b"changed live recording")
        self.assertTrue(maintenance.verify_record_snapshot(self.destination)["recoverability_complete"])

    def test_existing_destination_refused(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            convert_snapshot(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"do not overwrite")

    def test_corrupt_pool_not_overwritten_and_zip_not_published(self):
        target = object_path(self.destination, hashlib.sha256(self.audio.read_bytes()).hexdigest())
        target.parent.mkdir(parents=True)
        target.write_bytes(b"corrupt object")
        with self.assertRaises(ValueError):
            convert_snapshot(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(target.read_bytes(), b"corrupt object")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_degraded_snapshot_refused(self):
        source = self.rewrite(lambda m: m.update(missing=[{"category": "source_media"}]))
        with self.assertRaises(ValueError):
            convert_snapshot(source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_legacy_v1_preserved_and_shared_v3_refused(self):
        source = self.rewrite(lambda m: m.update(format_version=1))
        self.assertTrue(convert_snapshot(source, self.destination)["equivalent"])
        with self.assertRaises(ValueError):
            convert_snapshot(self.destination, self.destination.with_name("again.zip"))

    def test_unmanifested_content_refused_not_silently_dropped(self):
        source = self.rewrite(extra=("extra.txt", b"untracked historical content"))
        with self.assertRaises(ValueError):
            convert_snapshot(source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_duplicate_zip_member_refused(self):
        source = self.rewrite(extra=("manifest.json", b"{}"))
        with self.assertRaises(ValueError):
            convert_snapshot(source, self.destination)

    def test_manifest_length_mismatch_refused(self):
        source = self.rewrite(lambda m: m["entries"][0].update(bytes=1))
        with self.assertRaises(ValueError):
            convert_snapshot(source, self.destination)

    def test_unsafe_archive_member_refused(self):
        source = self.rewrite(extra=("../outside.txt", b"unsafe"))
        with self.assertRaises(ValueError):
            convert_snapshot(source, self.destination)

    def test_validation_failure_does_not_publish_or_remove_source(self):
        actual = maintenance.verify_record_snapshot(self.source)
        with patch("scripts.deduplicate_record_snapshot.verify_record_snapshot", side_effect=[actual, {"ok": False}]):
            with self.assertRaises(ValueError):
                convert_snapshot(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.destination.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
