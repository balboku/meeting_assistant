"""Isolated functional checks for shared terminology, retrieval and privacy."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi import FastAPI
from backend import database, company_vocabulary as vocabulary, meeting_knowledge as knowledge
from backend.auth import AuthActor
from backend.knowledge_api import router
from backend.local_speech import align_speakers
from backend.processing_policy import source_mode, require_cloud, outbound_context, meeting_is_private
from backend.security_policy import permission_for_request
from scripts.evaluate_speech import text_metrics, diarization_error
from tests.http_helpers import asgi_request


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_patch = patch.object(database, "DB_PATH", self.root / "meetings.db")
        self.db_patch.start()
        database.init_db()
        self.app = FastAPI()
        self.app.include_router(router)

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def meeting(self, title="Mercury", quote="Mercury 需要 12 件 S-ring。", private=False):
        path = self.root / f"meeting-{len(list(self.root.glob('*.md')))}.md"
        path.write_text("## 完整逐字稿 (Verbatim Transcript)\n[01:20] **[A]**：" + quote +
                        "\n\n## 英文中譯對照\n不應當作來源的翻譯", encoding="utf-8")
        return database.save_meeting(title, "2026/10/13", "sample.wav", str(path), summary=quote,
            quality_report={"processing_policy": {"mode": "local" if private else "cloud"}})

    def test_proposed_terms_not_used_until_explicitly_approved(self):
        term_id = vocabulary.propose("S-ring", ["S rain"], "parts", "editor")
        self.assertFalse(vocabulary.snapshot()["terms"])
        vocabulary.review(term_id, "approved", 1, "admin")
        self.assertEqual(vocabulary.snapshot()["terms"][0]["canonical"], "S-ring")

    def test_retry_snapshot_survives_disabling_and_does_not_cascade(self):
        term = vocabulary.propose("S-ring", ["S rain"], "", "editor")
        vocabulary.review(term, "approved", 1, "admin")
        frozen = vocabulary.snapshot()
        vocabulary.review(term, "disabled", 2, "admin")
        with vocabulary.vocabulary_context(frozen):
            self.assertEqual(vocabulary.apply_approved_aliases("S rain and XS rain"), "S-ring and XS rain")
        self.assertEqual(vocabulary.apply_approved_aliases("S rain"), "S rain")
        self.assertFalse(vocabulary.snapshot()["terms"])

    def test_conflicting_aliases_and_stale_review_are_rejected(self):
        first = vocabulary.propose("correct", ["wrong"], "", "editor")
        vocabulary.review(first, "approved", 1, "admin")
        second = vocabulary.propose("another", ["WRONG"], "", "editor")
        with self.assertRaises(ValueError): vocabulary.review(second, "approved", 1, "admin")
        with self.assertRaises(ValueError): vocabulary.review(first, "disabled", 1, "admin")

    def test_viewer_can_read_but_not_propose_or_approve(self):
        with patch("backend.knowledge_api.actor_from_request", return_value=AuthActor("viewer", "viewer", enabled=True)):
            self.assertEqual(asgi_request(self.app,"GET","/knowledge/vocabulary").status_code,200)
            self.assertEqual(asgi_request(self.app,"POST","/knowledge/vocabulary",json={"canonical":"part"}).status_code,403)
            self.assertEqual(asgi_request(self.app,"PATCH","/knowledge/vocabulary/1",json={"status":"approved","version":1}).status_code,403)
        self.assertEqual(permission_for_request("POST","/knowledge/ask"),"meeting:read")

    def test_editor_proposes_admin_approves(self):
        with patch("backend.knowledge_api.actor_from_request", return_value=AuthActor("editor", "editor", enabled=True)):
            response=asgi_request(self.app,"POST","/knowledge/vocabulary",json={"canonical":"S-ring","aliases":["S rain"]})
            self.assertEqual(response.status_code,201,response.text)
            term_id=response.json()["id"]
            self.assertEqual(asgi_request(self.app,"PATCH",f"/knowledge/vocabulary/{term_id}",json={"status":"approved","version":1}).status_code,403)
        with patch("backend.knowledge_api.actor_from_request", return_value=AuthActor("admin", "admin", enabled=True)):
            self.assertEqual(asgi_request(self.app,"PATCH",f"/knowledge/vocabulary/{term_id}",json={"status":"approved","version":1}).status_code,200)

    def test_search_returns_current_original_quotes_times_and_review_status(self):
        one=self.meeting(); two=self.meeting("Review", "Mercury 延後至 Friday。")
        result=knowledge.answer("Mercury")
        self.assertEqual({e["meeting_id"] for e in result["evidence"]},{one,two})
        self.assertTrue(all(e["seconds"]==80 and e["review_status"]=="generated" for e in result["evidence"]))
        self.assertFalse(knowledge.answer("不應當作來源的翻譯")["evidence"])

    def test_scope_and_no_evidence_abstain(self):
        one=self.meeting(); self.meeting()
        self.assertEqual({e["meeting_id"] for e in knowledge.answer("Mercury",meeting_ids=[one])["evidence"]},{one})
        self.assertEqual(knowledge.answer("UNSEEN-PROJECT")["status"],"no_evidence")
        self.assertFalse(knowledge.answer("Mercury",approved_only=True)["evidence"])

    def test_stale_approved_hash_is_not_formal_evidence(self):
        meeting_id=self.meeting()
        record=database.get_meeting(meeting_id)
        path=Path(record["output_path"])
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with database.get_db() as conn:
            conn.execute("UPDATE meetings SET review_status='approved',approved_content_sha256=? WHERE id=?",(digest,meeting_id))
        self.assertTrue(knowledge.answer("Mercury",approved_only=True)["evidence"])
        path.write_text(path.read_text(encoding="utf-8")+"\nChanged",encoding="utf-8")
        self.assertFalse(knowledge.answer("Mercury",approved_only=True)["evidence"])

    def test_private_evidence_never_reaches_generator(self):
        self.meeting(private=True); generate=Mock()
        result=knowledge.answer("Mercury",use_cloud=True,generator=generate)
        self.assertEqual(result["status"],"local_only");generate.assert_not_called()

    def test_unverifiable_model_quote_is_discarded(self):
        self.meeting()
        result=knowledge.answer("Mercury",use_cloud=True,generator=lambda *_:([{"evidence_id":"E1","quote":"invented"}],"fake"))
        self.assertEqual(result["status"],"evidence");self.assertNotIn("statements",result)

    def test_exact_quote_is_returned_with_source(self):
        self.meeting()
        result=knowledge.answer("Mercury",use_cloud=True,generator=lambda *_:([{"evidence_id":"E1","quote":"12 件 S-ring"}],"fake"))
        self.assertEqual(result["status"],"answered")

    def test_quota_exhaustion_preserves_local_evidence(self):
        from backend.model_availability import ModelsQuotaWaiting
        self.meeting()
        generate=Mock(side_effect=ModelsQuotaWaiting(9999999999,["fake"]))
        result=knowledge.answer("Mercury",use_cloud=True,generator=generate)
        self.assertEqual(result["status"],"quota_wait");self.assertTrue(result["evidence"])

    def test_source_privacy_survives_same_file_reupload(self):
        path=self.root/"audio.wav";path.write_bytes(b"test audio")
        self.assertEqual(source_mode(path,"local"),"local")
        self.assertEqual(source_mode(path,"cloud"),"local")
        with outbound_context(True):
            with self.assertRaises(PermissionError): require_cloud()
        with patch.dict("os.environ",{"MEETING_ALLOW_CLOUD":"0"}):
            with self.assertRaises(PermissionError): require_cloud()

    def test_shared_vocabulary_frozen_in_queue_payload(self):
        from backend.job_queue import enqueue_audio_job
        term=vocabulary.propose("Mercury",[],"","admin");vocabulary.review(term,"approved",1,"admin")
        path=self.root/"audio.wav";path.write_bytes(b"audio")
        enqueue_audio_job("test-job",path,self.root,"fake",custom_vocabulary=["S-ring"])
        payload=database.get_job("test-job")["payload"]
        self.assertEqual(payload["custom_vocabulary"],["S-ring","Mercury"])
        vocabulary.review(term,"disabled",2,"admin")
        self.assertEqual(database.get_job("test-job")["payload"]["vocabulary_snapshot"],payload["vocabulary_snapshot"])

    def test_private_source_vocabulary_never_enters_cloud_snapshot(self):
        term=vocabulary.propose("SecretPart",["secret part"],"","editor",meeting_id=self.meeting(private=True))
        vocabulary.review(term,"approved",1,"admin")
        self.assertFalse(vocabulary.snapshot()["terms"])
        self.assertEqual(vocabulary.snapshot(local_only=True)["terms"][0]["canonical"],"SecretPart")

    def test_approved_terms_override_legacy_normalizer_without_cascade(self):
        @vocabulary.preserve_approved_terms
        def legacy(text): return text.replace("Qisda","佳世達").replace("alias","other")
        with vocabulary.vocabulary_context({"terms":[{"canonical":"Qisda","aliases":["alias"]}]}):
            self.assertEqual(legacy("Qisda and alias"),"Qisda and Qisda")

    def test_checkpoint_identity_changes_with_terms_and_diarization_configuration(self):
        from backend.job_checkpoints import JobCheckpoints
        scope=JobCheckpoints(self.root,"job","source")
        stage="_generate_meeting_content_from_transcript"
        before=scope.path(stage,{"text":"same"})
        with vocabulary.vocabulary_context({"sha256":"new","terms":[]}):
            self.assertNotEqual(before,scope.path(stage,{"text":"same"}))
        with patch.dict("os.environ",{"SPEAKER_CLUSTER_THRESHOLD":"0.95"}):
            self.assertNotEqual(before,scope.path(stage,{"text":"same"}))
        self.assertEqual(scope.path("verified_segment",{"context":{}}),
                         scope.path("verified_segment",{"context":{"dictionary_sha256":None}}))

    def local_job(self, job_id="local-test"):
        from backend.job_queue import enqueue_audio_job
        audio=self.root/"local.wav";audio.write_bytes(b"injected local audio")
        enqueue_audio_job(job_id,audio,self.root,"fake",processing_mode="local",meeting_title="Private")
        return audio

    def test_local_job_completes_without_constructing_cloud_client(self):
        from backend import tasks
        audio=self.local_job()
        with patch("backend.local_processing.transcribe_locally",return_value="[00:00] **[發言者 A]**：Local test 12."), \
             patch.object(tasks.genai,"Client",side_effect=AssertionError("Cloud is forbidden")) as client:
            output=tasks.process_audio_task("local-test",audio,self.root)
        self.assertTrue(output.is_file());client.assert_not_called()
        record=database.get_meeting(database.get_meeting_by_job_id("local-test")["id"])
        self.assertEqual(database.get_job("local-test")["status"],"done")
        self.assertTrue(meeting_is_private(record))
        self.assertEqual(record["quality_report"]["processing_policy"]["cloud_requests"],0)
        self.assertEqual(record["review_status"],"needs_review")
        refreshed=tasks.recheck_transcript_quality_report("[00:00] **[A]**：Local test 12.",record["quality_report"])
        self.assertEqual(refreshed["processing_policy"]["mode"],"local")

    def test_local_cancellation_and_lease_loss_do_not_retry_or_publish(self):
        from backend import tasks
        audio=self.local_job()
        database.request_job_cancel("local-test")
        with patch("backend.local_processing.transcribe_locally") as infer:
            self.assertIsNone(tasks.process_audio_task("local-test",audio,self.root))
            infer.assert_not_called()
        self.assertEqual(database.get_job("local-test")["status"],"cancelled")
        self.assertIsNone(database.get_meeting_by_job_id("local-test"))
        audio=self.local_job("stale")
        with patch.object(database,"job_lease_is_current",return_value=False), patch.object(database,"update_job_status") as update:
            self.assertIsNone(tasks.process_audio_task("stale",audio,self.root,worker_id="old",worker_generation=1))
            update.assert_not_called()

    def test_private_semantic_review_is_rejected_before_queueing(self):
        import asyncio
        from backend import main
        meeting_id=self.meeting(private=True)
        with patch.object(main,"enqueue_meeting_semantic_review_job") as enqueue:
            with self.assertRaises(PermissionError):
                asyncio.run(main.enqueue_meeting_transcript_semantic_review(meeting_id))
            enqueue.assert_not_called()

    def test_private_evidence_upload_is_rejected_before_reading_attachment(self):
        import asyncio
        from backend import main
        file=Mock()
        with self.assertRaises(PermissionError):
            asyncio.run(main.add_meeting_evidence(self.meeting(private=True),file=file))
        file.read.assert_not_called()

    def test_local_upload_missing_models_is_rejected_before_reading_audio(self):
        import asyncio
        from fastapi import HTTPException
        from backend import main
        file=Mock()
        with patch("backend.local_speech.capabilities",return_value={"local_transcription":False,"message":"missing models"}):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(main.upload_media(file=file,processing_mode="local"))
        self.assertEqual(error.exception.status_code,409);file.read.assert_not_called()

    def test_legacy_cli_cannot_upload_a_private_source(self):
        import meeting_assistant as cli
        path=self.root/"private.wav";path.write_bytes(b"private audio")
        source_mode(path,"local")
        client=Mock()
        with self.assertRaises(PermissionError):cli.upload_audio_file(client,path,"audio/wav")
        client.files.upload.assert_not_called()


class SpeechMetricTests(unittest.TestCase):
    def test_anonymous_label_permutation_has_zero_der(self):
        ref=[{"start":0,"end":4,"speaker":"A"},{"start":5,"end":8,"speaker":"B"}]
        hyp=[dict(ref[0],speaker="B"),dict(ref[1],speaker="A")]
        self.assertEqual(diarization_error(ref,hyp)["der"],0)

    def test_missing_speaker_and_false_alarm_are_penalized(self):
        ref=[{"start":0,"end":10,"speaker":"A"}]
        hyp=[{"start":0,"end":5,"speaker":"B"},{"start":12,"end":14,"speaker":"B"}]
        self.assertAlmostEqual(diarization_error(ref,hyp)["der"],.7)

    def test_overlap_is_scored_and_words_numbers_are_not_hidden(self):
        ref=[{"start":0,"end":10,"speaker":"A"},{"start":0,"end":10,"speaker":"B"}]
        self.assertEqual(diarization_error(ref,ref[:1])["der"],.5)
        metrics=text_metrics("測試 Mercury 12", "測式 Mercury 20", ["Mercury"])
        self.assertEqual(metrics["chinese_cer"],.5);self.assertEqual(metrics["english_wer"],0)
        self.assertEqual(metrics["number_sequence_error_rate"],1)

    def test_whole_meeting_labels_preserve_text_and_times(self):
        source="[00:00] **[片段一 A]**：Hello 12.\n[00:10] **[片段二 B]**：中文。\n[00:20] **[片段三 A]**：Again."
        segments=[{"start":0,"end":10,"speaker":9},{"start":10,"end":20,"speaker":2},{"start":20,"end":30,"speaker":9}]
        actual=align_speakers(source,segments)
        self.assertIn("[00:00] **[發言者 A]**：Hello 12.",actual)
        self.assertIn("[00:20] **[發言者 A]**：Again.",actual)
        self.assertIn("[00:10] **[發言者 B]**：中文。",actual)

    def test_ambiguous_overlap_and_silence_are_not_single_speaker(self):
        source="[00:00] **[A]**：Mixed.\n[00:10] **[B]**：Unknown."
        segments=[{"start":0,"end":10,"speaker":1},{"start":0,"end":10,"speaker":2}]
        actual=align_speakers(source,segments)
        self.assertIn("多人／切換待確認",actual);self.assertIn("發言者不明",actual)

    def test_long_turn_checks_speaker_changes_in_its_tail(self):
        source="[00:00] **[A]**：First and second voices.\n[00:25] **[B]**：Next."
        segments=[{"start":0,"end":15,"speaker":1},{"start":15,"end":30,"speaker":2}]
        self.assertIn("[00:00] **[多人／切換待確認]**",align_speakers(source,segments))

    def test_empty_acoustic_result_does_not_confirm_guessed_cloud_labels(self):
        from backend.local_speech import speech_context, diarize_transcript, speech_report
        with speech_context("injected.wav"), patch.dict("os.environ",{"MEETING_DIARIZATION_ENABLED":"1"}), \
             patch("backend.local_speech.capabilities",return_value={"diarization":True}), \
             patch("backend.local_speech.infer_segments",return_value=[]):
            self.assertIn("發言者不明",diarize_transcript("[00:00] **[A]**：Test."))
            self.assertEqual(speech_report()["status"],"no_speech")


if __name__ == "__main__":
    unittest.main()
