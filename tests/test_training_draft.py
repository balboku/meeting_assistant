import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from backend import training_api
from backend.auth import AuthActor
from backend.job_checkpoints import ACTIVE_CHECKPOINTS, JobCheckpoints, save_verified_segment
from backend.training_context import training_context
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

    def save_verified(self, part, index, bounds, text, *, training=True):
        scope = JobCheckpoints(self.root, part["job_id"], part["source_sha256"])
        context = {"source_audio_sha256": part["source_sha256"], "total_segments": len(bounds),
                   "segment_bounds": bounds, "audio_preprocessing_profile": "test"}
        token = ACTIVE_CHECKPOINTS.set(scope)
        try:
            with training_context(part["id"] if training else False):
                save_verified_segment(index, context, text, ["not-a-real-model"])
        finally:
            ACTIVE_CHECKPOINTS.reset(token)
        return scope.path("verified_segment", {"index": index, "context": context})

    def test_new_course_can_preview_before_any_full_transcript_then_update(self):
        self.manifest.unlink()
        first, second, third = self.course["parts"]
        first["state"] = "processing"
        third.update(transcript="", state="pending")
        bounds = [[0, 30], [25, 60], [55, 90]]
        ordinary = self.save_verified(second, 0, bounds, "[00:02] 不屬於課程的來源", training=False)
        self.assertTrue(ordinary.is_file())
        self.save_verified(first, 0, bounds, "[00:02] 首先選擇來源。\n[00:07] [聽不清]\n[00:50] 範圍外不得引用。")
        result = build_draft(self.root, copy.deepcopy(self.course))
        first_draft, second_draft, third_draft = result["parts"]
        self.assertEqual(first_draft["basis"], "accepted")
        self.assertEqual(first_draft["extracts"], ["[00:02] 首先選擇來源。"])
        self.assertEqual(first_draft["sections"], [{"label": "操作與示例", "lines": first_draft["extracts"]}])
        self.assertEqual(first_draft["unconfirmed_windows"], [{"start_seconds": 30, "end_seconds": 90}])
        self.assertEqual(first_draft["unconfirmed_points"], [7])
        self.assertEqual(second_draft["extracts"], [])
        self.assertTrue(third_draft["unconfirmed_untimed"])
        self.save_verified(first, 2, bounds, "[01:04] 注意資料來源。\n[01:06] 首先選擇來源。")
        first_draft = build_draft(self.root, copy.deepcopy(self.course))["parts"][0]
        self.assertEqual(first_draft["extracts"], ["[00:02] 首先選擇來源。", "[01:04] 注意資料來源。"])
        self.assertEqual(first_draft["unconfirmed_windows"], [{"start_seconds": 30, "end_seconds": 55}])
        self.assertFalse(self.course["notes_current"])

    def test_mismatched_corrupt_or_replaced_checkpoint_is_never_quoted(self):
        self.manifest.unlink()
        first = self.course["parts"][0]
        bounds = [[0, 30], [25, 60]]
        self.save_verified(first, 0, bounds, "[00:02] 舊計畫片段。")
        second = self.save_verified(first, 1, [[0, 25], [20, 60]], "[00:26] 新計畫片段。")
        part = build_draft(self.root, copy.deepcopy(self.course))["parts"][0]
        self.assertEqual(part["extracts"], ["[00:26] 新計畫片段。"])
        self.assertEqual(part["unconfirmed_windows"], [{"start_seconds": 0, "end_seconds": 20}])
        changed = copy.deepcopy(self.course)
        changed["parts"][0]["source_sha256"] = "a" * 64
        self.assertEqual(build_draft(self.root, changed)["parts"][0]["extracts"], [])
        second.write_text('{"value":{"transcript":"[00:26] 不可信內容"}}', encoding="utf-8")
        part = build_draft(self.root, copy.deepcopy(self.course))["parts"][0]
        self.assertEqual(part["extracts"], [])
        self.assertEqual(part["unconfirmed_windows"], [{"start_seconds": 0, "end_seconds": 60}])

    def test_damaged_index_cannot_change_coverage_or_relabel_a_checkpoint(self):
        self.manifest.unlink()
        first = self.course["parts"][0]
        checkpoint = self.save_verified(first, 0, [[0, 30], [25, 60]], "[00:02] 原本的已驗收片段。")
        index = checkpoint.parent / "training-draft-index.json"
        document = json.loads(index.read_bytes())
        document["value"]["bounds"][0][1] = 55
        index.write_text(json.dumps(document), encoding="utf-8")
        part = build_draft(self.root, self.course)["parts"][0]
        self.assertEqual(part["extracts"], [])
        self.assertEqual(part["unconfirmed_windows"], [])
        self.assertTrue(part["unconfirmed_untimed"])

    def test_operator_approved_artifact_is_used_but_unknown_files_are_not(self):
        first = self.course["parts"][0]
        frames = [8 * SAMPLE_RATE, 9 * SAMPLE_RATE]
        name = f"{first['id']}/verified-{frames[0]}-{frames[1]}.json"
        artifact = self.folder / name
        artifact.parent.mkdir()
        artifact.write_text(json.dumps({"source_sha256": first["source_sha256"], "rate": SAMPLE_RATE,
            "range_frames": frames, "transcript": "[00:08] 已驗收的原句。\n[00:09] 不屬於驗收範圍。"}), encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.review["parts"][first["id"]]["accepted_artifacts"] = [
            {"path": name, "sha256": digest}, {"path": "../unauthorized.json", "sha256": digest}]
        self.manifest.write_text(json.dumps(self.review), encoding="utf-8")
        result = build_draft(self.root, copy.deepcopy(self.course))
        self.assertEqual(result["parts"][0]["extracts"], ["[00:08] 已驗收的原句。"])
        self.assertEqual(len(result["parts"][0]["unconfirmed_windows"]), 8)
        self.assertNotIn(digest, json.dumps(result))
        artifact.write_bytes(b"tampered")
        self.assertEqual(build_draft(self.root, copy.deepcopy(self.course))["parts"][0]["extracts"], [])

    def test_complete_transcript_can_still_contain_unconfirmed_markers(self):
        last = self.course["parts"][2]
        last["transcript"] = "[00:01] 避免變更來源。\n[00:07] [inaudible]\n[00:08] [MUSIC]\n[聽不清]"
        part = build_draft(self.root, self.course)["parts"][2]
        self.assertEqual(part["extracts"], ["[00:01] 避免變更來源。"])
        self.assertEqual(part["unconfirmed_points"], [7])
        self.assertTrue(part["unconfirmed_untimed"])
        self.assertTrue(part["complete"])

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
