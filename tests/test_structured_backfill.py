from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import database
from backend.confirmation_queue import (
    apply_structured_minutes_backfill,
    list_confirmation_tasks,
    update_confirmation_task,
)
from backend.structured_minutes import parse_standardized_minutes


MINUTES = """# 測試

## 一、討論摘要 (Discussion Summary)

### D1. 驗證主題
- 摘要：討論一項可驗證內容
- 背景：既有問題
- 重點：需要測試
- 影響/風險：延誤風險
- 待釐清：未提及
- 佐證時間：00:10

## 二、最終決議 (Final Decisions)

| ID | 關聯討論 | 決議 | 依據 | 狀態 |
| --- | --- | --- | --- | --- |
| R1 | D1 | 採用方案 | 會議明確同意<br>佐證：00:20 | confirmed |

## 三、待辦事項 (Action Items)

| ID | 關聯討論 | 關聯決議 | 任務 | 負責人 | 期限 | 優先級 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | D1 | R1 | 完成測試<br>佐證：00:30 | 未提及 | 未提及 | 高 |

## 📝 四、完整逐字稿 (Verbatim Transcript)
"""


class StructuredBackfillTests(unittest.TestCase):
    def test_parser_preserves_ids_relations_evidence_and_missing_fields(self) -> None:
        parsed = parse_standardized_minutes(MINUTES)

        self.assertEqual(parsed["parse_errors"], [])
        self.assertEqual(parsed["discussion_summary"][0]["id"], "D1")
        self.assertEqual(
            parsed["final_decisions"][0]["evidence_timecodes"],
            ["00:20"],
        )
        action = parsed["action_items"][0]
        self.assertEqual(action["related_decisions"], ["R1"])
        self.assertEqual(action["source_timecodes"], ["00:30"])
        self.assertEqual(action["confirmation_required"], ["owner", "due"])

    def test_apply_builds_confirmation_queue_and_resolution_updates_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "meeting.md"
            output.write_text(MINUTES, encoding="utf-8")
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                database.init_db()
                database.upsert_app_user(
                    email="admin@example.com",
                    display_name="Admin",
                    role="admin",
                    is_active=True,
                )
                meeting_id = database.save_meeting(
                    title="backfill",
                    date="2026/07/30",
                    source_audio="source.webm",
                    output_path=str(output),
                )
                result = apply_structured_minutes_backfill(
                    [(meeting_id, parse_standardized_minutes(MINUTES))],
                    actor_email="admin@example.com",
                )
                tasks = list_confirmation_tasks(meeting_id=meeting_id)

                self.assertEqual(result["applied_meetings"], 1)
                self.assertEqual(result["items_created"], 3)
                self.assertEqual(result["confirmation_tasks_created"], 2)
                self.assertEqual(len(tasks), 2)
                owner_task = next(
                    task for task in tasks if task["field_name"] == "owner"
                )
                update_confirmation_task(
                    int(owner_task["id"]),
                    status="resolved",
                    resolution_value="王小明",
                    resolution_note="會後確認",
                    actor_email="admin@example.com",
                )
                detail = database.get_meeting(meeting_id)

            self.assertEqual(
                detail["structured_summary"]["action_items"][0]["owner"],
                "王小明",
            )
            item = next(
                record
                for record in detail["structured_items"]
                if record["item_key"] == "A1"
            )
            self.assertEqual(item["payload"]["owner"], "王小明")
            self.assertEqual(
                list_confirmation_tasks.__module__,
                "backend.confirmation_queue",
            )

    def test_new_generated_minutes_enqueue_missing_native_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            structured = {
                "discussion_summary": [],
                "final_decisions": [],
                "action_items": [
                    {
                        "id": "A1",
                        "task": "完成測試",
                        "owner": "未提及",
                        "due": "待確認",
                        "source_timecodes": [],
                    }
                ],
            }
            with mock.patch.object(database, "DB_PATH", root / "meetings.db"):
                database.init_db()
                meeting_id = database.save_meeting(
                    title="native",
                    date="2026/07/30",
                    source_audio="source.webm",
                    output_path=str(root / "meeting.md"),
                    structured_summary=structured,
                )
                tasks = list_confirmation_tasks(meeting_id=meeting_id)

            self.assertEqual(
                {task["field_name"] for task in tasks},
                {"owner", "due", "source_timecodes"},
            )


if __name__ == "__main__":
    unittest.main()
