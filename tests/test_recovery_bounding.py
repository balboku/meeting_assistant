from __future__ import annotations

import tempfile
import unittest
import inspect
from pathlib import Path
from unittest import mock

from pydub import AudioSegment

from backend import tasks
from backend.recovery_progress import RecoveryNoProgressError


class RecoveryBoundingTests(unittest.TestCase):
    def test_three_unclear_recovery_windows_stop_before_more_calls(self) -> None:
        children = [(Path(f"child-{index}.wav"), index * 20, (index + 1) * 20)
                    for index in range(4)]
        unclear = "[00:00] **[發言者不明]**：[聽不清]"
        with mock.patch.object(tasks, "_audio_has_insufficient_transcription_evidence", return_value=False), \
             mock.patch.object(tasks, "_prepare_recovery_speech_focus_audio", return_value=(Path("segment.wav"), "original")), \
             mock.patch.object(tasks, "_split_audio_to_subsegments", return_value=children), \
             mock.patch.object(tasks, "_transcribe_segment", return_value=unclear) as transcribe, \
             mock.patch.object(tasks, "_recovery_subsegment_cache_context", return_value={"model": "model"}), \
             mock.patch.object(tasks, "_resolve_transcription_recovery_model", return_value=None), \
             mock.patch.object(tasks, "_load_segment_transcript_cache", return_value=None), \
             mock.patch.object(tasks, "_save_segment_transcript_cache") as save_cache, \
             mock.patch.object(tasks, "_raise_if_segment_transcript_incomplete"), \
             mock.patch.object(tasks, "update_job_status"):
            with self.assertRaises(RecoveryNoProgressError):
                tasks._transcribe_segment_with_recovery(
                    object(), Path("segment.wav"), 0, 1, "job", "model",
                    offset_seconds=0, duration_seconds=80, is_last_segment=True,
                    direct_recovery=True,
                    recovery_cache_output_dir=Path("output"),
                )
        self.assertEqual(transcribe.call_count, 3)
        save_cache.assert_not_called()

    def test_real_recovery_text_resets_no_progress_counter(self) -> None:
        children = [(Path(f"child-{index}.wav"), index * 20, (index + 1) * 20)
                    for index in range(4)]
        transcripts = iter([
            "[00:00] **[發言者不明]**：[聽不清]",
            "[00:00] **[發言者 A]**：確認這段內容。",
            "[00:00] **[發言者不明]**：[聽不清]",
            "[00:00] **[發言者不明]**：[聽不清]",
        ])
        with mock.patch.object(tasks, "_audio_has_insufficient_transcription_evidence", return_value=False), \
             mock.patch.object(tasks, "_prepare_recovery_speech_focus_audio", return_value=(Path("segment.wav"), "original")), \
             mock.patch.object(tasks, "_split_audio_to_subsegments", return_value=children), \
             mock.patch.object(tasks, "_transcribe_segment", side_effect=lambda *_a, **_k: next(transcripts)) as transcribe, \
             mock.patch.object(tasks, "_raise_if_segment_transcript_incomplete"), \
             mock.patch.object(tasks, "update_job_status"):
            result = tasks._transcribe_segment_with_recovery(
                object(), Path("segment.wav"), 0, 1, "job", "model",
                offset_seconds=0, duration_seconds=80, is_last_segment=True,
                direct_recovery=True,
            )
        self.assertEqual(transcribe.call_count, 4)
        self.assertIn("確認這段內容", result)

    def test_unclear_recovery_cache_is_not_verified_when_audio_has_activity(self) -> None:
        with mock.patch.object(tasks, "_audio_has_insufficient_transcription_evidence", return_value=False), \
             mock.patch.object(tasks, "_audio_backed_transcript_quality_issues") as quality:
            issues = tasks._recovery_subsegment_cached_audio_quality_issues(
                Path("child.wav"), "[00:00] **[發言者不明]**：[聽不清]",
                start_seconds=0, end_seconds=20,
            )
        self.assertTrue(issues)
        quality.assert_not_called()

    def test_every_recursive_recovery_call_advances_depth(self) -> None:
        source = inspect.getsource(tasks._transcribe_segment_with_recovery)
        recursive_calls = source.count("return _transcribe_segment_with_recovery(")
        depth_advances = source.count("recovery_depth=recovery_depth + 1")

        self.assertEqual(recursive_calls, 3)
        self.assertEqual(depth_advances, 4)

    def test_uncertainty_dominated_transcript_never_calls_summary_model(self) -> None:
        transcript = "\n".join([
            *(f"[{minute:02d}:00] **[發言者不明]**：[聽不清]" for minute in range(12)),
            "[12:00] **[發言者 A]**：好，那這部分我們就先這樣處理。",
            "[13:00] **[發言者 A]**：好，那這部分我們就先這樣處理。",
        ])
        with mock.patch.object(tasks, "_generate_text_with_fallback") as generate:
            content, model, payload = tasks._generate_meeting_content_from_transcript(
                client=object(), full_transcript=transcript, job_id="job",
                summary_primary_model="summary", summary_secondary_model="fallback",
                return_structured=True,
            )

        generate.assert_not_called()
        self.assertEqual(model, "local-insufficient-audio")
        self.assertEqual(payload["final_decisions"], [])
        self.assertEqual(payload["action_items"], [])
        self.assertIn("無法可靠整理會議主題", content)

    def test_recursive_subsegments_shrink_and_use_bounded_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current = root / ("very_long_source_name_" * 8 + ".wav")
            export_handle = AudioSegment.silent(duration=12_000).export(
                current,
                format="wav",
            )
            export_handle.close()
            previous_duration = 12

            for _ in range(10):
                segments = tasks._split_audio_to_subsegments(
                    current,
                    chunk_seconds=5,
                )
                self.assertTrue(all(len(path.name) < 80 for path, _, _ in segments))
                if len(segments) <= 1:
                    break
                current, start, end = max(
                    segments,
                    key=lambda item: item[2] - item[1],
                )
                duration = end - start
                self.assertLess(duration, previous_duration)
                previous_duration = duration
            else:
                self.fail("recovery splitting did not converge")

    def test_recovery_depth_guard_marks_local_uncertainty_without_another_model_call(self) -> None:
        quality_events = []
        transcript = tasks._transcribe_segment_with_recovery(
            object(),
            Path("segment.wav"),
            0,
            1,
            "job",
            "model",
            offset_seconds=120,
            duration_seconds=60,
            is_last_segment=True,
            quality_events=quality_events,
            recovery_depth=tasks.TRANSCRIPT_RECOVERY_MAX_DEPTH,
        )

        self.assertEqual(transcript, "[02:00] **[發言者不明]**：[聽不清]")
        self.assertTrue(any("安全上限" in item["issue"] for item in quality_events))

    def test_direct_recovery_excess_density_degrades_only_local_segment(self) -> None:
        issue = tasks.TRANSCRIPT_SPEECH_EXCESS_DENSITY_ISSUE_MARKER
        subsegments = [(Path("a.wav"), 0, 30), (Path("b.wav"), 30, 60)]
        with mock.patch.object(
            tasks, "_audio_has_insufficient_transcription_evidence", return_value=False
        ), mock.patch.object(
            tasks, "_prepare_recovery_speech_focus_audio", return_value=(Path("segment.wav"), "original")
        ), mock.patch.object(
            tasks, "_split_audio_to_subsegments", return_value=subsegments
        ), mock.patch.object(
            tasks, "_transcribe_segment", return_value="[00:00] **[發言者 A]**：測試內容"
        ), mock.patch.object(
            tasks, "_raise_if_segment_transcript_incomplete",
            side_effect=[None, None, RuntimeError(issue)],
        ), mock.patch.object(tasks, "update_job_status"):
            transcript = tasks._transcribe_segment_with_recovery(
                object(), Path("segment.wav"), 0, 2, "job", "model",
                offset_seconds=600, duration_seconds=60,
                is_last_segment=False, direct_recovery=True,
                direct_recovery_pass=tasks.TRANSCRIPT_DIRECT_RECOVERY_MAX_PASSES,
                preferred_recovery_chunk_seconds=30,
            )

        self.assertEqual(transcript, "[10:00] **[發言者不明]**：[聽不清]")

    def test_unsplittable_automatic_excess_density_degrades_only_local_segment(self) -> None:
        issue = tasks.TRANSCRIPT_SPEECH_EXCESS_DENSITY_ISSUE_MARKER
        with mock.patch.object(
            tasks, "_audio_has_insufficient_transcription_evidence", return_value=False
        ), mock.patch.object(
            tasks, "_transcribe_segment", return_value="[00:00] **[發言者 A]**：猜測內容"
        ) as transcribe, mock.patch.object(
            tasks, "_raise_if_segment_transcript_incomplete", side_effect=RuntimeError(issue)
        ), mock.patch.object(
            tasks, "_next_recovery_chunk_seconds", return_value=None
        ):
            transcript = tasks._transcribe_segment_with_recovery(
                object(), Path("segment.wav"), 8, 12, "job", "model",
                offset_seconds=4800, duration_seconds=1, is_last_segment=False,
            )

        self.assertEqual(transcript, "[80:00] **[發言者不明]**：[聽不清]")
        transcribe.assert_called_once()

    def test_unsplittable_automatic_low_density_degrades_only_local_segment(self) -> None:
        issue = tasks.TRANSCRIPT_SPEECH_DENSITY_ISSUE_MARKER
        with mock.patch.object(
            tasks, "_audio_has_insufficient_transcription_evidence", return_value=False
        ), mock.patch.object(
            tasks, "_transcribe_segment", return_value="[00:00] **[發言者 A]**：零散內容"
        ), mock.patch.object(
            tasks, "_raise_if_segment_transcript_incomplete", side_effect=RuntimeError(issue)
        ), mock.patch.object(
            tasks, "_next_recovery_chunk_seconds", return_value=None
        ):
            transcript = tasks._transcribe_segment_with_recovery(
                object(), Path("segment.wav"), 8, 12, "job", "model",
                offset_seconds=4800, duration_seconds=11, is_last_segment=False,
            )

        self.assertEqual(transcript, "[80:00] **[發言者不明]**：[聽不清]")

    def test_explicit_uncertainty_is_reviewable_without_blocking_delivery(self) -> None:
        transcript = "[80:00] **[發言者不明]**：[聽不清]"
        with mock.patch.object(tasks, "_audio_backed_transcript_quality_issues") as backed:
            issues = tasks._segment_transcript_current_quality_issues(
                transcript, 8, 12, expected_start_seconds=4800,
                expected_end_seconds=5400, is_last_segment=False,
                audio_path=Path("segment.wav"),
            )

        self.assertEqual(issues, [])
        backed.assert_not_called()

    def test_density_recovery_stops_after_three_local_levels(self) -> None:
        issue = tasks.TRANSCRIPT_SPEECH_DENSITY_ISSUE_MARKER
        quality_events = []
        with mock.patch.object(
            tasks, "_audio_has_insufficient_transcription_evidence", return_value=False
        ), mock.patch.object(
            tasks, "_transcribe_segment", return_value="[00:00] **[發言者 A]**：零散內容"
        ), mock.patch.object(
            tasks, "_raise_if_segment_transcript_incomplete", side_effect=RuntimeError(issue)
        ), mock.patch.object(tasks, "_split_audio_to_subsegments") as split:
            transcript = tasks._transcribe_segment_with_recovery(
                object(), Path("segment.wav"), 8, 12, "job", "model",
                offset_seconds=5220, duration_seconds=30, is_last_segment=False,
                recovery_depth=3, quality_events=quality_events,
            )

        self.assertEqual(transcript, "[87:00] **[發言者不明]**：[聽不清]")
        self.assertTrue(any(issue in event["issue"] for event in quality_events))
        split.assert_not_called()


if __name__ == "__main__":
    unittest.main()
