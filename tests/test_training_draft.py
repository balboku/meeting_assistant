import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from backend import training_api
from backend.auth import AuthActor
from backend.training_draft import build_draft, source_fingerprint
from backend.training_gap_review import DIRECTORY, SAMPLE_RATE, project_review_windows
from tests.http_helpers import asgi_request


class CourseDraftTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / DIRECTORY / "5"
        self.folder.mkdir(parents=True)
        self.course = {"id": 5, "version": 3, "title": "測試", "notes_current": False, "parts": [
            {"id": f"part-{i}", "job_id": f"job-{i}", "position": i, "source_sha256": str(i) * 64,
             "state": "done" if i == 3 else "failed",
             "transcript": "[00:00] 逐字稿開頭\n[00:17] 原文末尾" if i == 3 else ""}
            for i in (1, 2, 3)]}
        self.review = {"schema": 1, "course_id": 5, "sample_rate": SAMPLE_RATE, "parts": {
            p["id"]: {"job_id": p["job_id"], "source_sha256": p["source_sha256"],
                      "review_frames": [[n * SAMPLE_RATE + 7, (n + 1) * SAMPLE_RATE] for n in range(8)],
                      "verified_frames": [[8 * SAMPLE_RATE, 9 * SAMPLE_RATE]],
                      "accepted_artifacts": [{"path": "unreviewed-must-not-leak", "sha256": "private"}]}
            for p in self.course["parts"][:2]}}
        self.manifest = self.folder / "review.json"
        self.manifest.write_text(json.dumps(self.review), encoding="utf-8")

    def save_draft(self, markdown="## 確認過的課程筆記"):
        digest = project_review_windows(self.root, copy.deepcopy(self.course))
        snapshot = {"schema": 1, "source_fingerprint": source_fingerprint(self.course, digest),
                    "markdown": markdown}
        (self.folder / "draft.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def test_reviewed_notes_keep_sixteen_distinct_windows_and_no_raw_cache(self):
        self.save_draft()
        result = build_draft(self.root, copy.deepcopy(self.course))
        self.assertEqual(result["kind"], "reviewed")
        self.assertEqual(result["markdown"], "## 確認過的課程筆記")
        self.assertEqual([len(p["unconfirmed_windows"]) for p in result["parts"]], [8, 8, 0])
        self.assertEqual(result["parts"][0]["unconfirmed_windows"][0]["start_seconds"], 7 / SAMPLE_RATE)
        self.assertNotIn("unreviewed-must-not-leak", json.dumps(result))
        self.assertEqual([p["extracts"] for p in result["parts"]], [[], [], []])

    def test_wrong_revision_source_job_or_transcript_does_not_serve_reviewed_text(self):
        self.save_draft()
        cases = [
            ("version", lambda c: c.update(version=4)),
            ("source", lambda c: c["parts"][0].update(source_sha256="0" * 64)),
            ("job", lambda c: c["parts"][1].update(job_id="different-job")),
            ("transcript", lambda c: c["parts"][2].update(transcript="[00:00] 更新後")),
        ]
        for name, change in cases:
            with self.subTest(name=name):
                course = copy.deepcopy(self.course)
                change(course)
                result = build_draft(self.root, course)
                self.assertEqual(result["kind"], "extracts")
                self.assertEqual(result["markdown"], "")
                self.assertTrue(result["parts"][2]["extracts"])

    def test_changed_review_falls_back_to_verbatim_complete_part(self):
        self.save_draft()
        self.review["parts"]["part-1"]["review_frames"].pop()
        self.manifest.write_text(json.dumps(self.review), encoding="utf-8")
        result = build_draft(self.root, copy.deepcopy(self.course))
        self.assertEqual(result["kind"], "extracts")
        self.assertEqual(result["parts"][0]["extracts"], [])
        self.assertEqual(result["parts"][2]["extracts"], self.course["parts"][2]["transcript"].splitlines())
        self.assertEqual(len(result["parts"][0]["unconfirmed_windows"]), 7)

    def test_official_notes_do_not_look_like_draft(self):
        self.save_draft()
        self.course["notes_current"] = True
        self.assertEqual(build_draft(self.root, self.course), {"kind": "official"})

    def test_viewer_get_requires_read_permission_and_does_not_queue(self):
        self.save_draft()
        app = FastAPI()
        app.include_router(training_api.router)
        with patch.object(training_api, "OUTPUT_DIR", self.root), \
                patch.object(training_api.store, "get_course", return_value=copy.deepcopy(self.course)) as get_course, \
                patch.object(training_api.store, "_queue_notes") as queue:
            with patch.object(training_api, "actor_from_request", return_value=AuthActor("none", "unknown", enabled=True)):
                self.assertEqual(asgi_request(app, "GET", "/training/courses/5/draft").status_code, 403)
                get_course.assert_not_called()
            with patch.object(training_api, "actor_from_request", return_value=AuthActor("viewer", "viewer", enabled=True)):
                response = asgi_request(app, "GET", "/training/courses/5/draft")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.json()["kind"], "reviewed")
                get_course.assert_called_once_with(5, include_transcripts=True, read_only=True)
                self.assertEqual(asgi_request(app, "POST", "/training/courses/5/draft").status_code, 405)
            queue.assert_not_called()
