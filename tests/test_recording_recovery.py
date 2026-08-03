from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_regressions import asgi_request


ROOT = Path(__file__).resolve().parents[1]


class SourceMediaRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.source_dir = self.root / "source_audio"
        self.output_dir = self.root / "output"
        self.source_dir.mkdir()
        self.output_dir.mkdir()
        self.source_path = self.source_dir / "interrupted.webm"
        self.source_path.write_bytes(b"recoverable-media")

    def _patches(self):
        import backend.auth as auth
        import backend.source_media_recovery as recovery

        return (
            mock.patch.object(auth, "AUTH_FEATURE_ENABLED", False),
            mock.patch.object(recovery, "SOURCE_AUDIO_DIR", self.source_dir),
            mock.patch.object(recovery, "OUTPUT_DIR", self.output_dir),
            mock.patch.object(recovery, "validate_media_magic", return_value=None),
        )

    def test_unlinked_source_media_can_be_restarted_without_browser_copy(self) -> None:
        import backend.main as main
        import backend.source_media_recovery as recovery

        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(recovery, "list_meeting_source_audio_refs", return_value=[]), \
                mock.patch.object(recovery, "list_jobs", return_value=[]), \
                mock.patch.object(recovery, "enqueue_audio_job") as enqueue:
            response = asgi_request(
                main.app,
                "POST",
                "/source-media/inventory/interrupted.webm/restart?title=Recovered%20meeting",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        call = enqueue.call_args.kwargs
        self.assertEqual(call["audio_path"], self.source_path.resolve())
        self.assertEqual(call["output_dir"], self.output_dir)
        self.assertEqual(call["meeting_title"], "Recovered meeting")
        self.assertEqual(call["source"], "source_media_recovery")
        self.assertIn("中斷", call["client_recording_warning"])

    def test_linked_or_active_source_media_is_not_duplicated(self) -> None:
        import backend.main as main
        import backend.source_media_recovery as recovery

        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(
                    recovery,
                    "list_meeting_source_audio_refs",
                    return_value=[{"source_audio": str(self.source_path)}],
                ), \
                mock.patch.object(recovery, "enqueue_audio_job") as enqueue:
            linked_response = asgi_request(
                main.app,
                "POST",
                "/source-media/inventory/interrupted.webm/restart",
            )

        self.assertEqual(linked_response.status_code, 409)
        enqueue.assert_not_called()

        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(recovery, "list_meeting_source_audio_refs", return_value=[]), \
                mock.patch.object(
                    recovery,
                    "list_jobs",
                    side_effect=lambda **kwargs: [{
                        "job_id": "active-job",
                        "payload": {"audio_path": str(self.source_path)},
                    }] if kwargs.get("status") == "pending" else [],
                ), \
                mock.patch.object(recovery, "enqueue_audio_job") as enqueue:
            active_response = asgi_request(
                main.app,
                "POST",
                "/source-media/inventory/interrupted.webm/restart",
            )

        self.assertEqual(active_response.status_code, 409)
        self.assertIn("active-job", active_response.json()["detail"])
        enqueue.assert_not_called()

    def test_restart_route_requires_write_permission(self) -> None:
        from backend.security_policy import permission_for_request

        self.assertEqual(
            permission_for_request(
                "POST",
                "/source-media/inventory/interrupted.webm/restart",
            ),
            "meeting:write",
        )


class BrowserRecordingRecoveryTests(unittest.TestCase):
    def test_recovery_assets_and_recording_hooks_are_wired(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "recording-recovery.js").read_text(encoding="utf-8")

        self.assertIn('/static/recording-recovery.css', html)
        self.assertIn('/static/recording-recovery.js', html)
        self.assertIn('id="btn-open-record"', html)
        self.assertIn('await RecordingRecovery.begin(', html)
        self.assertIn('RecordingRecovery.captureChunk(e.data, recSeconds)', html)
        self.assertIn('await RecordingRecovery.finish(', html)
        self.assertIn('await RecordingRecovery.removeActive()', html)
        self.assertIn('await RecordingRecovery.markUploadFailed(err.message)', html)
        self.assertIn('RecordingRecovery.restartServerMedia(this.dataset.sourceFile)', html)
        self.assertIn("const DB_NAME = 'meeting-assistant-recording-recovery'", script)
        self.assertIn("const SESSION_STORE = 'sessions'", script)
        self.assertIn("const CHUNK_STORE = 'chunks'", script)
        self.assertIn("return '錄製中斷'", script)
        self.assertIn("return '上傳失敗，可重啟'", script)
        self.assertIn("window.RecordingRecovery", script)

    def test_recording_recovery_javascript_is_syntax_valid(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node executable is required for JavaScript syntax validation")
        result = subprocess.run(
            [node, "--check", str(ROOT / "static" / "recording-recovery.js")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
