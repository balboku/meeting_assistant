"""Native-inference isolation, bounded failure and verified resume contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend import local_speech, speech_process
from backend.job_checkpoints import ACTIVE_CHECKPOINTS, JobCheckpoints, load_verified_segment, save_verified_segment


class SpeechProcessTests(unittest.TestCase):
    def test_timeout_and_cancel_reap_real_child(self):
        real_popen = subprocess.Popen
        for cancel in (False, True):
            children = []
            calls = []
            def launch(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            def check():
                calls.append(1)
                if cancel and len(calls) >= 3:
                    raise LookupError("cancelled")
            with tempfile.TemporaryFile() as log, patch.object(speech_process.subprocess, "Popen", side_effect=launch):
                with self.assertRaises(LookupError if cancel else speech_process.SpeechWorkerTimeout):
                    speech_process._run([sys.executable, "-c", "import time; time.sleep(30)"],
                        deadline=time.monotonic() + 0.8, check_cancelled=check, log=log)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())

    def test_nonzero_exit_is_failure(self):
        with tempfile.TemporaryFile() as log, self.assertRaises(speech_process.SpeechWorkerError):
            speech_process._run([sys.executable, "-c", "raise SystemExit(3)"],
                deadline=time.monotonic() + 10, check_cancelled=lambda: None, log=log)

    def test_child_holding_gil_does_not_stop_parent_heartbeat_or_http(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        @app.get("/livez")
        async def live():
            return {"status": "ok"}
        errors, checks = [], []
        launched = threading.Event()
        native_sleep = ("import ctypes; ctypes.PyDLL('kernel32').Sleep(1600)" if os.name == "nt"
                        else "import ctypes; ctypes.PyDLL(None).sleep(2)")
        def work():
            try:
                with tempfile.TemporaryFile() as log:
                    launched.set()
                    speech_process._run([sys.executable, "-c", native_sleep],
                        deadline=time.monotonic() + 10, check_cancelled=lambda: checks.append(time.monotonic()), log=log)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(launched.wait(3))
        with TestClient(app) as client:
            for _ in range(3):
                started = time.monotonic()
                self.assertEqual(client.get("/livez").json(), {"status": "ok"})
                self.assertLess(time.monotonic() - started, 1)
                time.sleep(.1)
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertGreaterEqual(len(checks), 3)

    def test_runner_validates_result_and_cleans_temp_directory(self):
        segments = [{"start": 0, "end": 2, "speaker": 0}]
        for value, fails in ((segments, False), ([{"start": 3, "end": 1, "speaker": 0}], True),
                             ([{"start": 0, "end": float("nan"), "speaker": 0}], True), (None, True)):
            commands, folders = [], []
            def run(command, **kwargs):
                commands.append(command)
                if command[0] == sys.executable:
                    result = Path(command[6])
                    folders.append(result.parent)
                    result.write_text(json.dumps(value), encoding="utf-8")
                    self.assertEqual(kwargs["env"]["MEETING_ALLOW_CLOUD"], "0")
            with patch.object(speech_process, "_run", side_effect=run):
                if fails:
                    with self.assertRaises(speech_process.SpeechWorkerError):
                        speech_process.run_speech("diarize", "a.wav", models={})
                else:
                    self.assertEqual(speech_process.run_speech("diarize", "a.wav", models={}), segments)
            self.assertEqual(len(commands), 2)
            self.assertFalse(folders[0].exists())

    def test_timeout_settings_are_bounded(self):
        for value, expected in (("nan", 300), ("oops", 300), ("-1", 1), ("999999", 7200), ("23", 23)):
            with patch.dict(os.environ, {"MEETING_DIARIZATION_TIMEOUT_SECONDS": value}):
                self.assertEqual(speech_process.timeout_seconds("diarize"), expected)

    def test_failed_diarization_preserves_text_and_time_but_not_guessed_identity(self):
        text = "[00:02] **[A]**：English 12.\n[01:03] **[B]**：中文 34。"
        for error, status in ((speech_process.SpeechWorkerTimeout("timeout"), "timed_out"),
                              (speech_process.SpeechWorkerError("crash"), "failed")):
            with local_speech.speech_context("x.wav"), patch.dict(os.environ, {"MEETING_DIARIZATION_ENABLED": "1"}), \
                 patch.object(local_speech, "capabilities", return_value={"diarization": True}), \
                 patch.object(local_speech, "infer_segments", side_effect=error) as infer:
                actual = local_speech.diarize_transcript(text)
                self.assertEqual(actual, text.replace("[A]", "[發言者不明]").replace("[B]", "[發言者不明]"))
                self.assertEqual(local_speech.speech_report()["status"], status)
                self.assertIn("人工確認", local_speech.speech_report()["message"])
                local_speech.diarize_transcript(text)
                infer.assert_called_once()

    def test_cancellation_is_not_swallowed_as_diarization_degradation(self):
        with local_speech.speech_context("x.wav"), patch.dict(os.environ, {"MEETING_DIARIZATION_ENABLED": "1"}), \
             patch.object(local_speech, "capabilities", return_value={"diarization": True}), \
             patch.object(local_speech, "infer_segments", side_effect=RuntimeError("lease lost")):
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                local_speech.diarize_transcript("[00:00] [A]: Test")

    def test_private_asr_timeout_propagates_without_cloud_or_fabricated_output(self):
        with patch.object(local_speech, "capabilities", return_value={"local_transcription": True}), \
             patch.object(local_speech, "run_speech", side_effect=speech_process.SpeechWorkerTimeout("timeout")) as run:
            with self.assertRaises(speech_process.SpeechWorkerTimeout):
                local_speech.transcribe_locally("private.wav")
            self.assertEqual(run.call_args.args[0], "transcribe")

    def test_interrupted_job_reuses_verified_segments_but_force_and_context_change_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            context = {"source_audio_sha256": "sha", "dictionary_sha256": "v1"}
            token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(folder, "job", "sha"))
            try:
                save_verified_segment(0, context, "verified", ["model"])
            finally:
                ACTIVE_CHECKPOINTS.reset(token)
            for forced, source, ctx, expected in ((False, "sha", context, "verified"),
                    (True, "sha", context, None), (False, "new", context, None),
                    (False, "sha", dict(context, dictionary_sha256="v2"), None)):
                token = ACTIVE_CHECKPOINTS.set(JobCheckpoints(folder, "job", source, forced=forced, resume_verified=True))
                try:
                    self.assertEqual(load_verified_segment(0, ctx), expected)
                finally:
                    ACTIVE_CHECKPOINTS.reset(token)

    def test_job_attempt_controls_resume_without_changing_cache_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            fresh = JobCheckpoints.for_job(folder, "job", "sha", {"attempts": 1})
            retry = JobCheckpoints.for_job(folder, "job", "sha", {"attempts": 2})
            forced = JobCheckpoints.for_job(folder, "job", "sha", {"attempts": 2}, True)
            self.assertFalse(fresh.resume_verified)
            self.assertTrue(retry.resume_verified)
            self.assertFalse(forced.resume_verified)
            self.assertEqual(fresh.path("verified_segment", {}), retry.path("verified_segment", {}))


if __name__ == "__main__":
    unittest.main()
