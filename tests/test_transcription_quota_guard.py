from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from backend import database, retry_policy, tasks
from backend.transcription_budget import (
    ACTIVE_TRANSCRIPTION_BUDGET,
    TranscriptionRequestBudget,
    TranscriptionRequestBudgetExceeded,
)


DAILY_429 = (
    "429 RESOURCE_EXHAUSTED: quotaId: "
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue: 500"
)


class TranscriptionQuotaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(database, "DB_PATH", Path(temporary.name) / "test.db")
        patch.start()
        self.addCleanup(patch.stop)
        database.init_db()

    def _claim_job(self, job_id: str, *, max_attempts: int = 5) -> None:
        database.create_job(job_id, max_attempts=max_attempts)
        claimed = database.claim_next_pending_job()
        self.assertEqual(claimed["job_id"], job_id)

    def test_only_daily_quota_id_triggers_daily_deferral(self) -> None:
        self.assertTrue(retry_policy.is_daily_quota_error(DAILY_429))
        self.assertFalse(retry_policy.is_daily_quota_error("429 RESOURCE_EXHAUSTED: RPM limit"))
        self.assertFalse(retry_policy.is_daily_quota_error("503 UNAVAILABLE"))

    def test_pacific_reset_handles_dst_transition_midnights(self) -> None:
        reset = retry_policy.next_pacific_daily_reset_utc
        self.assertEqual(
            reset(datetime(2026, 3, 8, 7, tzinfo=timezone.utc)),
            datetime(2026, 3, 8, 8, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            reset(datetime(2026, 3, 8, 11, tzinfo=timezone.utc)),
            datetime(2026, 3, 9, 7, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            reset(datetime(2026, 11, 1, 6, tzinfo=timezone.utc)),
            datetime(2026, 11, 1, 7, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            reset(datetime(2026, 11, 1, 10, tzinfo=timezone.utc)),
            datetime(2026, 11, 2, 8, 5, tzinfo=timezone.utc),
        )

    def test_budget_bounds_segment_and_whole_attempt(self) -> None:
        budget = TranscriptionRequestBudget("job", segment_limit=2, job_limit=3)
        budget.reserve(0)
        budget.reserve(0)
        with self.assertRaises(TranscriptionRequestBudgetExceeded):
            budget.reserve(0)
        budget.reserve(1)
        with self.assertRaises(TranscriptionRequestBudgetExceeded):
            budget.reserve(2)
        self.assertEqual(budget.total_requests, 3)

    def test_fallback_is_counted_and_not_sent_after_budget_exhaustion(self) -> None:
        budget = TranscriptionRequestBudget("job", segment_limit=1, job_limit=2)
        client = mock.Mock()
        client.models.generate_content.side_effect = RuntimeError("503 UNAVAILABLE")
        token = ACTIVE_TRANSCRIPTION_BUDGET.set(budget)
        try:
            with mock.patch.object(tasks, "_resolve_transcription_recovery_model", return_value="fallback"), \
                 mock.patch.object(tasks, "update_job_status") as update:
                with self.assertRaises(TranscriptionRequestBudgetExceeded):
                    tasks._generate_transcript_with_transient_fallback(
                        client, model="primary", contents=[], config=mock.Mock(),
                        job_id="job", segment_index=0, total_segments=1,
                    )
                update.assert_not_called()
        finally:
            ACTIVE_TRANSCRIPTION_BUDGET.reset(token)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_daily_quota_wait_refunds_attempt_without_marking_done(self) -> None:
        self._claim_job("daily")
        self.assertEqual(database.retry_or_fail_job("daily", DAILY_429), "pending")
        job = database.get_job("daily")
        self.assertEqual(job["attempts"], 0)
        self.assertIsNone(job["completed_at"])
        self.assertIn("後續跑", job["message"])
        self.assertIsNone(database.claim_next_pending_job())

    def test_daily_quota_never_becomes_failure_only_due_to_wait_count(self) -> None:
        self._claim_job("repeated")
        for _ in range(8):
            self.assertEqual(database.retry_or_fail_job("repeated", DAILY_429), "pending")
            with database.get_db() as conn:
                conn.execute("UPDATE jobs SET queued_at='2000-01-01 00:00:00' WHERE job_id='repeated'")
            self.assertEqual(database.claim_next_pending_job()["job_id"], "repeated")
        self.assertEqual(database.retry_or_fail_job("repeated", DAILY_429), "pending")
        job = database.get_job("repeated")
        self.assertEqual(job["attempts"], 0)
        self.assertIn("額度恢復", job["message"])
        self.assertNotEqual(job["status"], "done")

    def test_budget_waits_once_then_requires_review(self) -> None:
        self._claim_job("budget")
        detail = "TRANSCRIPTION_REQUEST_BUDGET_EXHAUSTED: segment=1"
        self.assertEqual(database.retry_or_fail_job("budget", detail), "pending")
        with database.get_db() as conn:
            conn.execute("UPDATE jobs SET queued_at='2000-01-01 00:00:00' WHERE job_id='budget'")
        database.claim_next_pending_job()
        self.assertEqual(database.retry_or_fail_job("budget", detail), "failed")
        self.assertIn("需人工複核", database.get_job("budget")["message"])

    def test_recovery_no_progress_requires_review_without_deferral(self) -> None:
        self._claim_job("no-progress")
        detail = "TRANSCRIPTION_RECOVERY_NO_PROGRESS: 第 1 段連續 3 個視窗無可用語句"
        self.assertEqual(database.retry_or_fail_job("no-progress", detail), "failed")
        job = database.get_job("no-progress")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 1)
        self.assertIn("需人工核聽", job["message"])
        with database.get_db() as conn:
            event_types = [row[0] for row in conn.execute(
                "SELECT event_type FROM job_events WHERE job_id=?", ("no-progress",)
            )]
        self.assertIn("manual_review_required", event_types)
        self.assertNotIn("transcription_budget_deferred", event_types)

    def test_short_429_refunds_attempt_and_waits_instead_of_failing(self) -> None:
        self._claim_job("rpm")
        self.assertEqual(database.retry_or_fail_job("rpm", "429 RPM limit"), "pending")
        job = database.get_job("rpm")
        self.assertEqual(job["attempts"], 0)
        self.assertIn("額度恢復", job["message"])


if __name__ == "__main__":
    unittest.main()
