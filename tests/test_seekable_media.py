import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import seekable_media


class SeekableMediaTests(unittest.TestCase):
    def test_webm_is_stream_copied_to_validated_atomic_cache_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "recording.webm"
            source.write_bytes(b"\x1aE\xdf\xa3original-live-webm")
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            cache_dir = root / "playback"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                if command[0] == "ffmpeg-test":
                    Path(command[-1]).write_bytes(source.read_bytes() + b"-seekable")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "45.125\n", "")

            with mock.patch.dict(
                os.environ,
                {"FFMPEG_PATH": "ffmpeg-test", "FFPROBE_PATH": "ffprobe-test"},
                clear=False,
            ), mock.patch.object(seekable_media.subprocess, "run", side_effect=fake_run):
                result = seekable_media.ensure_seekable_playback_media(source, cache_dir)

            self.assertTrue(result.is_file())
            self.assertEqual(result.read_bytes(), source.read_bytes() + b"-seekable")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_digest)
            self.assertEqual(commands[0][0], "ffmpeg-test")
            self.assertIn("copy", commands[0])
            self.assertNotIn("shell", commands[0])
            self.assertEqual(commands[1][0], "ffprobe-test")
            self.assertFalse(list(cache_dir.glob("*.tmp.webm")))

            with mock.patch.object(seekable_media.subprocess, "run") as cached_run:
                cached_result = seekable_media.ensure_seekable_playback_media(source, cache_dir)
            self.assertEqual(cached_result, result)
            cached_run.assert_not_called()

    def test_invalid_duration_does_not_publish_partial_derivative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "recording.webm"
            source.write_bytes(b"\x1aE\xdf\xa3original-live-webm")
            cache_dir = root / "playback"

            def fake_run(command, **_kwargs):
                if command[0] == "ffmpeg-test":
                    Path(command[-1]).write_bytes(b"partial")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "N/A\n", "")

            with mock.patch.dict(
                os.environ,
                {"FFMPEG_PATH": "ffmpeg-test", "FFPROBE_PATH": "ffprobe-test"},
                clear=False,
            ), mock.patch.object(seekable_media.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(seekable_media.SeekableMediaError):
                    seekable_media.ensure_seekable_playback_media(source, cache_dir)

            self.assertFalse(list(cache_dir.glob("*.seekable.webm")))
            self.assertFalse(list(cache_dir.glob("*.tmp.webm")))

    def test_non_webm_media_is_returned_without_remux(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "recording.mp4"
            source.write_bytes(b"mp4")
            with mock.patch.object(seekable_media.subprocess, "run") as run:
                result = seekable_media.ensure_seekable_playback_media(
                    source,
                    Path(tmpdir) / "playback",
                )
            self.assertEqual(result, source)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
