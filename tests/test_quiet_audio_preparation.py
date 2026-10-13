import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import tasks
from backend.audio_preparation import needs_initial_speech_focus


class QuietAudioPreparationTests(unittest.TestCase):
    def focus(self, average, peak, active):
        return needs_initial_speech_focus(
            average, peak, active, clip_dbfs=-0.1,
            min_dynamic_range_db=14, min_active_ratio=0.55,
        )

    def test_quiet_uneven_source_does_not_require_clipping(self):
        self.assertTrue(self.focus(-41.2, -6.0, 0.60))
        self.assertTrue(self.focus(-32, -12, 0.20))

    def test_normal_quiet_even_noise_and_sparse_sources_stay_unchanged(self):
        for values in [(-24.5, -0.2, 0.64), (-22, -2, 0.7),
                       (-40, -37, 0.99), (-60, -41, 0.5),
                       (-41, -6, 0.19), (-31.9, -10, 0.8),
                       (-40, -20.1, 0.8)]:
            with self.subTest(values=values):
                self.assertFalse(self.focus(*values))

    def test_existing_clipped_dense_route_is_preserved(self):
        self.assertTrue(self.focus(-22.4, 0, 0.64))
        self.assertFalse(self.focus(-22.4, 0, 0.10))

    def test_nonfinite_levels_do_not_trigger_gain(self):
        for values in [(float('-inf'), -6, 0.6), (-40, float('nan'), 0.6),
                       (-40, -6, float('inf'))]:
            self.assertFalse(self.focus(*values))

    def preflight(self, root, *, focus_result, enabled=True, initial=True):
        audio = mock.MagicMock()
        audio.channels, audio.frame_rate = 1, 48000
        audio.dBFS, audio.max_dBFS = -41.2, -6.0
        audio.__len__.return_value = 825840
        source = root / 'original.webm'
        source.write_bytes(b'original media must remain untouched')
        with mock.patch('pydub.AudioSegment.from_file', return_value=audio), \
             mock.patch('pydub.silence.detect_silence', return_value=[(0, 300000)]), \
             mock.patch.object(tasks, 'AUDIO_PREPROCESSING_ENABLED', enabled), \
             mock.patch.object(tasks, 'AUDIO_INITIAL_SPEECH_FOCUS_ENABLED', initial), \
             mock.patch.object(tasks, '_create_speech_focus_audio', return_value=focus_result) as focus, \
             mock.patch('pydub.effects.normalize', return_value=audio):
            result = tasks._prepare_audio_for_transcription(source, root / 'temp', 'quiet')
        self.assertEqual(source.read_bytes(), b'original media must remain untouched')
        return result, focus

    def test_quiet_source_uses_lossless_copy_and_honest_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'focus.flac'
            (prepared, report), focus = self.preflight(root, focus_result=path)
            self.assertEqual(prepared, path)
            self.assertEqual(report['preprocessing_mode'], 'speech_focus')
            self.assertIn('低音量', report['warnings'][0])
            self.assertIn('回聽確認', report['warnings'][0])
            focus.assert_called_once()

    def test_filter_failure_retains_original_normalization_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            (prepared, report), focus = self.preflight(Path(tmp), focus_result=None)
            self.assertEqual(report['preprocessing_mode'], 'normalize_lossless')
            self.assertEqual(prepared.suffix, '.flac')
            focus.assert_called_once()

    def test_preprocessing_switches_remain_effective(self):
        for enabled, initial in [(False, True), (True, False)]:
            with self.subTest(enabled=enabled, initial=initial), tempfile.TemporaryDirectory() as tmp:
                (_, report), focus = self.preflight(
                    Path(tmp), focus_result=Path(tmp)/'focus.flac', enabled=enabled, initial=initial,
                )
                focus.assert_not_called()
                self.assertEqual(report['preprocessing_mode'], 'normalize_lossless' if enabled else 'original')

    def test_recovery_plan_cannot_reuse_previous_audio_preparation(self):
        params = dict(model='gemini-test', source_audio_sha256='abc', segment_index=0,
                      total_segments=1, start_seconds=0, end_seconds=275)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(tasks, 'AUDIO_PREPROCESSING_VERSION', 8):
                old = tasks._segment_recovery_plan_context(**params)
            tasks._save_segment_recovery_plan(root, old, chunk_seconds=30, reason='old audio')
            current = tasks._segment_recovery_plan_context(**params)
            self.assertNotEqual(old, current)
            self.assertIsNone(tasks._load_segment_recovery_plan(root, current))
            self.assertEqual(tasks._load_segment_recovery_plan(root, old), 30)
            self.assertTrue(tasks._segment_recovery_plan_file(root, old).is_file())


if __name__ == '__main__':
    unittest.main()
