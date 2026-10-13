from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import tasks, transcript_audio_quality as quality


class MultilingualDensityTests(unittest.TestCase):
    def _issues(self, text: str, active_ms: int) -> list[str]:
        class Audio:
            dBFS = -25.0

            def __len__(self):
                return 45_000

            def __getitem__(self, _slice):
                return self

        with mock.patch('pydub.AudioSegment.from_file', return_value=Audio()), \
             mock.patch('pydub.silence.detect_nonsilent', return_value=[(0, active_ms)] if active_ms else []), \
             mock.patch.object(quality, 'TRANSCRIPT_SPEECH_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND', 8), \
             mock.patch.object(quality, 'TRANSCRIPT_SPEECH_DENSITY_MAX_WORDS_PER_ACTIVE_SECOND', 8):
            return quality.speech_backed_transcript_excess_density_quality_issues(
                Path('window.wav'), '[00:00] **[發言者 A]**：' + text,
                expected_start_seconds=0, expected_end_seconds=45,
            )

    def test_clear_english_is_counted_as_words_instead_of_letters(self):
        # Like the affected 45-second window: high Latin character count,
        # about 185 words, and 32 seconds detected above the energy threshold.
        english = ' '.join(['regulatory requirements presentation community insights'] * 37)
        self.assertGreater(quality._spoken_character_count(english) / 32, 8)
        self.assertEqual(self._issues(english, 32_000), [])

    def test_mixed_speech_uses_combined_language_duration(self):
        self.assertEqual(self._issues('中' * 40 + ' regulation' * 40, 10_000), [])
        self.assertTrue(self._issues('中' * 80 + ' regulation' * 80, 10_000))

    def test_chinese_excess_and_english_excess_remain_blocked(self):
        self.assertTrue(self._issues('不可能的過量文字' * 30, 5_000))
        self.assertTrue(self._issues(' regulation' * 185, 5_000))

    def test_silent_audio_does_not_accept_an_english_sentence(self):
        self.assertTrue(self._issues('Thank you everyone.', 0))

    def test_labels_timestamps_uncertainty_and_apostrophes_are_not_extra_words(self):
        text = "[02:15] **[發言者 A]**：I'm reviewing MDR today. 中文 123 [聽不清]"
        self.assertEqual(quality._spoken_density_counts(text), (5, 4))


class UncertaintyCacheTests(unittest.TestCase):
    def test_old_uncertainty_cache_is_quarantined_and_normal_cache_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = {'total_segments': 1, 'segment_bounds': [[0, 45]]}
            bad = tasks._segment_cache_file(root, 'old', 0)
            bad.parent.mkdir(parents=True)
            bad.write_text(json.dumps({
                **context, 'segment_index': 0,
                'transcript': '[00:00] **[發言者不明]**：[聽不清]',
            }), encoding='utf-8')
            good = tasks._save_segment_transcript_cache(
                root, 'good', 0, context, '[00:00] **[發言者 A]**：Valid English content.'
            )
            with mock.patch.object(tasks, '_segment_cache_matches', return_value=True):
                self.assertIsNone(tasks._load_segment_transcript_cache(root, 'old', 0, context))
                self.assertIn('Valid English', tasks._load_segment_transcript_cache(root, 'good', 0, context))
            self.assertTrue(good.is_file())
            self.assertFalse(bad.exists())
            self.assertTrue(list((root / 'segment_cache_quarantine').rglob('segment_001.json')))

    def test_placeholder_is_not_saved_as_a_successful_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = tasks._save_segment_transcript_cache(
                root, 'new', 0, {'total_segments': 1},
                '[00:00] **[發言者不明]**：[聽不清]',
            )
            self.assertIsNone(result)
            self.assertFalse(tasks._segment_cache_file(root, 'new', 0).exists())


if __name__ == '__main__':
    unittest.main()
