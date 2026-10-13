"""Inject audio instead of activating the machine's microphone or loopback."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from gui.dual_recorder import DualRecorder


class FakeDevice:
    channels = 2
    def __init__(self, *, signal=.1, fail=False):
        self.signal, self.fail = signal, fail
        self.ready = threading.Event()
    def recorder(self, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def record(self, numframes):
        if self.fail: raise OSError("device disconnected")
        time.sleep(.005)
        self.ready.set()
        return np.full((numframes, 2), self.signal, dtype=np.float32)


class DesktopCaptureTests(unittest.TestCase):
    def test_independent_tracks_can_stop_and_make_seekable_compressed_output(self):
        with tempfile.TemporaryDirectory() as folder:
            devices={"speaker":FakeDevice(signal=.2),"mic":FakeDevice(signal=.1)}
            recorder=DualRecorder(folder,provider=lambda name,loopback:devices[name])
            recorder.start(speaker_id="speaker",microphone_id="mic")
            for device in devices.values(): self.assertTrue(device.ready.wait(2))
            state=recorder.stop()
            self.assertEqual(state["status"],"saved")
            self.assertEqual(set(state["tracks"]),{"system","microphone"})
            self.assertTrue((recorder.folder/"recording.json").is_file())
            output=recorder.mix()
            with sf.SoundFile(output) as stream:
                self.assertGreater(len(stream),0);self.assertEqual(stream.samplerate,16000)
                stream.seek(len(stream)//2);self.assertTrue(len(stream.read(10)))
            self.assertTrue((recorder.folder/"system.wav").is_file())
            with self.assertRaises(FileExistsError): recorder.mix()

    def test_device_failure_is_error_not_success_and_keeps_partial_files(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder=DualRecorder(folder,provider=lambda *args:FakeDevice(fail=True))
            recorder.start(speaker_id="speaker")
            self.assertTrue(recorder.stop_event.wait(2))
            state=recorder.stop()
            self.assertEqual(state["status"],"error");self.assertTrue(state["errors"])
            self.assertTrue((recorder.folder/"recording.json").is_file())
            with self.assertRaises(RuntimeError): recorder.mix()

    def test_silent_system_audio_does_not_claim_complete_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            device=FakeDevice(signal=0)
            recorder=DualRecorder(folder,provider=lambda *args:device)
            recorder.start(speaker_id="speaker")
            self.assertTrue(device.ready.wait(2));recorder.stop()
            with self.assertRaisesRegex(RuntimeError,"未偵測到系統聲音"):recorder.mix()

    def test_failed_metadata_write_does_not_start_audio_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder=DualRecorder(folder,provider=lambda *args:FakeDevice())
            with patch.object(recorder,"_persist",side_effect=OSError("disk full")):
                with self.assertRaises(OSError):recorder.start(speaker_id="speaker")
            self.assertFalse(any(thread.is_alive() for thread in recorder.threads))

    def test_merge_failure_can_retry_and_does_not_publish_partial_output(self):
        with tempfile.TemporaryDirectory() as folder:
            device=FakeDevice()
            recorder=DualRecorder(folder,provider=lambda *args:device)
            recorder.start(speaker_id="speaker");self.assertTrue(device.ready.wait(2));recorder.stop()
            with patch("audioop.ratecv",side_effect=OSError("disk full")):
                with self.assertRaises(OSError):recorder.mix()
            self.assertFalse((recorder.folder/"meeting.flac").exists())
            self.assertTrue((recorder.folder/"system.wav").exists())
            self.assertTrue(recorder.mix().is_file())


if __name__ == "__main__":unittest.main()
