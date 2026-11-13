"""Independent, same-frame source/value checks without network or local data."""
from unittest import TestCase, mock

from backend.visual_grounding import grounded_notes, note_has_frame_support


class VisualGroundingTests(TestCase):
    def test_full_values_not_substrings_and_title_must_match(self):
        note = {"source": "強度計算", "observed": "垂直 22.452 kgf/cm²，側面 131.706 kgf/cm²"}
        self.assertTrue(note_has_frame_support(note, "強 度 計 算\n22. 452 kgf/cm2\n131.706 kgf/cm2"))
        for text in ("強度計算 122.452 131.706", "強度計算 22.452 131706", "強度計算 −22.452 131.706", "別的表 22.452 131.706",
                     "強度計算 22.452 131.706 正在等待其他人加入"):
            self.assertFalse(note_has_frame_support(note, text), text)

    def test_text_only_claim_must_be_visible_verbatim(self):
        note = {"source": "試驗計畫", "observed": "PVT 試組"}
        self.assertTrue(note_has_frame_support(note, "試驗計畫\nPVT 試 組"))
        self.assertFalse(note_has_frame_support(note, "試驗計畫\nDVT 試組"))

    def test_other_frame_cannot_supply_missing_evidence(self):
        note = {"source": "驗收表", "observed": "90%", "frame_id": "F1"}
        other = {"source": "進度表", "observed": "40%", "frame_id": "F2"}
        frames = {"F1": b"wrong", "F2": b"right", "F3": b"uncited"}
        with mock.patch("backend.visual_grounding.read_frame_text", return_value={
                "F1": "驗收表 80%", "F2": "驗收表 90%\n進度表 40%"}) as ocr:
            self.assertEqual(grounded_notes({"D1": [note, other]}, frames), {"D1": [other]})
        ocr.assert_called_once_with({"F1": b"wrong", "F2": b"right"})
