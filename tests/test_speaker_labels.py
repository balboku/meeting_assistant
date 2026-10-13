"""Speaker labels are transcript metadata, not proof of audio diarization."""
from copy import deepcopy
import unittest
from unittest import mock

from backend.transcript_diagnostics import transcript_speaker_labels, transcript_turns


class SpeakerLabelTests(unittest.TestCase):
    def test_mixed_markdown_plain_and_inline_labels(self):
        transcript = "[00:00] **[發言者 A]**：開始。 [00:12] [發言者 B]：回答。\n[00:20] *[發言者 C]*: next."
        self.assertEqual(transcript_speaker_labels(transcript), ["發言者 A", "發言者 B", "發言者 C"])

    def test_names_quoted_in_speech_and_emphasis_are_not_labels(self):
        transcript = "[00:00] [A]：引用 **[產品]**，舉例 [B]：不要計入。\n普通文字 **[C]**。"
        self.assertEqual(transcript_speaker_labels(transcript), ["A"])

    def test_unknown_overlapping_and_uncertain_are_not_extra_people(self):
        transcript = "[00:00] [發言者不明]：[聽不清]\n[00:20] [多人重疊]：混合語音\n[00:30] [聽不清]：雜訊"
        self.assertEqual(transcript_speaker_labels(transcript), [])

    def test_missing_label_is_not_inherited_or_invented(self):
        self.assertEqual(transcript_speaker_labels("[00:00] [A]：开始\n[00:20] 沒有標籤\n[00:40] 內容"), ["A"])
        self.assertEqual(transcript_speaker_labels("[00:00] 沒有標籤"), [])

    def test_hours_and_line_level_legacy_labels(self):
        self.assertEqual(transcript_speaker_labels("[01:02:03] [Speaker 2]: hello\n**[A]**：legacy"), ["A", "Speaker 2"])

    def test_explicit_turn_parser_contract_is_preserved(self):
        transcript = "[01:02] **[A]**：hello\n[01:04] [B]: yes\n[01:06] 未知"
        self.assertEqual(transcript_turns(transcript), [(62, "A", "hello"), (64, "B", "yes")])

    def test_quality_report_counts_plain_labels_without_changing_blocking_status(self):
        from backend.tasks import _build_quality_report
        report = _build_quality_report({"warnings": []}, [], "[00:00] **[A]**：報告。\n[00:20] [B]：回應。")
        self.assertEqual(report["speaker_labels"], ["A", "B"])
        self.assertEqual(report["blocking_segment_indices"], [])

    def test_detail_recomputes_stale_labels_without_writing_database(self):
        from backend import main
        from tests.http_helpers import asgi_request
        from tests.test_minutes_view import record
        r = record()
        r["full_content"] = r["full_content"].replace("\n## 英文中譯對照", "\n[00:40] [B]：追加回應。\n\n## 英文中譯對照")
        r["full_content"] += "\n[00:50] [C]：只在翻譯附錄出現。\n"
        r["quality_report"] = {"score": 100, "label": "良好", "speaker_labels": ["A"],
                               "recheck": {"method": "local_transcript_only", "version": main.TRANSCRIPT_QUALITY_RECHECK_VERSION}}
        before = deepcopy(r)
        with mock.patch.object(main, "get_meeting", return_value=r), mock.patch.object(main, "update_meeting_quality_report") as write:
            response = asgi_request(main.app, "GET", "/meetings/1")
        self.assertEqual(response.status_code, 200, response.text)
        # The translation appendix is not part of the source transcript.
        self.assertEqual(response.json()["quality_report"]["speaker_labels"], ["A", "B"])
        self.assertEqual(r, before)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
