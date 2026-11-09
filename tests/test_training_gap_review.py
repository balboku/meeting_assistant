import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from backend import training_api
from backend.auth import AuthActor
from backend.training_gap_review import DIRECTORY, SAMPLE_RATE, project_review_windows
from tests.http_helpers import asgi_request


class TrainingGapReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.course = {"id": 5, "parts": [
            {"id": f"part-{i}", "job_id": f"job-{i}", "source_sha256": str(i) * 64,
             "state": "done" if i == 3 else "failed"} for i in (1, 2, 3)]}
        self.bundle = {"schema": 1, "course_id": 5, "sample_rate": SAMPLE_RATE, "parts": {}}
        for part in self.course["parts"]:
            self.bundle["parts"][part["id"]] = {
                "job_id": part["job_id"], "source_sha256": part["source_sha256"],
                "review_frames": [[SAMPLE_RATE * n + 5, SAMPLE_RATE * (n + 1)] for n in range(8)],
                "verified_frames": [[SAMPLE_RATE * 8, SAMPLE_RATE * 9]],
                "accepted_artifacts": [{"path": "private-cache.json", "sha256": "private"}],
            }
        self.manifest = self.root / DIRECTORY / "5" / "review.json"

    def write(self):
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps(self.bundle), encoding="utf-8")

    def test_two_lists_with_exact_frame_times_and_no_completed_part(self):
        self.write()
        original = self.manifest.read_bytes()
        project_review_windows(self.root, self.course)
        self.assertEqual([len(p.get("review_windows", [])) for p in self.course["parts"]], [8, 8, 0])
        self.assertEqual(self.course["parts"][0]["review_windows"][0],
                         {"start_seconds": 5 / SAMPLE_RATE, "end_seconds": 1})
        self.assertNotIn("private-cache", json.dumps(self.course))
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_source_or_job_mismatch_and_covered_window_are_not_projected(self):
        self.bundle["parts"]["part-1"]["source_sha256"] = "0" * 64
        self.bundle["parts"]["part-2"]["verified_frames"] = [[0, SAMPLE_RATE]]
        self.write()
        project_review_windows(self.root, self.course)
        self.assertTrue(all("review_windows" not in p for p in self.course["parts"]))
        self.bundle["parts"]["part-1"].update(source_sha256="1" * 64, job_id="changed")
        self.write()
        project_review_windows(self.root, self.course)
        self.assertNotIn("review_windows", self.course["parts"][0])

    def test_malformed_or_missing_snapshot_leaves_status_untouched(self):
        before = copy.deepcopy(self.course)
        project_review_windows(self.root, self.course)
        self.assertEqual(self.course, before)
        self.write()
        self.manifest.write_text('{"parts":[', encoding="utf-8")
        project_review_windows(self.root, self.course)
        self.assertEqual(self.course, before)

    def test_detail_keeps_existing_read_permission(self):
        self.write()
        app = FastAPI()
        app.include_router(training_api.router)
        with patch.object(training_api, "OUTPUT_DIR", self.root), \
                patch.object(training_api.store, "get_course", side_effect=lambda _: copy.deepcopy(self.course)) as get_course:
            with patch.object(training_api, "actor_from_request", return_value=AuthActor("none", "unknown", enabled=True)):
                self.assertEqual(asgi_request(app, "GET", "/training/courses/5").status_code, 403)
                get_course.assert_not_called()
            with patch.object(training_api, "actor_from_request", return_value=AuthActor("viewer", "viewer", enabled=True)):
                response = asgi_request(app, "GET", "/training/courses/5")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["parts"][0]["review_windows"]), 8)
                self.assertNotIn("accepted_artifacts", response.text)
