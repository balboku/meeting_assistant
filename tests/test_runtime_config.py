"""Runtime configuration and startup health API regressions."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.http_helpers import ROOT, asgi_request


class ConfigRegressionTests(unittest.TestCase):
    def test_database_and_runtime_paths_can_be_overridden_by_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                **os.environ,
                "DB_PATH": str(tmp_path / "custom.db"),
                "MEETING_TEMP_DIR": str(tmp_path / "custom-temp"),
                "MEETING_OUTPUT_DIR": str(tmp_path / "custom-output"),
                "MEETING_SOURCE_AUDIO_DIR": str(tmp_path / "custom-source-audio"),
            }
            script = (
                "import json; "
                "import backend.database as database; "
                "import backend.main as main; "
                "print(json.dumps({"
                "'db': str(database.DB_PATH), "
                "'temp': str(main.TEMP_DIR), "
                "'output': str(main.OUTPUT_DIR), "
                "'source_audio': str(main.SOURCE_AUDIO_DIR)"
                "}, ensure_ascii=False))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["db"], str(tmp_path / "custom.db"))
        self.assertEqual(payload["temp"], str(tmp_path / "custom-temp"))
        self.assertEqual(payload["output"], str(tmp_path / "custom-output"))
        self.assertEqual(payload["source_audio"], str(tmp_path / "custom-source-audio"))

    def test_config_endpoint_reports_runtime_upload_limit_and_formats(self):
        import backend.main as main

        original_mb = main.MAX_UPLOAD_MB
        original_bytes = main.MAX_UPLOAD_BYTES
        main.MAX_UPLOAD_MB = 123
        main.MAX_UPLOAD_BYTES = 123 * 1024 * 1024
        try:
            response = asgi_request(main.app, "GET", "/config")
        finally:
            main.MAX_UPLOAD_MB = original_mb
            main.MAX_UPLOAD_BYTES = original_bytes

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["max_upload_mb"], 123)
        self.assertEqual(payload["max_upload_bytes"], 123 * 1024 * 1024)
        self.assertEqual(payload["model"], main.GEMINI_MODEL)
        self.assertEqual(payload["transcription_model"], main.GEMINI_MODEL)
        self.assertEqual(payload["summary_model"], main.SUMMARY_MODEL)
        self.assertEqual(payload["summary_fallback_model"], main.SUMMARY_FALLBACK_MODEL)
        self.assertEqual(payload["summary_verifier_model"], main.SUMMARY_VERIFIER_MODEL)
        self.assertEqual(payload["source_media_archive_retention_days"], main.SOURCE_MEDIA_ARCHIVE_RETENTION_DAYS)
        self.assertFalse(payload["auth"]["enabled"])
        self.assertEqual(payload["recording_profiles"]["audio_standard"]["audio_bps"], 48000)
        self.assertEqual(payload["recording_profiles"]["video_balanced"]["video_fps"], 15)
        self.assertEqual(payload["recording_profiles"]["video_balanced"]["label"], "錄影平衡")
        self.assertIn(".mp3", payload["supported_extensions"])
        self.assertIn(".mp4", payload["supported_extensions"])

    def test_upload_media_is_primary_and_upload_audio_is_deprecated_alias(self):
        import backend.main as main

        response = asgi_request(main.app, "GET", "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/upload-media", paths)
        self.assertIn("/upload-audio", paths)
        self.assertEqual(paths["/upload-media"]["post"]["summary"], "上傳音訊或影片並觸發 AI 處理")
        self.assertFalse(paths["/upload-media"]["post"].get("deprecated", False))
        self.assertTrue(paths["/upload-audio"]["post"]["deprecated"])
        self.assertIn("相容舊路徑", paths["/upload-audio"]["post"]["summary"])


class StartupHealthRegressionTests(unittest.TestCase):
    def test_startup_health_reports_missing_api_key_and_unwritable_paths(self):
        from backend.maintenance import run_startup_health_checks

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            checks = run_startup_health_checks(
                temp_dir=tmp_path / "missing-temp",
                output_dir=tmp_path / "missing-output",
                static_vendor_dir=tmp_path / "missing-vendor",
                env={},
            )

        names = {check["name"]: check for check in checks}
        self.assertEqual(names["gemini_api_key"]["status"], "failed")
        self.assertEqual(names["temp_dir"]["status"], "failed")
        self.assertEqual(names["output_dir"]["status"], "failed")
        self.assertEqual(names["static_vendor"]["status"], "failed")

    def test_startup_health_uses_configured_media_tool_paths(self):
        from backend.maintenance import run_startup_health_checks

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            ffmpeg_path = tmp_path / "ffmpeg.exe"
            ffprobe_path = tmp_path / "ffprobe.exe"
            ffmpeg_path.touch()
            ffprobe_path.touch()

            checks = run_startup_health_checks(
                temp_dir=tmp_path,
                output_dir=tmp_path,
                static_vendor_dir=tmp_path,
                env={
                    "GEMINI_API_KEY": "configured",
                    "FFMPEG_PATH": str(ffmpeg_path),
                    "FFPROBE_PATH": str(ffprobe_path),
                },
            )

        names = {check["name"]: check for check in checks}
        self.assertEqual(names["media_tools"]["status"], "ok")
        self.assertIn(str(ffmpeg_path), names["media_tools"]["detail"])
        self.assertIn(str(ffprobe_path), names["media_tools"]["detail"])

    def test_health_endpoint_includes_startup_checks(self):
        import backend.main as main

        response = asgi_request(main.app, "GET", "/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("checks", payload)
        self.assertFalse(payload["auth"]["enabled"])
        self.assertTrue(any(check["name"] == "database" for check in payload["checks"]))
        self.assertIn("loaded_commit", payload["build"])
        self.assertIn("matches_workspace", payload["build"])
        self.assertIn("worker_running", payload["runtime"])
        self.assertIn("schema_version", payload["runtime"])
        self.assertIn("segment_cache_version", payload["runtime"])



if __name__ == "__main__":
    unittest.main()
