from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx


class OperationalReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        import backend.database as database

        self.database = database
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.db_patcher = mock.patch.object(
            database,
            "DB_PATH",
            self.root / "meetings.db",
        )
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)
        database.init_db()

    def test_runtime_lease_uses_generation_as_fencing_token(self) -> None:
        database = self.database

        first_generation = database.try_acquire_runtime_lease(
            "queue",
            "worker-a",
            lease_seconds=60,
        )
        self.assertEqual(first_generation, 1)
        self.assertIsNone(
            database.try_acquire_runtime_lease(
                "queue",
                "worker-b",
                lease_seconds=60,
            )
        )

        with database.get_db() as conn:
            conn.execute(
                """UPDATE runtime_leases
                      SET expires_at='2000-01-01 00:00:00'
                    WHERE lease_name='queue'"""
            )

        second_generation = database.try_acquire_runtime_lease(
            "queue",
            "worker-b",
            lease_seconds=60,
        )
        self.assertEqual(second_generation, 2)
        self.assertFalse(
            database.renew_runtime_lease(
                "queue",
                "worker-a",
                first_generation,
                lease_seconds=60,
            )
        )
        self.assertFalse(
            database.release_runtime_lease(
                "queue",
                "worker-a",
                first_generation,
            )
        )

    def test_healthy_job_lease_is_not_requeued_but_expired_job_is(self) -> None:
        database = self.database
        database.create_job(
            "lease-job",
            payload={
                "audio_path": str(self.root / "audio.mp3"),
                "output_dir": str(self.root),
            },
            max_attempts=3,
        )
        claimed = database.claim_next_pending_job(
            worker_id="worker-a",
            worker_generation=4,
            lease_seconds=60,
        )

        self.assertEqual(claimed["worker_id"], "worker-a")
        self.assertEqual(claimed["worker_generation"], 4)
        self.assertEqual(database.requeue_interrupted_jobs(), 0)
        self.assertTrue(
            database.renew_job_lease(
                "lease-job",
                "worker-a",
                4,
                lease_seconds=60,
            )
        )

        with database.get_db() as conn:
            conn.execute(
                """UPDATE jobs
                      SET lease_expires_at='2000-01-01 00:00:00'
                    WHERE job_id='lease-job'"""
            )

        self.assertEqual(database.requeue_interrupted_jobs(), 1)
        recovered = database.get_job("lease-job")
        self.assertEqual(recovered["status"], "pending")
        self.assertIsNone(recovered["worker_id"])
        self.assertIsNone(recovered["lease_expires_at"])

    def test_audio_worker_retries_after_task_records_failed_and_releases_lease(self) -> None:
        import backend.job_queue as job_queue

        audio_path = self.root / "recorded-failure.mp3"
        audio_path.write_bytes(b"audio")
        job_queue.enqueue_audio_job(
            "worker-recorded-failure-job",
            audio_path=audio_path,
            output_dir=self.root,
            model="test-model",
            max_attempts=3,
        )
        worker = job_queue.JobQueueWorker(poll_interval=0.01)
        claim = self.database.claim_next_pending_job()

        def record_failure(**kwargs):
            self.database.update_job_status(
                kwargs["job_id"],
                "failed",
                "暫時失敗",
                error_detail="503 UNAVAILABLE",
            )
            return None

        with mock.patch.object(
            job_queue,
            "process_audio_task",
            side_effect=record_failure,
        ):
            worker.process_job(claim)

        job = self.database.get_job("worker-recorded-failure-job")
        events = self.database.list_job_events("worker-recorded-failure-job")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["attempts"], 1)
        self.assertTrue(
            any(event["event_type"] == "retry_scheduled" for event in events)
        )
        self.assertTrue(audio_path.exists())

    def test_deterministic_quality_failure_has_lower_retry_ceiling(self) -> None:
        database = self.database
        database.create_job(
            "quality-retry-job",
            payload={
                "audio_path": str(self.root / "audio.mp3"),
                "output_dir": str(self.root),
            },
            max_attempts=5,
        )

        for expected_attempt in (1, 2):
            database.claim_next_pending_job()
            status = database.retry_or_fail_job(
                "quality-retry-job",
                "第 1/1 段轉錄不完整：文字密度偏低",
            )
            self.assertEqual(status, "pending")
            self.assertEqual(
                database.get_job("quality-retry-job")["attempts"],
                expected_attempt,
            )
        database.claim_next_pending_job()
        final_status = database.retry_or_fail_job(
            "quality-retry-job",
            "第 1/1 段轉錄不完整：文字密度偏低",
        )

        self.assertEqual(final_status, "failed")
        self.assertEqual(database.get_job("quality-retry-job")["attempts"], 3)

    def test_only_one_embedded_worker_holds_leadership(self) -> None:
        import backend.job_queue as job_queue

        first = job_queue.JobQueueWorker(
            worker_id="embedded-a",
            lease_seconds=60,
            heartbeat_interval=5,
        )
        second = job_queue.JobQueueWorker(
            worker_id="embedded-b",
            lease_seconds=60,
            heartbeat_interval=5,
        )

        first_generation = first._ensure_leadership()
        second_generation = second._ensure_leadership()

        self.assertEqual(first_generation, 1)
        self.assertIsNone(second_generation)
        self.assertTrue(first.is_leader())
        self.assertFalse(second.is_leader())

    def test_dead_local_worker_lease_is_fenced_and_taken_over_immediately(self) -> None:
        import backend.job_queue as job_queue

        dead_owner = f"{socket.gethostname()}:99999999:dead-worker"
        first_generation = self.database.try_acquire_runtime_lease(
            job_queue.WORKER_LEASE_NAME,
            dead_owner,
            lease_seconds=90,
        )
        self.assertEqual(first_generation, 1)
        self.assertFalse(job_queue.local_worker_process_alive(dead_owner))

        successor = job_queue.JobQueueWorker(
            worker_id=f"{socket.gethostname()}:{os.getpid()}:successor",
            lease_seconds=90,
            heartbeat_interval=15,
        )
        second_generation = successor._ensure_leadership()

        self.assertEqual(second_generation, 2)
        self.assertTrue(successor.is_leader())
        lease = self.database.get_runtime_lease(job_queue.WORKER_LEASE_NAME)
        self.assertEqual(lease["owner_id"], successor.worker_id)
        self.assertEqual(lease["generation"], 2)

    def test_meeting_save_and_job_done_share_one_transaction(self) -> None:
        database = self.database
        output_path = self.root / "meeting.md"
        output_path.write_text("# meeting", encoding="utf-8")
        database.create_job(
            "atomic-job",
            payload={
                "audio_path": str(self.root / "audio.mp3"),
                "output_dir": str(self.root),
            },
        )
        database.claim_next_pending_job(
            worker_id="worker-a",
            worker_generation=1,
            lease_seconds=60,
        )

        original_record_event = database._record_job_event
        with mock.patch.object(
            database,
            "_record_job_event",
            side_effect=RuntimeError("fault after meeting insert"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fault after meeting insert"):
                database.save_meeting(
                    title="Atomic",
                    date="2026/07/29",
                    source_audio="audio.mp3",
                    output_path=str(output_path),
                    summary="summary",
                    job_id="atomic-job",
                )

        with database.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM meetings WHERE job_id='atomic-job'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(database.get_job("atomic-job")["status"], "processing")

        with mock.patch.object(
            database,
            "_record_job_event",
            wraps=original_record_event,
        ):
            meeting_id = database.save_meeting(
                title="Atomic",
                date="2026/07/29",
                source_audio="audio.mp3",
                output_path=str(output_path),
                summary="summary",
                job_id="atomic-job",
            )

        job = database.get_job("atomic-job")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["output_path"], str(output_path))
        self.assertIsNone(job["worker_id"])
        self.assertIsNotNone(job["completed_at"])

        duplicate_id = database.save_meeting(
            title="Should not duplicate",
            date="2026/07/30",
            source_audio="other.mp3",
            output_path=str(self.root / "other.md"),
            summary="other",
            job_id="atomic-job",
        )
        self.assertEqual(duplicate_id, meeting_id)
        with database.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM meetings WHERE job_id='atomic-job'"
                ).fetchone()[0],
                1,
            )

    def test_output_path_is_stable_across_attempts(self) -> None:
        from backend.tasks import _meeting_output_path

        audio_path = self.root / "source.webm"
        first = _meeting_output_path(self.root, audio_path, "job:stable/id")
        second = _meeting_output_path(self.root, audio_path, "job:stable/id")

        self.assertEqual(first, second)
        self.assertEqual(
            first.name,
            "meeting_notes_source_job_stable_id.md",
        )

    def test_windows_smoke_server_always_stops_full_process_tree(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "smoke_with_server.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("function Stop-ProcessTree", script)
        self.assertIn("Get-CimInstance Win32_Process", script)
        finally_block = script[script.index("finally {") :]
        self.assertIn(
            "Stop-ProcessTree -RootProcessId $ServerProcess.Id",
            finally_block,
        )

    def test_access_tokens_are_purpose_scoped_and_expire(self) -> None:
        from backend.access_tokens import create_access_token, validate_access_token

        with mock.patch(
            "backend.access_tokens.secrets.token_urlsafe",
            return_value="fixed-nonce",
        ):
            token = create_access_token(
                "secret",
                "bootstrap",
                ttl_seconds=300,
                now=1000,
            )

        self.assertTrue(
            validate_access_token(
                token,
                "secret",
                "bootstrap",
                now=1200,
            )
        )
        self.assertFalse(
            validate_access_token(
                token,
                "secret",
                "session",
                now=1200,
            )
        )
        self.assertFalse(
            validate_access_token(
                token,
                "secret",
                "bootstrap",
                now=1301,
            )
        )

    def test_identity_header_is_rejected_from_untrusted_direct_client(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request

        import backend.auth as auth

        self.database.upsert_app_user(
            "admin@example.com",
            role="admin",
        )
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/users",
            "headers": [(b"x-meeting-user", b"admin@example.com")],
            "client": ("192.168.1.25", 12345),
        })

        with mock.patch.object(auth, "AUTH_FEATURE_ENABLED", True):
            with self.assertRaises(HTTPException) as raised:
                auth.actor_from_request(request)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("可信任反向代理", str(raised.exception.detail))

    def test_loopback_and_api_key_sessions_resolve_persisted_local_admin(self) -> None:
        from starlette.requests import Request

        import backend.auth as auth

        local_email = "local-admin@meeting-assistant.local"
        self.database.upsert_app_user(local_email, role="admin")
        loopback = Request({
            "type": "http",
            "method": "GET",
            "path": "/meetings",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        api_key = Request({
            "type": "http",
            "method": "GET",
            "path": "/meetings",
            "headers": [(b"x-api-key", b"secret")],
            "client": ("203.0.113.10", 12345),
        })

        with mock.patch.object(auth, "AUTH_FEATURE_ENABLED", True), \
                mock.patch.object(auth, "AUTH_LOCAL_SESSION_USER", local_email), \
                mock.patch.object(auth, "AUTH_API_KEY", "secret"):
            local_actor = auth.actor_from_request(loopback)
            remote_actor = auth.actor_from_request(api_key)

        self.assertEqual(local_actor.email, local_email)
        self.assertEqual(local_actor.role, "admin")
        self.assertTrue(local_actor.enabled)
        self.assertEqual(remote_actor.email, local_email)
        self.assertEqual(remote_actor.role, "admin")

    def test_trusted_lan_resolves_separate_editor_identity(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request

        import backend.auth as auth

        lan_email = "meeting-lan-editor@meeting-assistant.local"
        self.database.upsert_app_user(lan_email, role="editor")
        allowed = Request({
            "type": "http",
            "method": "POST",
            "path": "/upload-media",
            "query_string": b"",
            "headers": [],
            "client": ("192.168.20.55", 12345),
        })
        denied = Request({
            "type": "http",
            "method": "GET",
            "path": "/meetings",
            "query_string": b"",
            "headers": [],
            "client": ("192.168.21.55", 12345),
        })

        with mock.patch.object(auth, "AUTH_FEATURE_ENABLED", True), \
                mock.patch.object(auth, "AUTH_TRUST_LOCAL_NETWORK", True), \
                mock.patch.object(
                    auth,
                    "AUTH_TRUSTED_LOCAL_NETWORKS",
                    (auth.ipaddress.ip_network("192.168.20.0/24"),),
                ), \
                mock.patch.object(auth, "AUTH_LAN_SESSION_USER", lan_email), \
                mock.patch.object(auth, "AUTH_API_KEY", "secret"):
            actor = auth.actor_from_request(allowed)
            with self.assertRaises(HTTPException) as raised:
                auth.actor_from_request(denied)

        self.assertEqual(actor.email, lan_email)
        self.assertEqual(actor.role, "editor")
        self.assertTrue(actor.can("meeting:write"))
        self.assertFalse(actor.can("meeting:delete"))
        self.assertEqual(raised.exception.status_code, 401)

    def test_security_headers_and_generic_mutation_audit_are_applied(self) -> None:
        import backend.main as main

        async def exercise():
            transport = httpx.ASGITransport(
                app=main.app,
                client=("127.0.0.1", 32100),
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost",
            ) as client:
                health = await client.get("/health")
                deletion = await client.delete("/jobs/missing-job")
                return health, deletion

        with mock.patch.object(main.auth_module, "AUTH_FEATURE_ENABLED", False), \
                mock.patch.object(main, "OFFSITE_BACKUP_DIR", None), \
                mock.patch.object(main, "BACKUP_DIR", self.root / "backups"):
            health, deletion = asyncio.run(exercise())

        self.assertEqual(
            health.headers.get("x-content-type-options"),
            "nosniff",
        )
        self.assertEqual(health.headers.get("x-frame-options"), "DENY")
        self.assertIn(
            "frame-ancestors 'none'",
            health.headers.get("content-security-policy", ""),
        )
        self.assertTrue(deletion.headers.get("x-request-id"))
        audits = self.database.list_audit_logs()
        self.assertEqual(audits[0]["action"], "http.delete")
        self.assertEqual(audits[0]["resource_id"], "/jobs/missing-job")
        self.assertEqual(audits[0]["detail"]["status_code"], 404)

    def test_schema_v6_archives_orphan_events_and_enforces_foreign_keys(self) -> None:
        database = self.database
        with database.get_db(enforce_foreign_keys=False) as conn:
            conn.execute(
                "UPDATE app_meta SET value='4' WHERE key='schema_version'"
            )
            conn.execute(
                """INSERT INTO job_events (
                       job_id, event_type, message
                   ) VALUES ('missing-job', 'legacy', 'orphan')"""
            )

        backup_root = self.root / "migration-backups"
        with mock.patch.dict(
            os.environ,
            {"MEETING_BACKUP_DIR": str(backup_root)},
        ):
            database.init_db()

        with database.get_db() as conn:
            self.assertEqual(
                int(
                    conn.execute(
                        "SELECT value FROM app_meta WHERE key='schema_version'"
                    ).fetchone()[0]
                ),
                6,
            )
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertIsNotNone(
                conn.execute(
                    """SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name='meeting_confirmation_tasks'"""
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM job_event_archive"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO meeting_revisions (
                           meeting_id, source, content
                       ) VALUES (999999, 'test', 'invalid')"""
                )

        self.assertTrue(
            any((backup_root / "schema_migrations").glob("schema_pre_v4_to_v6_*.db"))
        )

    def test_source_media_is_saved_under_content_addressed_name(self) -> None:
        from backend.source_audio import finalize_source_audio_upload

        content = b"content-addressed-audio"
        digest = hashlib.sha256(content).hexdigest()
        first_temp = self.root / ".upload_first.webm.tmp"
        first_temp.write_bytes(content)
        path, created = finalize_source_audio_upload(
            first_temp,
            self.root / "legacy-name.webm",
            digest,
            len(content),
            {".webm"},
        )

        self.assertTrue(created)
        self.assertEqual(path.name, f"{digest}.webm")
        self.assertEqual(path.read_bytes(), content)

        second_temp = self.root / ".upload_second.webm.tmp"
        second_temp.write_bytes(content)
        duplicate_path, duplicate_created = finalize_source_audio_upload(
            second_temp,
            self.root / "another-name.webm",
            digest,
            len(content),
            {".webm"},
        )
        self.assertEqual(duplicate_path, path)
        self.assertFalse(duplicate_created)
        self.assertFalse(second_temp.exists())

    def test_liveness_readiness_and_queue_metrics_expose_worker_leases(self) -> None:
        import backend.main as main

        self.database.create_job(
            "observable-job",
            payload={
                "audio_path": str(self.root / "audio.webm"),
                "output_dir": str(self.root),
            },
        )
        self.database.claim_next_pending_job(
            worker_id="worker-observable",
            worker_generation=7,
            lease_seconds=60,
        )
        generation = self.database.try_acquire_runtime_lease(
            "meeting-assistant-job-queue",
            "worker-observable",
            lease_seconds=60,
        )
        self.assertEqual(generation, 1)

        async def exercise():
            transport = httpx.ASGITransport(
                app=main.app,
                client=("127.0.0.1", 32101),
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost",
            ) as client:
                return await client.get("/livez"), await client.get("/readyz")

        worker_status = {
            "running": True,
            "leader": True,
            "worker_id": "worker-observable",
            "generation": generation,
            "active_job_id": "observable-job",
            "lease_seconds": 60,
            "heartbeat_seconds": 15,
        }
        with mock.patch.object(
            main.job_worker,
            "status",
            return_value=worker_status,
        ):
            live, ready = asyncio.run(exercise())

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "ok")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")

        metrics = self.database.queue_operational_metrics()
        self.assertEqual(
            metrics["by_task_and_status"]["audio_processing"]["processing"],
            1,
        )
        self.assertEqual(metrics["attempt_distribution"]["1"], 1)
        self.assertEqual(metrics["leader"]["owner_id"], "worker-observable")

    def test_startup_reuses_recent_unchanged_database_and_full_snapshot(self) -> None:
        from backend.maintenance import (
            backup_database,
            create_record_snapshot,
            run_startup_maintenance,
        )

        source_dir = self.root / "source_audio"
        source_dir.mkdir()
        source = source_dir / "source.webm"
        source.write_bytes(b"source-media")
        output = self.root / "meeting.md"
        output.write_text("# meeting", encoding="utf-8")
        self.database.save_meeting(
            "snapshot reuse",
            "2026/07/29",
            source.name,
            str(output),
        )
        backup_dir = self.root / "backups"
        database_backup = backup_database(
            self.database.DB_PATH,
            backup_dir,
        )
        existing_snapshot = create_record_snapshot(
            database_backup,
            backup_dir,
            source_media_dir=source_dir,
        )

        result = run_startup_maintenance(
            self.database.DB_PATH,
            backup_dir,
            source_media_dir=source_dir,
            backup_min_interval_hours=168,
        )

        self.assertFalse(result["backup_created"])
        self.assertTrue(result["snapshot_reused"])
        self.assertEqual(Path(result["snapshot_path"]), existing_snapshot)
        self.assertEqual(Path(result["backup_path"]), database_backup)


if __name__ == "__main__":
    unittest.main()
