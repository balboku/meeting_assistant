from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydub import AudioSegment

from backend import tasks


class RecoveryBoundingTests(unittest.TestCase):
    def test_recursive_subsegments_shrink_and_use_bounded_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current = root / ("very_long_source_name_" * 8 + ".wav")
            export_handle = AudioSegment.silent(duration=12_000).export(
                current,
                format="wav",
            )
            export_handle.close()
            previous_duration = 12

            for _ in range(10):
                segments = tasks._split_audio_to_subsegments(
                    current,
                    chunk_seconds=5,
                )
                self.assertTrue(all(len(path.name) < 80 for path, _, _ in segments))
                if len(segments) <= 1:
                    break
                current, start, end = max(
                    segments,
                    key=lambda item: item[2] - item[1],
                )
                duration = end - start
                self.assertLess(duration, previous_duration)
                previous_duration = duration
            else:
                self.fail("recovery splitting did not converge")

    def test_recovery_depth_guard_fails_before_another_model_call(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "安全上限"):
            tasks._transcribe_segment_with_recovery(
                object(),
                Path("segment.wav"),
                0,
                1,
                "job",
                "model",
                offset_seconds=0,
                duration_seconds=60,
                is_last_segment=True,
                recovery_depth=tasks.TRANSCRIPT_RECOVERY_MAX_DEPTH,
            )


if __name__ == "__main__":
    unittest.main()
