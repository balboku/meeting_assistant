"""Isolated video evidence tests; no live DB, real meeting media, or model calls."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from backend import database, tasks, visual_evidence as visual
from backend.job_queue import JobQueueWorker, enqueue_audio_job
from backend.minutes_view import build_minutes_view, topic_minutes_markdown
from backend.processing_policy import outbound_context
from backend.transcript_sections import _extract_transcript_section_body
from backend.visual_notes import read_visual_notes, topics_for_visual_analysis
from backend import visual_sampling as sampling
from tests.test_minutes_view import CONTENT, record


TRANSCRIPT = "[00:01] **[發言者 A]**：團隊提到驗證目標為 85%。\n"
FRAMES = {"F01": (12, b"fixture-jpeg-1"), "F02": (22, b"fixture-jpeg-2")}
SUPPLEMENT = {"topic_id": "D1", "frame_id": "F01", "kind": "supplement",
              "observed": "表格欄位標為 PVT 試組", "source": "開發時程", "transcript_quote": ""}
CONFLICT = {"topic_id": "D1", "frame_id": "F02", "kind": "conflict",
            "observed": "表列驗證目標 90%", "source": "驗證條件",
            "transcript_quote": "驗證目標為 85%"}


class VisualEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.ocr = self.enterContext(mock.patch("backend.visual_grounding.read_frame_text", return_value={
            "F01": "開發時程\n表格欄位標為 PVT 試組", "F02": "驗證條件\n表列驗證目標 90%",
        }))
        self.check = mock.Mock()
        self.client = mock.Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps({"items": [SUPPLEMENT, CONFLICT]}, ensure_ascii=False))
        self.content = CONTENT + TRANSCRIPT

    def enhance(self, path="meeting.webm"):
        return visual.supplement_new_video(self.client, Path(path), self.content, TRANSCRIPT,
                                          "mock-vision", self.check)

    def test_model_is_bounded_and_evidence_roundtrips_under_topic_only(self):
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_MODEL": "",
                                          "MEETING_VISUAL_EVIDENCE": "1"}), \
             mock.patch.object(visual, "_video_stream", return_value=True), \
             mock.patch.object(visual, "_keyframes", return_value=FRAMES):
            enriched, report = self.enhance()
        self.assertEqual(report, {"status": "complete", "frame_count": 2, "item_count": 2})
        self.client.models.generate_content.assert_called_once()
        options = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(options["model"], "mock-vision")
        self.assertEqual(options["config"].http_options.retry_options.attempts, 1)
        self.assertEqual(options["config"].http_options.timeout, 45000)
        inline = [p.inline_data for p in options["contents"][0].parts if p.inline_data]
        self.assertEqual([p.mime_type for p in inline], ["image/jpeg", "image/jpeg"])
        self.assertEqual([p.data for p in inline], [x[1] for x in FRAMES.values()])
        self.assertEqual(_extract_transcript_section_body(enriched), _extract_transcript_section_body(self.content))
        restored = "\n".join(line for line in enriched.split("\n") if not line.startswith("- 畫面補充"))
        self.assertEqual(restored, self.content)
        r = {**record(), "full_content": enriched}
        old_view = build_minutes_view(record())
        view = build_minutes_view(r)
        self.assertEqual(view["decisions"], old_view["decisions"])
        self.assertEqual(view["actions"], old_view["actions"])
        notes = view["discussions"][0]["visual_notes"]
        self.assertEqual([n["seconds"] for n in notes], [12, 22])
        self.assertEqual([n["pending"] for n in notes], [False, True])
        self.assertIn("逐字稿：驗證目標為 85%", topic_minutes_markdown(r))
        self.assertIn("錄影：[00:22]", topic_minutes_markdown(r))
        del r["structured_summary"]
        self.assertEqual(build_minutes_view(r)["discussions"][0]["visual_notes"], notes)
        self.assertEqual(read_visual_notes(self.content), {})

    def test_invalid_topic_frame_fabricated_conflict_and_duplicate_are_not_saved(self):
        cases = [{**SUPPLEMENT, "topic_id": "D99"}, {**SUPPLEMENT, "frame_id": "F99"},
                 {**CONFLICT, "transcript_quote": "逐字稿根本沒有這句"},
                 {**SUPPLEMENT, "observed": "驗證目標為 85%"}, SUPPLEMENT, SUPPLEMENT]
        result = visual._validated_notes(json.dumps({"items": cases}),
                                         topics_for_visual_analysis(self.content), FRAMES, TRANSCRIPT)
        self.assertEqual(len(result["D1"]), 1)
        self.assertEqual(result["D1"][0]["timecode"], "00:12")

    def test_unreadable_or_misbound_evidence_is_never_published(self):
        for ocr in ({}, {"F01": "驗證條件\n表列驗證目標 90%", "F02": "開發時程\n表格欄位標為 PVT 試組"}):
            self.ocr.return_value = ocr
            with self.subTest(ocr=ocr), mock.patch.dict(os.environ, {
                    "MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_EVIDENCE": "1"}), \
                 mock.patch.object(visual, "_video_stream", return_value=True), \
                 mock.patch.object(visual, "_keyframes", return_value=FRAMES):
                content, report = self.enhance()
                self.assertEqual(content, self.content)
                self.assertEqual(report["status"], "no_evidence")

    def test_skips_local_disabled_and_pure_audio_without_outbound_call(self):
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_EVIDENCE": "1"}), \
             mock.patch.object(visual, "_video_stream", return_value=False) as probe, \
             mock.patch.object(visual, "_keyframes") as extract:
            with outbound_context(True):
                self.assertEqual(self.enhance(), (self.content, None))
            self.assertEqual(self.enhance("meeting.wav"), (self.content, None))
            probe.assert_not_called()
            self.assertEqual(self.enhance(), (self.content, None))
            probe.assert_called_once()
            with mock.patch.dict(os.environ, {"MEETING_VISUAL_EVIDENCE": "off"}):
                self.assertEqual(self.enhance(), (self.content, None))
            extract.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_intermittent_visual_failures_return_the_original_content(self):
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_EVIDENCE": "1"}), \
             mock.patch.object(visual, "_video_stream", return_value=True), \
             mock.patch.object(visual, "_keyframes", return_value=FRAMES) as extract:
            self.client.models.generate_content.side_effect = RuntimeError("429 quota")
            self.assertEqual(self.enhance(), (self.content, {"status": "unavailable"}))
            self.client.models.generate_content.assert_called_once()
            self.client.models.generate_content.side_effect = None
            self.client.models.generate_content.return_value.text = "invalid JSON"
            self.assertEqual(self.enhance(), (self.content, {"status": "unavailable"}))
            extract.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
            self.assertEqual(self.enhance(), (self.content, {"status": "unavailable"}))

    def test_cancelled_stage_never_publishes_a_transcript_only_result(self):
        def extract(_path, check):
            check()
            self.fail("should have propagated cancellation")
        self.check.side_effect = [None, tasks.JobCancelled()]
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1", "MEETING_VISUAL_EVIDENCE": "1"}), \
             mock.patch.object(visual, "_video_stream", return_value=True), \
             mock.patch.object(visual, "_keyframes", side_effect=extract):
            with self.assertRaises(tasks.JobCancelled):
                self.enhance()
        self.client.models.generate_content.assert_not_called()

    def test_budget_uses_duration_and_distinct_scenes_and_prioritizes_short_table(self):
        scenes = []
        for i in range(36):
            thumb = random.Random(i).randbytes(sampling.WIDTH * sampling.HEIGHT)
            scenes.append(sampling.Scene(i * 8, 8.5, 0.55, thumb))
        self.assertEqual(sampling._rank(scenes[:2], 300), [0, 8])
        self.assertLess(len(sampling._rank(scenes, 300)), 12)
        long_scenes = [sampling.Scene(s.second * 10, 80, s.layout, s.thumbnail) for s in scenes]
        self.assertGreater(len(sampling._rank(long_scenes, 3_000)), 12)
        table = sampling.Scene(1519, 1, 0.7, random.Random(100).randbytes(sampling.WIDTH * sampling.HEIGHT))
        ranked = sampling._rank(long_scenes + [table], 3_000)
        self.assertEqual(ranked[0], table.second)
        self.assertTrue(any(second >= 2_400 for second in ranked))
        self.assertEqual(len(sampling._rank(long_scenes, sampling.MAX_SCAN_SECONDS)), sampling.MAX_REQUEST_IMAGES)

    def test_identical_pages_do_not_consume_more_slots_and_flash_is_not_promoted(self):
        width, height = sampling.WIDTH, sampling.HEIGHT
        page = bytes(35 if y % 6 == 0 or x % 12 == 0 else 220 for y in range(height) for x in range(width))
        brief = bytes(55 if y % 4 == 0 or x % 9 == 0 else 190 for y in range(height) for x in range(width))
        flash = random.Random(100).randbytes(width * height)
        frames = [page] * 8 + [flash] + [page] * 5 + [brief] * 2 + [page] * 8
        timestamps = "\n".join(f"pts_time:{i / 2}" for i in range(len(frames))).encode()
        self.assertEqual(sampling.select_scene_times(b"".join(frames), timestamps), [7, 1])

    def test_keyframes_respect_total_image_bytes_without_a_fixed_twelve(self):
        large = b"\xff\xd8" + b"x" * 380_000 + b"\xff\xd9"
        small = b"\xff\xd8" + b"x" * 90_000 + b"\xff\xd9"
        for jpeg, expected in ((small, sampling.MAX_REQUEST_IMAGES), (large, 12)):
            with self.subTest(jpeg_bytes=len(jpeg)), \
                 mock.patch.object(visual, "_run", return_value=SimpleNamespace(stdout=jpeg, stderr=b"")), \
                 mock.patch.object(visual, "select_scene_times", return_value=list(range(sampling.MAX_REQUEST_IMAGES))):
                result = visual._keyframes(Path("fixture.webm"), self.check)
                self.assertEqual(len(result), expected)
                self.assertLessEqual(sum(len(image) for _, image in result.values()), visual.MAX_REQUEST_JPEG_BYTES)

    def test_request_rejects_excess_payload_and_transcript_context_stays_bounded(self):
        extra_frames = {f"F{i:02d}": (i * 200, b"x") for i in range(sampling.MAX_REQUEST_IMAGES + 1)}
        extra_bytes = {key: (t, b"x" * 400_000) for key, (t, _) in list(extra_frames.items())[:13]}
        with mock.patch.dict(os.environ, {"MEETING_ALLOW_CLOUD": "1"}):
            for frames in (extra_frames, extra_bytes):
                with self.assertRaisesRegex(ValueError, "request budget"):
                    visual._request_notes(self.client, "fixture", {}, TRANSCRIPT, frames)
        self.client.models.generate_content.assert_not_called()
        frames = dict(list(extra_frames.items())[:sampling.MAX_REQUEST_IMAGES])
        transcript = "\n".join(f"[{s // 60:02d}:{s % 60:02d}] " + ("文字" * 125) for s in range(0, 5_000, 5))
        context = visual._transcript_context(transcript, frames)
        self.assertLessEqual(len(context), visual.MAX_CONTEXT_CHARS)
        self.assertIn("\nF23\n", context)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_ffmpeg_on_synthetic_scene_changes_with_live_webm_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenes.webm"
            subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=5:d=4,drawgrid=w=40:h=30:t=4:c=black",
                            "-f", "lavfi", "-i", "smptebars=s=320x180:r=5:d=4",
                            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                            "-c:v", "libvpx-vp9", "-live", "1", str(path)], check=True, timeout=20)
            self.assertTrue(visual._video_stream(path))
            frames = visual._keyframes(path, self.check)
            self.assertEqual([seconds for seconds, _ in frames.values()], [1, 5])
            self.assertTrue(all(jpeg.startswith(b"\xff\xd8") for _, jpeg in frames.values()))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_ffmpeg_keeps_one_second_table_between_static_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief-table.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i",
                            "color=c=white:s=320x180:r=10:d=8,drawgrid=w=32:h=24:t=3:c=black:"
                            "enable='gte(t,3.4)*lt(t,4.4)'",
                            "-c:v", "mpeg4", str(path)], check=True, timeout=20)
            frames = visual._keyframes(path, self.check)
            self.assertEqual([seconds for seconds, _ in frames.values()], [4])
            jpeg = frames["F01"][1]
            preview = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                                      "-f", "mjpeg", "-i", "pipe:0", "-frames:v", "1",
                                      "-vf", f"scale={sampling.WIDTH}:{sampling.HEIGHT},format=gray",
                                      "-threads:v", "1", "-f", "rawvideo", "pipe:1"],
                                     input=jpeg, capture_output=True, check=True, timeout=10).stdout
            self.assertGreaterEqual(sampling._layout(preview), 0.4)

    def test_new_video_worker_saves_on_visual_failure_but_old_payload_never_enters_stage(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source = root / "meeting.webm"
            source.write_bytes(b"synthetic-visual-fixture")
            stack.enter_context(mock.patch.object(database, "DB_PATH", root / "db.sqlite"))
            stack.enter_context(mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fixture", "MEETING_ALLOW_CLOUD": "1",
                                                              "MEETING_VISUAL_EVIDENCE": "1"}))
            database.init_db()
            enqueue_audio_job("new-video", source, root / "output", capture_visual_evidence=True)
            enqueue_audio_job("old-video", source, root / "output")
            enqueue_audio_job("rerun-video", source, root / "output", source="meeting_rerun",
                              capture_visual_evidence=True)
            self.assertFalse(database.get_job("rerun-video")["payload"]["capture_visual_evidence"])
            for name, value in {
                "_prepare_audio_for_transcription": (source, {}),
                "_split_audio_to_segments": [tasks.AudioSlice(source, 0, 30)],
                "_load_segment_transcript_cache": None,
                "_transcribe_segment_with_recovery": TRANSCRIPT,
                "_segment_transcript_current_quality_issues": [],
                "_generate_meeting_content_from_transcript": (CONTENT, "fake-summary", None),
            }.items():
                stack.enter_context(mock.patch.object(tasks, name, return_value=value))
            for name in ("_raise_if_full_transcript_unsafe", "_raise_if_delivery_blocked_by_segment_quality"):
                stack.enter_context(mock.patch.object(tasks, name))
            stack.enter_context(mock.patch.object(tasks, "_repair_meeting_content_if_needed",
                                                side_effect=lambda **kw: kw["meeting_content"]))
            stack.enter_context(mock.patch.object(tasks, "_finalize_meeting_content",
                                                side_effect=lambda content, *_: content))
            stack.enter_context(mock.patch.object(tasks, "_append_transcript_chinese_translation",
                                                side_effect=lambda client, content, *_: (content, {})))
            stack.enter_context(mock.patch.object(tasks.genai, "Client", return_value=self.client))
            extract = stack.enter_context(mock.patch.object(visual, "_keyframes", return_value=FRAMES))
            probe = stack.enter_context(mock.patch.object(visual, "_video_stream", return_value=True))
            self.client.models.generate_content.side_effect = RuntimeError("429 fixture")
            worker = JobQueueWorker()
            for _ in range(3):
                worker.process_job(database.claim_next_pending_job())
            for job_id in ("new-video", "old-video", "rerun-video"):
                with self.subTest(job=job_id):
                    self.assertEqual(database.get_job(job_id)["status"], "done")
                    meeting = database.get_meeting(database.get_meeting_by_job_id(job_id)["id"])
                    expected = {"status": "unavailable"} if job_id == "new-video" else None
                    self.assertEqual(meeting["quality_report"].get("visual_evidence"), expected)
                    self.assertNotIn("畫面補充", Path(meeting["output_path"]).read_text(encoding="utf-8"))
            probe.assert_called_once()
            self.client.models.generate_content.assert_called_once()
            self.assertEqual(source.read_bytes(), b"synthetic-visual-fixture")

            from backend.visual_frames import read_frame
            jpeg = b"\xff\xd8worker fixture\xff\xd9"
            extract.return_value = {"F01": (12, jpeg)}
            self.client.models.generate_content.side_effect = None
            self.client.models.generate_content.return_value = SimpleNamespace(
                text=json.dumps({"items": [SUPPLEMENT]}))
            enqueue_audio_job("new-with-screenshot", source, root / "output", capture_visual_evidence=True)
            worker.process_job(database.claim_next_pending_job())
            self.assertEqual(database.get_job("new-with-screenshot")["status"], "done")
            meeting = database.get_meeting(database.get_meeting_by_job_id("new-with-screenshot")["id"])
            self.assertEqual(meeting["quality_report"]["visual_evidence"]["screenshot_count"], 1)
            image = read_visual_notes(meeting["full_content"])["D1"][0]["frame_sha256"]
            self.assertEqual(read_frame(Path(meeting["output_path"]), image), jpeg)
