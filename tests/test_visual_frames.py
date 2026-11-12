"""Screenshot persistence, API scope and portable restore using disposable records."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database, maintenance, visual_evidence, visual_frames, visual_frames_api
from backend.minutes_view import build_minutes_view
from backend.security_policy import permission_for_request
from backend.visual_notes import insert_visual_notes, referenced_frames, strip_frame_markers
from tests.test_minutes_view import CONTENT, record
from tests.test_visual_evidence import CONFLICT, SUPPLEMENT, TRANSCRIPT


JPEG = b"\xff\xd8fixture jpeg\xff\xd9"
SECOND = b"\xff\xd8unused candidate\xff\xd9"
DIGEST = hashlib.sha256(JPEG).hexdigest()
OTHER = hashlib.sha256(SECOND).hexdigest()


class VisualFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.markdown = self.root / "meeting.md"
        self.bundle = visual_frames.bundle_path(self.markdown)
        self.note = {"observed": "87.6%", "source": "判定表", "timecode": "00:12",
                     "pending": False, "transcript_quote": None, "frame_id": "F01"}
        self.frames = {"F01": (12, JPEG), "F02": (23, SECOND), "F03": (36, JPEG)}

    def save_fixture(self):
        notes = {"D1": [self.note]}
        self.assertEqual(visual_frames.attach_referenced_frames(self.markdown, notes, self.frames), 1)
        self.markdown.write_text(insert_visual_notes(CONTENT, notes), encoding="utf-8")

    def test_only_distinct_approved_frames_are_published_and_text_is_retained(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps({"items": [
            SUPPLEMENT, {**CONFLICT, "frame_id": "F03"}, {**SUPPLEMENT, "frame_id": "F02", "topic_id": "D99"},
        ]}))
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_EVIDENCE": "1"}), \
             mock.patch.object(visual_evidence, "_video_stream", return_value=True), \
             mock.patch.object(visual_evidence, "_keyframes", return_value=self.frames):
            content, result = visual_evidence.supplement_new_video(
                client, Path("fixture.mp4"), CONTENT, TRANSCRIPT, "mock", lambda: None,
                output_path=self.markdown)
        self.assertEqual(result, {"status": "complete", "frame_count": 3, "item_count": 2, "screenshot_count": 1})
        client.models.generate_content.assert_called_once()
        with ZipFile(self.bundle) as archive:
            self.assertEqual(archive.namelist(), [DIGEST + ".jpg"])
        view = build_minutes_view({**record(), "full_content": content})
        self.assertTrue(view["available"])
        notes = view["discussions"][0]["visual_notes"]
        self.assertEqual([n["frame_sha256"] for n in notes], [DIGEST, DIGEST])
        self.assertEqual([n["seconds"] for n in notes], [12, 36])
        self.assertTrue(notes[1]["pending"])
        self.assertIn("逐字稿：驗證目標為 85%", strip_frame_markers(content))
        self.assertNotIn(DIGEST, strip_frame_markers(content))
        self.assertEqual(referenced_frames(content), {DIGEST})

    def test_write_failure_does_not_erase_the_text_or_leave_an_image(self):
        notes = {"D1": [self.note]}
        with mock.patch.object(visual_frames, "NamedTemporaryFile", side_effect=OSError("fixture")):
            self.assertEqual(visual_frames.attach_referenced_frames(self.markdown, notes, self.frames), 0)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(referenced_frames(insert_visual_notes(CONTENT, notes)))
        self.assertIn("錄影：[00:12]", insert_visual_notes(CONTENT, notes))

    def test_route_requires_citation_in_that_meeting_and_an_intact_jpeg(self):
        self.save_fixture()
        app = FastAPI()
        app.include_router(visual_frames_api.router)
        self.assertEqual(permission_for_request("GET", f"/meetings/1/visual-frames/{DIGEST}"), "meeting:read")
        current = {"output_path": str(self.markdown), "full_content": self.markdown.read_text(encoding="utf-8")}
        with mock.patch.object(visual_frames_api, "get_meeting", side_effect=lambda n: current if n == 1 else None):
            with TestClient(app) as client:
                good = client.get(f"/meetings/1/visual-frames/{DIGEST}")
                self.assertEqual(good.content, JPEG)
                self.assertEqual(good.headers["content-type"], "image/jpeg")
                self.assertEqual(good.headers["cache-control"], "private, no-store")
                for route in (f"/meetings/2/visual-frames/{DIGEST}", f"/meetings/1/visual-frames/{OTHER}",
                              "/meetings/1/visual-frames/not-a-digest"):
                    self.assertEqual(client.get(route).status_code, 404)
                self.bundle.write_bytes(b"corrupt fixture")
                self.assertEqual(client.get(f"/meetings/1/visual-frames/{DIGEST}").status_code, 404)

    def test_backup_restores_exact_cited_image_and_meeting_deletion_cleans_only_its_bundle(self):
        self.save_fixture()
        source = self.root / "video.mp4"
        source.write_bytes(b"disposable video fixture")
        db = self.root / "fixture.db"
        with mock.patch.object(database, "DB_PATH", db):
            database.init_db()
            meeting_id = database.save_meeting("Fixture", "2026/11/12", source.name, str(self.markdown))
            backup_dir = self.root / "backups"
            snapshot = maintenance.create_record_snapshot(maintenance.backup_database(db, backup_dir),
                                                          backup_dir, source_media_dir=self.root)
            self.assertTrue(maintenance.verify_record_snapshot(snapshot)["recoverability_complete"])
            keep = self.root / "another-meeting.md.visual.zip"
            keep.write_bytes(b"another meeting")
            self.assertTrue(database.delete_meeting(meeting_id))
            self.assertFalse(self.bundle.exists())
            self.assertTrue(source.is_file())
            self.assertEqual(keep.read_bytes(), b"another meeting")
            restore = self.root / "restored"
            self.assertTrue(maintenance.restore_record_snapshot(snapshot, restore)["restored"])
            md = restore / "runtime" / "output" / f"{meeting_id}_meeting.md"
            self.assertEqual(referenced_frames(md.read_text(encoding="utf-8")), {DIGEST})
            self.assertEqual(visual_frames.read_frame(md, DIGEST), JPEG)
