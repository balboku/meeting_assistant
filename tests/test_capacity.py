from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.capacity import capacity_health_checks


class CapacityHealthTests(unittest.TestCase):
    def test_capacity_checks_report_threshold_breaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = root / "meetings.db"
            source = root / "source"
            backups = root / "backups"
            source.mkdir()
            backups.mkdir()
            database.write_bytes(b"db")
            (source / "audio.webm").write_bytes(b"source")
            (backups / "snapshot.zip").write_bytes(b"backup")

            checks = capacity_health_checks(
                db_path=database,
                source_media_dir=source,
                backup_dir=backups,
                env={
                    "MEETING_DATABASE_MAX_BYTES": "1",
                    "MEETING_SOURCE_MEDIA_MAX_BYTES": "1",
                    "MEETING_BACKUP_MAX_BYTES": "1",
                    "MEETING_MIN_FREE_DISK_BYTES": "1",
                },
            )

        by_name = {check["name"]: check for check in checks}
        self.assertEqual(by_name["database_capacity"]["status"], "failed")
        self.assertEqual(by_name["source_media_capacity"]["status"], "failed")
        self.assertEqual(by_name["backup_capacity"]["status"], "failed")
        self.assertEqual(by_name["local_disk_free"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
