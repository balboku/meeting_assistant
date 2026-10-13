from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import database, tasks
from backend.job_checkpoints import ACTIVE_CHECKPOINTS, JobCheckpoints, checkpointed, save_verified_segment, load_verified_segment
from backend.model_availability import (
    AUDIO_MODELS, ModelsQuotaWaiting, QuotaStore, generate_with_pool, quota_wait_until,
)
from backend.model_routing import cache_candidates, candidates
from backend.transcribe_adapter import generate_transcription, render_transcription
from backend.transcript_translation import _validated_translation_batch
from google.genai import types
from contextlib import ExitStack
import os
import time

DAILY = RuntimeError("429 quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")


class ModelContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 10, 12, 1, tzinfo=timezone.utc).timestamp()
        self.store = QuotaStore(self.root / "quota.db", "project", lambda: self.now)

    def test_switches_after_multiple_429_and_skips_them_after_restart(self):
        models = list(AUDIO_MODELS[1:])
        generate = mock.Mock(side_effect=[DAILY, DAILY, "transcript"])
        result, used = generate_with_pool(models, generate, store=self.store)
        self.assertEqual((result, used), ("transcript", models[2]))
        restarted = QuotaStore(self.store.db_path, "project", lambda: self.now + 70)
        generate = mock.Mock(return_value="next segment")
        self.assertEqual(generate_with_pool(models, generate, store=restarted)[1], models[2])
        generate.assert_called_once_with(models[2])

    def test_all_quota_exhausted_defer_until_first_available_model(self):
        models = [AUDIO_MODELS[1], AUDIO_MODELS[2]]
        generate = mock.Mock(side_effect=[DAILY, RuntimeError("429 retryDelay: 90s")])
        with self.assertRaises(ModelsQuotaWaiting) as waiting:
            generate_with_pool(models, generate, store=self.store)
        self.assertEqual(quota_wait_until(str(waiting.exception), self.now), self.now + 92)
        generate = mock.Mock()
        with self.assertRaises(ModelsQuotaWaiting):
            generate_with_pool(models, generate, store=self.store)
        generate.assert_not_called()
        self.now += 93
        generate.return_value = "remaining text"
        self.assertEqual(generate_with_pool(models, generate, store=self.store)[0], "remaining text")
        generate.assert_called_once_with(models[1])

    def test_all_cooling_no_provider_call_or_budget_charge(self):
        self.store.reserve(AUDIO_MODELS[1])
        generate, reserve = mock.Mock(), mock.Mock()
        with self.assertRaises(ModelsQuotaWaiting):
            generate_with_pool([AUDIO_MODELS[1]], generate, store=self.store, before_call=reserve)
        generate.assert_not_called()
        reserve.assert_not_called()

    def test_atomic_reservations_and_project_isolation(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.reserve(AUDIO_MODELS[1]), range(4)))
        self.assertEqual(results.count(None), 1)
        self.assertIsNone(QuotaStore(self.store.db_path, "other", lambda: self.now).reserve(AUDIO_MODELS[1]))

    def test_local_daily_cap_and_reset(self):
        for _ in range(25):
            self.assertIsNone(self.store.reserve(AUDIO_MODELS[0], 500))
            self.now += 25
        self.assertGreater(self.store.reserve(AUDIO_MODELS[0]), self.now)
        self.now = datetime(2026, 10, 13, 7, 6, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(self.store.reserve(AUDIO_MODELS[0]))

    def test_oversized_transcribe_uses_flash_not_a_permanent_quota_wait(self):
        generate = mock.Mock(return_value="valid")
        self.assertEqual(generate_with_pool([AUDIO_MODELS[0], AUDIO_MODELS[1]], generate,
                         store=self.store, input_tokens=12000)[1], AUDIO_MODELS[1])
        generate.assert_called_once_with(AUDIO_MODELS[1])

    def test_bad_credentials_are_not_disguised_as_quota_wait(self):
        generate = mock.Mock(side_effect=RuntimeError("401 invalid credential"))
        with self.assertRaisesRegex(RuntimeError, "401"):
            generate_with_pool(AUDIO_MODELS, generate, store=self.store, is_transient=lambda _: False)
        self.assertEqual(generate.call_count, 1)

    def test_unavailable_intermediate_model_does_not_break_quota_continuation(self):
        models = list(AUDIO_MODELS[1:4])
        generate = mock.Mock(side_effect=[DAILY, RuntimeError("404 model not found"), "valid"])
        self.assertEqual(generate_with_pool(models, generate, store=self.store,
                         is_transient=lambda _: False)[0], "valid")
        self.now += 70
        generate = mock.Mock(return_value="next")
        self.assertEqual(generate_with_pool(models, generate, store=self.store)[0], "next")
        generate.assert_called_once_with(models[2])

    def test_all_unsupported_models_do_not_become_fake_quota_wait(self):
        generate = mock.Mock(side_effect=RuntimeError("404 model not found"))
        with self.assertRaisesRegex(RuntimeError, "404"):
            generate_with_pool(AUDIO_MODELS[1:3], generate, store=self.store)
        generate = mock.Mock()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            generate_with_pool(AUDIO_MODELS[1:3], generate, store=self.store)
        generate.assert_not_called()

    def test_quota_wait_is_pending_preserves_progress_and_can_cancel(self):
        with mock.patch.object(database, "DB_PATH", self.root / "jobs.db"):
            database.init_db()
            database.create_job("waiting")
            database.claim_next_pending_job()
            database.update_job_status("waiting", "processing", "segment done", progress_current=2, progress_total=4)
            detail = str(ModelsQuotaWaiting(self.now + 60, list(AUDIO_MODELS)))
            # Use a future real time for the DB queue admission.
            with mock.patch("backend.model_availability.time.time", return_value=self.now):
                self.assertEqual(database.retry_or_fail_job("waiting", detail), "pending")
            job = database.get_job("waiting")
            self.assertEqual((job["progress_current"], job["progress_total"], job["attempts"]), (2, 4, 0))
            with database.get_db() as conn:
                conn.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id='waiting'")
            self.assertEqual(database.retry_or_fail_job("waiting", detail), "cancelled")
            self.assertIsNone(database.get_job("waiting")["error_detail"])

    def test_checkpoint_survives_restart_and_input_change_invalidates_it(self):
        calls = []
        @checkpointed
        def validated_stage(client, *, source):
            calls.append(source)
            return "minutes " + source, "actual-model", {"topics": [source]}
        for _ in range(2):
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "job", "sha"))
            try:
                self.assertEqual(validated_stage(None, source="transcript")[1], "actual-model")
            finally:
                ACTIVE_CHECKPOINTS.reset(token)
        self.assertEqual(calls, ["transcript"])
        token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "job", "new-sha"))
        try:
            validated_stage(None, source="transcript")
        finally:
            ACTIVE_CHECKPOINTS.reset(token)
        self.assertEqual(len(calls), 2)

    def test_checkpoint_never_saves_failed_stage(self):
        @checkpointed
        def bad_stage(client):
            raise ModelsQuotaWaiting(self.now + 60, ["model"])
        token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "job", "sha"))
        try:
            with self.assertRaises(ModelsQuotaWaiting):
                bad_stage(None)
        finally:
            ACTIVE_CHECKPOINTS.reset(token)
        self.assertFalse(list(self.root.glob("job_checkpoints/**/*.json")))

    def test_forced_quota_resume_uses_only_this_jobs_verified_segment(self):
        audio = self.root / "source.wav"
        audio.write_bytes(b"source")
        context = tasks._segment_cache_context(audio, AUDIO_MODELS[3], 1, 10)
        transcript = "[00:00] **[發言者 A]**：這是可用的會議逐字稿內容。"
        with mock.patch.object(tasks, "_segment_cache_quality_issues", return_value=[]):
            tasks._save_segment_transcript_cache(self.root, "previous", 0, context, transcript)
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "new-job", "sha", str(DAILY), forced=True))
            try:
                self.assertIsNone(tasks._load_segment_transcript_cache(self.root, "new-job", 0, context))
                tasks._save_segment_transcript_cache(self.root, "new-job", 0, context, transcript)
                self.assertEqual(tasks._load_segment_transcript_cache(self.root, "new-job", 0, context), transcript)
            finally:
                ACTIVE_CHECKPOINTS.reset(token)

    def test_pool_capabilities_and_actual_model_cache_isolation(self):
        self.assertNotIn("gemini-3.5-transcribe", candidates("gemma-4-31b-it", AUDIO_MODELS[4], audio=False))
        self.assertNotIn("gemma-4-31b-it", candidates(AUDIO_MODELS[0], AUDIO_MODELS[2], audio=True))
        source = {"model": AUDIO_MODELS[0], "source_audio_sha256": "sha", "segment_bounds": [[0, 120]]}
        contexts = cache_candidates(AUDIO_MODELS[0], AUDIO_MODELS[2], source)
        self.assertEqual({c[1]["model"] for c in contexts}, set(AUDIO_MODELS[1:]))
        self.assertTrue(all(c[1]["source_audio_sha256"] == "sha" for c in contexts))
        self.assertEqual(source["model"], AUDIO_MODELS[0])

    def test_hostile_or_malformed_wait_timestamp_does_not_stall_job(self):
        self.assertIsNone(quota_wait_until('MODEL_QUOTA_WAIT:{"retry_at":"NaN"}', self.now))
        self.assertIsNone(quota_wait_until('MODEL_QUOTA_WAIT:bad', self.now))
        self.assertEqual(quota_wait_until('MODEL_QUOTA_WAIT:{"retry_at":1e30}', self.now), self.now + 86400)

    def test_validated_translation_batch_is_reused_after_restart(self):
        batch = [{"id": "T1", "source": "Please review 12 items.", "timecode": "00:00", "speaker": "發言者 A"}]
        for _ in range(2):
            generate = mock.Mock(return_value=(SimpleNamespace(text=json.dumps(
                {"translations": [{"id": "T1", "text": "請檢查 12 個項目。"}]})), "actual-model"))
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "job", "sha"))
            try:
                translated, model = _validated_translation_batch(None, batch=batch, primary_model="primary",
                    fallback_model="fallback", job_id="job", generate_text=generate)
                self.assertEqual((translated["T1"], model), ("請檢查 12 個項目。", "actual-model"))
            finally:
                ACTIVE_CHECKPOINTS.reset(token)
            self.assertEqual(generate.call_count, 1 if _ == 0 else 0)

    def test_stale_worker_cannot_defer_successor_job(self):
        from backend.job_queue import JobQueueWorker
        worker = JobQueueWorker()
        job = {"job_id": "job", "task_type": "audio_processing", "worker_id": "old", "worker_generation": 1}
        with mock.patch.object(worker, "_process_audio_job", side_effect=ModelsQuotaWaiting(self.now + 60, ["model"])), \
             mock.patch("backend.job_queue.is_job_cancel_requested", return_value=False), \
             mock.patch("backend.job_queue.job_lease_is_current", return_value=False), \
             mock.patch("backend.job_queue.retry_or_fail_job") as retry:
            worker.process_job(job)
        retry.assert_not_called()

    def test_worker_finishes_after_quota_wait_without_retranscribing_verified_segment(self):
        """Real queue/task/cache/SQLite transitions, with a bounded fake provider."""
        from backend.job_queue import JobQueueWorker, enqueue_audio_job
        audio = self.root / "meeting.wav"
        audio.write_bytes(b"audio-fixture")
        output = self.root / "output"
        self.now = time.time()
        self.store = QuotaStore(self.root / "jobs.db", "project", lambda: self.now)
        client = mock.Mock()
        calls = []
        def provider(model, **kwargs):
            calls.append(model)
            if model != AUDIO_MODELS[-1]:
                raise DAILY
            return SimpleNamespace(text="[00:00] **[發言者 A]**：我們討論測試結果與接續處理。\n"
                "[00:50] **[發言者 A]**：本段結束，確認下一步。")
        client.models.generate_content.side_effect = provider
        def transcribe(client, path, index, total, job_id, model, **kwargs):
            response, used = tasks._generate_transcript_with_transient_fallback(client, model=model,
                contents=["fixture"], config=types.GenerateContentConfig(), job_id=job_id,
                segment_index=index, total_segments=total, audio_duration_seconds=60)
            kwargs["response_models"].extend([AUDIO_MODELS[1], used])  # verified hybrid parent
            if used != model:
                kwargs["transient_fallback_models"].append(used)
            return tasks._offset_transcript_timestamps(response.text, kwargs["offset_seconds"])
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(database, "DB_PATH", self.root / "jobs.db"))
            database.init_db()
            enqueue_audio_job("resume", audio, output, model=AUDIO_MODELS[1], force_full_meeting_rerun=True)
            stack.enter_context(mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}))
            stack.enter_context(mock.patch.object(tasks.genai, "Client", return_value=client))
            stack.enter_context(mock.patch.object(tasks, "_model_quota_store", return_value=self.store))
            stack.enter_context(mock.patch.object(tasks, "generate_transcription", side_effect=DAILY))
            stack.enter_context(mock.patch.object(tasks, "_prepare_audio_for_transcription", return_value=(audio, {})))
            split = stack.enter_context(mock.patch.object(tasks, "_split_audio_to_segments", return_value=[
                tasks.AudioSlice(audio, 0, 60), tasks.AudioSlice(audio, 60, 120)]))
            stack.enter_context(mock.patch.object(tasks, "_transcribe_segment_with_recovery", side_effect=transcribe))
            for name in ("_segment_cache_quality_issues", "_segment_transcript_current_quality_issues",
                         "_speech_backed_timestamp_gap_quality_ranges", "_speech_backed_transcript_local_density_quality_ranges"):
                stack.enter_context(mock.patch.object(tasks, name, return_value=[]))
            stack.enter_context(mock.patch.object(tasks, "_raise_if_full_transcript_unsafe"))
            content = "## 📋 一、討論摘要 (Discussion Summary)\n測試通過。\n## 📝 四、完整逐字稿 (Verbatim Transcript)\n"
            summary = stack.enter_context(mock.patch.object(tasks, "_generate_meeting_content_from_transcript", return_value=(content, "summary-model", {})))
            stack.enter_context(mock.patch.object(tasks, "_repair_meeting_content_if_needed", side_effect=lambda **kw: kw["meeting_content"]))
            stack.enter_context(mock.patch.object(tasks, "_finalize_meeting_content", side_effect=lambda content, transcript, job: content + transcript))
            stack.enter_context(mock.patch.object(tasks, "_append_transcript_chinese_translation", side_effect=lambda client, content, *args: (content, {})))
            worker = JobQueueWorker()
            worker.process_job(database.claim_next_pending_job())
            waiting = database.get_job("resume")
            self.assertEqual((waiting["status"], waiting["attempts"], waiting["progress_current"]), ("pending", 0, 1))
            self.assertTrue(audio.exists())
            self.assertIsNone(database.get_meeting_by_job_id("resume"))
            self.now += 70
            with database.get_db() as conn:
                conn.execute("UPDATE jobs SET queued_at='2000-01-01 00:00:00' WHERE job_id='resume'")
            worker.process_job(database.claim_next_pending_job())
            self.assertEqual(database.get_job("resume")["status"], "done")
            self.assertIsNotNone(database.get_meeting_by_job_id("resume"))
            summary.assert_called_once()
            self.assertEqual(calls.count(AUDIO_MODELS[-1]), 2)  # one call per slice, not 3
            with database.get_db() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events WHERE job_id='resume' AND event_type='status_failed'").fetchone()[0], 0)
            # A single-slice hybrid journal must never become a fake primary-
            # model shared cache when summary generation resumes after quota.
            split.return_value = [tasks.AudioSlice(audio, 0, 60)]
            enqueue_audio_job("single", audio, output, model=AUDIO_MODELS[1], force_full_meeting_rerun=True)
            from backend.company_vocabulary import vocabulary_context
            stack.enter_context(vocabulary_context(database.get_job("single")["payload"]["vocabulary_snapshot"]))
            context = tasks._segment_cache_context(audio, AUDIO_MODELS[1], 1, tasks.SEGMENT_MINUTES,
                                                   segment_bounds=[[0, 60]])
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(output, "single", tasks._sha256_file(audio)))
            try:
                save_verified_segment(0, context, "[00:00] **[發言者 A]**：這是已驗證的混合轉錄結果。",
                                      [AUDIO_MODELS[1], AUDIO_MODELS[-1]])
            finally:
                ACTIVE_CHECKPOINTS.reset(token)
            with database.get_db() as conn:
                conn.execute("UPDATE jobs SET error_detail=? WHERE job_id='single'",
                             (str(ModelsQuotaWaiting(self.now + 60, list(AUDIO_MODELS))),))
            client.models.generate_content.reset_mock()
            with mock.patch.object(tasks, "_save_segment_transcript_cache") as provider_cache:
                worker.process_job(database.claim_next_pending_job())
            self.assertEqual(database.get_job("single")["status"], "done")
            client.models.generate_content.assert_not_called()
            provider_cache.assert_not_called()

    def test_verified_hybrid_segment_is_job_local_and_only_used_for_quota_resume(self):
        context = {"source_audio_sha256": "sha", "model": "requested", "segment_bounds": [[0, 60]]}
        token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, "same-job", "sha"))
        try:
            save_verified_segment(0, context, "verified hybrid", ["primary", "fallback"])
            self.assertIsNone(load_verified_segment(0, context))  # fresh/manual run
        finally:
            ACTIVE_CHECKPOINTS.reset(token)
        for job, changed_context, expected in (
            ("same-job", context, "verified hybrid"),
            ("other-job", context, None),
            ("same-job", dict(context, model="new-model"), None),
        ):
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(self.root, job, "sha", str(DAILY)))
            try:
                self.assertEqual(load_verified_segment(0, changed_context), expected)
            finally:
                ACTIVE_CHECKPOINTS.reset(token)

    def test_corrupt_verified_segment_is_not_used(self):
        scope = JobCheckpoints(self.root, "job", "sha", str(DAILY))
        token = ACTIVE_CHECKPOINTS.set(scope)
        try:
            context = {"source_audio_sha256": "sha", "model": "requested"}
            save_verified_segment(0, context, "valid", ["primary", "fallback"])
            path = scope.path("verified_segment", {"index": 0, "context": context})
            data = json.loads(path.read_text(encoding="utf-8"))
            data["value"]["transcript"] = "corrupt"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(load_verified_segment(0, context))
        finally:
            ACTIVE_CHECKPOINTS.reset(token)

    def test_native_default_routes_to_adapter_then_free_fallback(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="[00:00] **[發言者 A]**：完成。")
        with mock.patch.object(tasks, "_model_quota_store", return_value=self.store), \
             mock.patch.object(tasks, "generate_transcription", side_effect=DAILY) as native, \
             mock.patch.object(tasks, "_raise_if_cancelled"), \
             mock.patch.object(tasks, "update_job_status"), \
             mock.patch.object(tasks, "_resolve_transcription_recovery_model", return_value=AUDIO_MODELS[1]):
            response, model = tasks._generate_transcript_with_transient_fallback(client, model=AUDIO_MODELS[0],
                contents=[SimpleNamespace(uri="file-uri"), "prompt"], config=types.GenerateContentConfig(),
                job_id="job", segment_index=0, total_segments=1, audio_duration_seconds=30)
        native.assert_called_once()
        self.assertEqual(model, AUDIO_MODELS[1])
        self.assertEqual(client.models.generate_content.call_args.kwargs["model"], AUDIO_MODELS[1])

    def test_quota_wait_does_not_run_incomplete_translation_as_success(self):
        from backend.transcript_translation import append_chinese_translation
        transcript = "[00:00] **[發言者 A]**：Please review the report."
        with self.assertRaises(ModelsQuotaWaiting):
            append_chinese_translation(None, "minutes", transcript, "job", "primary", "fallback", enabled=True,
                generate_text=mock.Mock(side_effect=ModelsQuotaWaiting(self.now + 60, ["model"])),
                check_active=lambda: None, update_status=mock.Mock(), fatal_errors=(ModelsQuotaWaiting,))

    def test_partial_checkpoint_corruption_is_not_reused(self):
        generate = mock.Mock(return_value=("validated", "model"))
        @checkpointed
        def stage(client):
            return generate()
        scope = JobCheckpoints(self.root, "job", "sha")
        token = ACTIVE_CHECKPOINTS.set(scope)
        try:
            stage(None)
            path = next(scope.root.glob("*.json"))
            data = json.loads(path.read_text(encoding="utf-8"))
            data["value"][0] = "corrupted"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(stage(None)[0], "validated")
        finally:
            ACTIVE_CHECKPOINTS.reset(token)
        self.assertEqual(generate.call_count, 2)


class TranscribeAdapterTests(unittest.TestCase):
    def payload(self, words):
        return {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "text", "annotations": [dict(type="word_info", text=text, speaker=speaker,
                start_offset=f"{index}s", end_offset=f"{index + .5}s")
                for index, (text, speaker) in enumerate(words)]}]}]}

    def test_real_response_speaker_format_and_english_original(self):
        text = render_transcription(self.payload([("Hello", "spk:0"), ("world.", "spk:0"), ("Yes.", "spk:1")]))
        self.assertIn("[發言者 A]**：Hello world.", text)
        self.assertIn("[00:02] **[發言者 B]**：Yes.", text)

    def test_multisegment_labels_do_not_assert_global_identity(self):
        text = render_transcription(self.payload([("明白", "spk_1")]), segment_index=2, total_segments=4)
        self.assertIn("發言者 A（片段 3）", text)

    def test_recovery_requests_within_one_parent_do_not_merge_local_speaker_ids(self):
        first = render_transcription(self.payload([("第一段", "spk:0")]), audio_scope="aaaaaaaa")
        second = render_transcription(self.payload([("第二段", "spk:0")]), audio_scope="bbbbbbbb")
        self.assertIn("發言者 A（片段 1-aaaaaaaa）", first)
        self.assertIn("發言者 A（片段 1-bbbbbbbb）", second)

    def test_missing_annotations_or_invalid_timestamps_fail_closed(self):
        with self.assertRaises(ValueError):
            render_transcription(self.payload([]))
        payload = self.payload([("test", "spk:0")])
        payload["steps"][0]["content"][0]["annotations"][0]["start_offset"] = "-1s"
        with self.assertRaises(ValueError):
            render_transcription(payload)

    def test_official_rest_request_does_not_mix_forbidden_vocabulary(self):
        payload = self.payload([("Hello", "spk:0")])
        response = SimpleNamespace(status_code=200, json=lambda: payload)
        transport = mock.Mock()
        transport.__enter__ = mock.Mock(return_value=transport)
        transport.__exit__ = mock.Mock(return_value=False)
        transport.post.return_value = response
        client = SimpleNamespace(_api_client=SimpleNamespace(api_key="test-credential"))
        with mock.patch("backend.transcribe_adapter.httpx.Client", return_value=transport):
            result = generate_transcription(client, SimpleNamespace(uri="file-uri", mime_type="audio/wav"))
        body = transport.post.call_args.kwargs["json"]
        config = body["generation_config"]["transcription_config"]
        self.assertEqual(config["mode"]["type"], "verbatim")
        self.assertNotIn("custom_vocabulary", config)
        self.assertFalse(body["store"])
        self.assertIn("Hello", result.text)

    def test_utf8_offsets_keep_original_spacing_and_traditional_chinese(self):
        payload = self.payload([("我们", "spk:0"), ("review", "spk:0"), ("12", "spk:0")])
        content = payload["steps"][0]["content"][0]
        content["text"] = "我们 review 12"
        for word, start, end in zip(content["annotations"], (0, 7, 14), (6, 13, 16)):
            word.update(start_index=start, end_index=end)
        result = render_transcription(payload)
        self.assertIn("我們 review 12", result)

    def test_word_annotations_must_cover_source_words(self):
        payload = self.payload([("world", "spk:0")])
        content = payload["steps"][0]["content"][0]
        content["text"] = "Hello world"
        content["annotations"][0].update(start_index=6, end_index=11)
        with self.assertRaisesRegex(ValueError, "skipped"):
            render_transcription(payload)


if __name__ == "__main__":
    unittest.main()
