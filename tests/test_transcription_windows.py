from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock
import wave

from backend.transcription_windows import GapPlan, Window, WindowMismatch, cut_pcm, run_gap_plan


class TranscriptionWindowTests(unittest.TestCase):
    def test_prior_overlapping_route_becomes_only_the_uncovered_ranges(self):
        plan = GapPlan.build(Window(594, 743),
                             [Window(594, 628), Window(620, 635), Window(620, 642),
                              Window(648, 670), Window(662, 678)], max_frames=100)
        self.assertEqual(plan.protected, (Window(594, 642), Window(648, 678)))
        self.assertEqual(plan.missing, (Window(642, 648), Window(678, 743)))
        with self.assertRaises(WindowMismatch):
            plan.check_request(Window(620, 656))
        with self.assertRaises(WindowMismatch):
            plan.check_request(Window(594, 743))

    def test_caches_are_required_and_must_fit_the_source(self):
        for cached in ([], [Window(1, 101)]):
            with self.assertRaises(WindowMismatch):
                GapPlan.build(Window(0, 100), cached, max_frames=10)

    def test_constructed_plan_cannot_overlap_or_escape_its_audio_extent(self):
        for missing in [(Window(39,100),), (Window(40,101),), (Window(41,100),),
                        (Window(40,80),Window(79,100))]:
            with self.assertRaises(WindowMismatch):
                GapPlan(Window(0,100), (Window(0,40),), missing)

    def test_adjacent_verified_windows_leave_nothing_to_send(self):
        plan = GapPlan.build(Window(0, 100), [Window(0, 25), Window(25, 100)], max_frames=20)
        self.assertEqual(plan.missing, ())
        transcribe = Mock()
        run_gap_plan(plan, (), revalidate=Mock(), transcribe=transcribe, accept=Mock())
        transcribe.assert_not_called()

    def prepare(self, directory):
        source = Path(directory)/'source.wav'
        with wave.open(str(source),'wb') as audio:
            audio.setparams((1,2,8000,0,'NONE','not compressed'))
            audio.writeframes(struct.pack('<100h',*range(100)))
        plan = GapPlan.build(Window(0, 100), [Window(0, 40), Window(48, 78)], max_frames=12)
        clips = [cut_pcm(source, window, Path(directory)/f'gap-{i}.wav', rate=8000)
                 for i, window in enumerate(plan.missing)]
        return source, plan, clips

    def test_outbound_pcm_contains_exactly_the_missing_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            source, plan, clips = self.prepare(directory)
            before = source.read_bytes()
            self.assertEqual(plan.missing, (Window(40,48),Window(78,89),Window(89,100)))
            sent = []
            def send(clip):
                with wave.open(str(clip.path),'rb') as audio:
                    sent.extend(v[0] for v in struct.iter_unpack('<h', audio.readframes(audio.getnframes())))
                return 'validated by fixture'
            accepted = Mock()
            run_gap_plan(plan, clips, revalidate=Mock(), transcribe=send, accept=accepted)
            self.assertEqual(sent, [*range(40,48),*range(78,100)])
            self.assertEqual(accepted.call_count,3)
            self.assertEqual(source.read_bytes(),before)

    def test_tampered_late_clip_stops_entire_batch_before_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            _, plan, clips = self.prepare(directory)
            with clips[-1].path.open('ab') as changed:
                changed.write(b'changed')
            transcribe = Mock()
            with self.assertRaises(WindowMismatch):
                run_gap_plan(plan, clips, revalidate=Mock(), transcribe=transcribe, accept=Mock())
            transcribe.assert_not_called()

    def test_runtime_identity_mismatch_stops_before_next_request(self):
        with tempfile.TemporaryDirectory() as directory:
            _, plan, clips = self.prepare(directory)
            revalidate = Mock(side_effect=[None,None,WindowMismatch('cache changed')])
            transcribe = Mock(return_value='result')
            with self.assertRaisesRegex(WindowMismatch,'cache changed'):
                run_gap_plan(plan, clips, revalidate=revalidate, transcribe=transcribe, accept=Mock())
            transcribe.assert_called_once_with(clips[0])

    def test_duplicate_missing_clip_is_rejected_without_any_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            _, plan, clips = self.prepare(directory)
            transcribe = Mock()
            with self.assertRaises(WindowMismatch):
                run_gap_plan(plan, [clips[0], clips[0], clips[-1]], revalidate=Mock(), transcribe=transcribe, accept=Mock())
            transcribe.assert_not_called()

    def test_failed_quality_gate_does_not_resend_or_widen_the_window(self):
        with tempfile.TemporaryDirectory() as directory:
            _, plan, clips = self.prepare(directory)
            transcribe = Mock(return_value='unusable')
            accept = Mock(side_effect=ValueError('unusable result'))
            with self.assertRaisesRegex(ValueError,'unusable result'):
                run_gap_plan(plan, clips, revalidate=Mock(), transcribe=transcribe, accept=accept)
            transcribe.assert_called_once_with(clips[0])

    def test_resume_never_dispatches_a_window_recorded_as_already_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, plan, clips = self.prepare(directory)
            transcribe = Mock()
            run_gap_plan(plan, clips, revalidate=Mock(), transcribe=transcribe, accept=Mock(),
                         previously_dispatched=[clips[0].window,clips[-1].window])
            transcribe.assert_called_once_with(clips[1])
            transcribe.reset_mock()
            with self.assertRaises(WindowMismatch):
                run_gap_plan(plan, clips, revalidate=Mock(), transcribe=transcribe, accept=Mock(),
                             previously_dispatched=[Window(39,48)])
            transcribe.assert_not_called()
