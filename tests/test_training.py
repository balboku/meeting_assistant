"""Course lifecycle, queue boundaries, privacy, references and recoverability."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from backend import database, training_store as store
from backend.auth import AuthActor
from backend.training_api import router
from tests.http_helpers import asgi_request


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source_audio"
        self.source.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(database, "DB_PATH", self.root / "meetings.db"))
        self.stack.enter_context(patch.dict("os.environ", {"MEETING_ALLOW_CLOUD":"1","GEMINI_API_KEY":"fake-test-key","MEETING_DIARIZATION_ENABLED":"0"}))
        database.init_db()
        self.course = store.create_course("課程測試", "cloud")
        self.app = FastAPI()
        self.app.include_router(router)

    def part(self, client="one", course_id=None):
        part = store.reserve_part(course_id or self.course, client, f"章節 {client}")
        audio = self.source / f"{part['id']}.wav"
        audio.write_bytes(b"synthetic media " + client.encode())
        digest = hashlib.sha256(audio.read_bytes()).hexdigest()
        mode = store.get_course(part["course_id"])["processing_mode"]
        store.bind_upload(part["id"], audio, digest, mode=mode)
        database.create_job(part["job_id"], payload={"audio_path":str(audio),"output_dir":str(self.root),"processing_mode":mode})
        return part, audio

    def complete(self, part, audio, text="[00:01] [發言者 A]：Learn the workflow.\n[00:10] [發言者 B]：操作步驟測試。"):
        return store.finish_transcript(part["job_id"], text, audio, self.root)

    def close(self):
        c = store.get_course(self.course)
        store.set_course_status(self.course, "closed", c["version"])
        return database.get_job(store.get_course(self.course)["notes_job_id"])

    def response(self):
        from backend.training_processing import SECTIONS
        value = {key: [] for key in SECTIONS}
        value["overview"] = [{"text":"Workflow 說明", "timecode":"00:01"}]
        return Mock(text=json.dumps(value)), "free-model"

    def test_reservation_is_idempotent_and_concurrent(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            parts = list(pool.map(lambda _: store.reserve_part(self.course, "same"), range(3)))
        self.assertEqual(len({p["id"] for p in parts}), 1)
        self.assertEqual(len(store.get_course(self.course)["parts"]), 1)

    def test_separate_parts_get_distinct_jobs_and_original_timestamps(self):
        p1,a1=self.part();p2,a2=self.part("two")
        self.complete(p1,a1);self.complete(p2,a2)
        c=store.get_course(self.course,include_transcripts=True)
        self.assertEqual(len(c["parts"]),2)
        self.assertNotEqual(p1["job_id"],p2["job_id"])
        self.assertTrue(all(p["transcript"].startswith("[00:01]") for p in c["parts"]))
        self.assertFalse(database.list_meetings())

    def test_closing_waits_for_all_parts_then_queues_once(self):
        p1,a1=self.part();p2,a2=self.part("two")
        self.close()
        self.assertIsNone(store.get_course(self.course)["notes_job_id"])
        self.complete(p1,a1)
        self.assertIsNone(store.get_course(self.course)["notes_job_id"])
        self.complete(p2,a2)
        job_id=store.get_course(self.course)["notes_job_id"]
        self.assertEqual(database.get_job(job_id)["task_type"],"training_notes")
        self.complete(p2,a2)
        self.close()
        self.assertEqual(store.get_course(self.course)["notes_job_id"],job_id)

    def test_empty_course_and_missing_parts_cannot_look_complete(self):
        with self.assertRaises(ValueError):self.close()
        store.reserve_part(self.course,"missing")
        self.close()
        c=store.get_course(self.course)
        self.assertFalse(c["notes_current"])
        self.assertIsNone(c["notes_job_id"])
        self.assertEqual(c["parts"][0]["state"],"saved")

    def test_closed_course_rejects_new_parts_but_accepts_existing_retry(self):
        p,_=self.part();self.close()
        self.assertEqual(store.reserve_part(self.course,"one")["id"],p["id"])
        with self.assertRaises(ValueError):store.reserve_part(self.course,"new")

    def test_order_and_stale_version_are_checked(self):
        p1,_=self.part();p2,_=self.part("two");version=store.get_course(self.course)["version"]
        store.reorder(self.course,[p2["id"],p1["id"]],version)
        self.assertEqual(store.get_course(self.course)["parts"][0]["id"],p2["id"])
        with self.assertRaises(ValueError):store.reorder(self.course,[p1["id"],p2["id"]],version)
        with self.assertRaises(ValueError):store.reorder(self.course,[p1["id"]],version+1)

    def test_different_file_cannot_overwrite_same_part(self):
        p,a=self.part()
        with self.assertRaises(ValueError):store.bind_upload(p["id"],a,"different",mode="cloud")
        with self.assertRaises(ValueError):store.upload_target(p["id"],"local")
        with self.assertRaises(KeyError):store.upload_target("unknown","cloud")

    def test_finished_part_is_reused_without_retranscription(self):
        from backend import job_queue
        p,a=self.part();self.complete(p,a)
        with patch.object(job_queue,"process_audio_task",side_effect=AssertionError("must reuse")):
            job_queue.JobQueueWorker()._process_audio_job(database.get_job(p["job_id"]))
        self.assertEqual(database.get_job(p["job_id"])["status"],"done")

    def test_ordinary_meeting_does_not_enter_training_completion(self):
        self.assertIsNone(store.finish_transcript("ordinary","text",Path("ordinary.wav"),self.root))

    def test_course_does_not_apply_meeting_specific_terminology(self):
        from backend import tasks
        from backend.training_context import training_context
        source="Qisda and Jasta are words used in this training example."
        ordinary_profile=tasks._audio_preprocessing_profile()
        with training_context(True):
            self.assertEqual(tasks._normalize_domain_terms(source),source)
            self.assertNotEqual(tasks._audio_preprocessing_profile(),ordinary_profile)
        self.assertEqual(tasks._audio_preprocessing_profile(),ordinary_profile)
        self.assertNotEqual(tasks._normalize_domain_terms(source),source)

    def test_local_part_and_notes_never_construct_cloud_client(self):
        from backend import tasks, training_processing as notes
        self.course=store.create_course("機密課程","local")
        p,a=self.part()
        with patch("backend.local_processing.transcribe_locally",return_value="[00:01] Local original."), \
             patch.object(tasks.genai,"Client",side_effect=AssertionError("no cloud")) as client:
            output=tasks.process_audio_task(p["job_id"],a,self.root)
            notes.process_notes(self.close())
        self.assertTrue(output.is_file());client.assert_not_called()
        c=store.get_course(self.course)
        self.assertTrue(c["notes_current"])
        self.assertIn("待人工整理",c["notes_markdown"])
        self.assertFalse(database.list_meetings())

    def test_course_notes_reuse_cached_batches_after_failure(self):
        from backend import training_processing as notes
        p1,a1=self.part();p2,a2=self.part("two")
        self.complete(p1,a1);self.complete(p2,a2);job=self.close()
        with patch.object(notes.genai,"Client",return_value=Mock()), \
             patch.object(notes,"_generate_text_with_fallback",side_effect=[self.response(),RuntimeError("429 quota")]):
            with self.assertRaises(RuntimeError):notes.process_notes(job)
        with patch.object(notes.genai,"Client",return_value=Mock()), \
             patch.object(notes,"_generate_text_with_fallback",return_value=self.response()) as generate:
            notes.process_notes(job)
        self.assertEqual(generate.call_count,1)
        course=store.get_course(self.course)
        self.assertTrue(course["notes_current"])
        self.assertIn(f"part={p1['id']}&t=1",course["notes_markdown"])

    def test_stale_notes_cannot_overwrite_reopened_course(self):
        from backend import training_processing as notes
        p,a=self.part();self.complete(p,a);job=self.close()
        c=store.get_course(self.course);store.set_course_status(self.course,"open",c["version"])
        with patch.object(notes.genai,"Client",side_effect=AssertionError("stale")):
            notes.process_notes(job)
        self.assertEqual(database.get_job(job["job_id"])["status"],"cancelled")
        self.assertFalse(store.get_course(self.course)["notes_markdown"])

    def test_notes_reject_invented_source_and_escape_generated_links(self):
        from backend.training_processing import parse_notes,render_notes
        raw=json.loads(self.response()[0].text);raw["overview"][0]["timecode"]="99:59"
        with self.assertRaises(ValueError):parse_notes(json.dumps(raw),"[00:01] original")
        raw["overview"][0].update(timecode="00:01",text="<script>x</script> [fake](https://bad)")
        parsed=parse_notes(json.dumps(raw),"[00:01] original")
        p,a=self.part();self.complete(p,a);c=store.get_course(self.course,include_transcripts=True)
        markdown=render_notes(c,[(c["parts"][0],parsed)])
        self.assertNotIn("<script>",markdown)
        self.assertNotIn("[fake]",markdown)

    def test_long_transcript_batches_cover_all_characters(self):
        from backend.training_processing import chunks
        text="[00:01] "+"A"*50001+"\n"+"中文\n"*10000
        batch=chunks(text)
        self.assertEqual("".join(batch),text)
        self.assertTrue(all(len(s)<=24000 for s in batch))

    def test_existing_meeting_is_snapshotted_without_modification(self):
        path=self.root/"existing.md";text="## 完整逐字稿 (Verbatim Transcript)\n[00:01] Original English."
        path.write_text(text,encoding="utf-8")
        meeting_id=database.save_meeting("既有課程","2026/10/21","recording.wav",str(path))
        before=database.get_meeting(meeting_id)
        first=store.attach_meeting(self.course,meeting_id)
        self.assertEqual(store.attach_meeting(self.course,meeting_id),first)
        self.assertEqual(database.get_meeting(meeting_id),before)
        self.assertEqual(path.read_text(encoding="utf-8"),text)

    def test_private_meeting_cannot_be_added_to_cloud_course(self):
        path=self.root/"private.md";path.write_text("## 完整逐字稿\n[00:01] secret",encoding="utf-8")
        meeting_id=database.save_meeting("secret","2026/10/21","private.wav",str(path),quality_report={"processing_policy":{"mode":"local"}})
        with self.assertRaises(ValueError):store.attach_meeting(self.course,meeting_id)

    def test_viewer_is_read_only_and_cross_course_part_is_not_found(self):
        p,_=self.part();second=store.create_course("second","cloud")
        with patch("backend.training_api.actor_from_request",return_value=AuthActor("viewer","viewer",enabled=True)):
            self.assertEqual(asgi_request(self.app,"GET","/training/courses").status_code,200)
            self.assertEqual(asgi_request(self.app,"POST","/training/courses",json={"title":"bad"}).status_code,403)
            self.assertEqual(asgi_request(self.app,"PUT",f"/training/courses/{self.course}/status",json={"status":"closed","version":1}).status_code,403)
            self.assertEqual(asgi_request(self.app,"GET",f"/training/courses/{second}/parts/{p['id']}/transcript").status_code,404)

    def test_upload_queues_training_job_and_duplicate_send_is_idempotent(self):
        from backend import main
        p=store.reserve_part(self.course,"upload")
        with patch("backend.auth.AUTH_FEATURE_ENABLED",False), \
             patch.object(main,"SOURCE_AUDIO_DIR",self.source),patch.object(main,"OUTPUT_DIR",self.root), \
             patch.object(main,"validate_media_magic",return_value=None):
            for _ in range(2):
                result=asgi_request(main.app,"POST","/upload-media",data={"training_part_id":p["id"],"processing_mode":"cloud"},files={"file":("part.wav",b"fake media","audio/wav")})
                self.assertEqual(result.status_code,202,result.text)
                self.assertEqual(result.json()["job_id"],p["job_id"])
        self.assertEqual(len(database.list_jobs()),1)
        self.assertTrue(store.get_course(self.course)["parts"][0]["source_audio"])

    def test_course_sources_are_protected_from_orphan_archival(self):
        from backend import main
        p,a=self.part();self.complete(p,a)
        with patch.object(main,"SOURCE_AUDIO_DIR",self.source),patch.object(main,"_storage_source_media_type",return_value="audio"):
            inventory=main._source_media_inventory()
            self.assertEqual(inventory.unlinked_files,0)
            self.assertEqual(inventory.files[0].linked_course_id,self.course)
            self.assertNotIn(a.name,main._unlinked_source_media_names())
            with self.assertRaises(Exception) as error:main._delete_unlinked_source_media(a.name)
            self.assertEqual(error.exception.status_code,409)
        self.assertTrue(a.exists())

    def test_snapshot_restore_preserves_course_sources_and_notes(self):
        from backend.maintenance import backup_database,create_record_snapshot,verify_record_snapshot,restore_record_snapshot,record_state_fingerprint
        p,a=self.part();self.complete(p,a)
        before=record_state_fingerprint(database.DB_PATH)
        store.cached_notes(self.course,"key",{"overview":[]})
        self.assertNotEqual(before,record_state_fingerprint(database.DB_PATH))
        backup=self.root/"backups";saved=backup_database(database.DB_PATH,backup)
        archive=create_record_snapshot(saved,backup,source_media_dir=self.source,markdown_dir=self.root)
        self.assertTrue(verify_record_snapshot(archive)["recoverability_complete"])
        destination=self.root/"restored";report=restore_record_snapshot(archive,destination)
        self.assertTrue(Path(report["runtime"]["runtime_database"]).is_file())
        import sqlite3
        with closing(sqlite3.connect(report["runtime"]["runtime_database"])) as conn:
            row=conn.execute("SELECT source_audio,output_path,transcript FROM training_parts").fetchone()
        self.assertTrue(Path(row[1]).is_file())
        self.assertIn("Learn the workflow",row[2])
        self.assertTrue((Path(report["runtime"]["runtime_output"])/"source_audio"/row[0]).is_file())

    def test_cloud_audio_pipeline_stops_at_transcript_for_short_and_long_parts(self):
        from backend import tasks
        for number in (1,2):
            with self.subTest(slices=number), ExitStack() as stack:
                part,audio=self.part(f"pipeline-{number}")
                slices=[tasks.AudioSlice(audio,i*60,(i+1)*60) for i in range(number)]
                stack.enter_context(patch.object(tasks.genai,"Client",return_value=Mock()))
                stack.enter_context(patch.object(tasks,"_prepare_audio_for_transcription",return_value=(audio,{})))
                stack.enter_context(patch.object(tasks,"_split_audio_to_segments",return_value=slices))
                stack.enter_context(patch.object(tasks,"_load_segment_transcript_cache",return_value=None))
                stack.enter_context(patch.object(tasks,"_transcribe_segment_with_recovery",return_value="[00:01] **[發言者 A]**：This is a course example."))
                for name in ("_segment_transcript_current_quality_issues","_speech_backed_timestamp_gap_quality_ranges",
                             "_speech_backed_transcript_local_density_quality_ranges","_transcript_repetition_repair_ranges"):
                    stack.enter_context(patch.object(tasks,name,return_value=[]))
                summary=stack.enter_context(patch.object(tasks,"_generate_meeting_content_from_transcript",side_effect=AssertionError("no DRA for courses")))
                output=tasks.process_audio_task(part["job_id"],audio,self.root,model="fake-model")
                self.assertIsNotNone(output)
                self.assertEqual(database.get_job(part["job_id"])["status"],"done")
                summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
