"""Slow backup checks must not block cheap API routes or multiply polling."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx

from tests.http_helpers import ROOT


class DashboardResponsivenessTests(unittest.TestCase):
    def setUp(self):
        from backend import database, main
        self.database, self.main = database, main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(database, 'DB_PATH', self.root / 'meetings.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        database.create_job('dashboard-done')
        database.update_job_status('dashboard-done', 'done', 'complete')

    def check_nonblocking(self, path):
        from backend.backup_status import BackupHealthMonitor
        entered, release = threading.Event(), threading.Event()
        backup = {'ok': True, 'detail': 'test backup', 'age_hours': 0}

        def slow_check(*args, **kwargs):
            entered.set()
            release.wait(1.5)  # Also bounds failure on the old blocking route.
            return backup.copy(), backup.copy()

        async def exercise():
            transport = httpx.ASGITransport(app=self.main.app, client=('127.0.0.1', 123))
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                started = time.perf_counter()
                slow = asyncio.create_task(client.get(path))
                try:
                    did_enter = await asyncio.to_thread(entered.wait, 3)
                    self.assertTrue(did_enter)
                    self.assertLess(time.perf_counter() - started, 1)
                    live, jobs, ready = await asyncio.wait_for(asyncio.gather(
                        client.get('/livez'), client.get('/jobs?limit=20'), client.get('/readyz')
                    ), timeout=1)
                    self.assertFalse(release.is_set())
                    self.assertEqual(live.status_code, 200)
                    self.assertEqual(jobs.status_code, 200)
                    self.assertEqual(jobs.json()['total'], 1)
                    self.assertEqual(ready.status_code, 200)
                    response = await asyncio.wait_for(slow, timeout=1)
                    state = response.json()
                    backup_status = state['runtime']['backup'] if path == '/health' else state['backup']
                    self.assertFalse(backup_status['ok'])
                    self.assertTrue(backup_status['verification_pending'])
                finally:
                    release.set()
                    response = await slow
                self.assertEqual(response.status_code, 200)

        with mock.patch.object(self.main, 'runtime_backup_health', side_effect=slow_check), \
             mock.patch.object(self.main, 'backup_health_monitor', BackupHealthMonitor()), \
             mock.patch.object(self.main, '_worker_readiness_snapshot', return_value={'ready': True}), \
             mock.patch.object(self.main, 'run_startup_health_checks', return_value=[]), \
             mock.patch.object(self.main, 'capacity_health_checks', return_value=[]), \
             mock.patch.object(self.main, '_storage_metrics', return_value=self.main.StorageMetrics(
                 source_media_files=0, source_media_bytes=0,
                 meeting_markdown_files=0, meeting_markdown_bytes=0)):
            asyncio.run(exercise())

    def test_backup_monitor_reuses_verified_result_and_preserves_warning_while_refreshing(self):
        from backend.backup_status import BackupHealthMonitor
        monitor = BackupHealthMonitor()
        paths = (self.root / 'db', self.root / 'backups', None, None, self.root / 'offsite')
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        result = ({'ok': True, 'detail': 'backup OK'}, {'ok': False, 'detail': '缺 6 個來源'})
        def check(*args, **kwargs):
            entered.set()
            release.wait(1)
            completed.set()
            return result
        first = monitor.snapshot(check, paths, max_age_hours=48)
        self.assertTrue(entered.wait(1))
        self.assertTrue(first[0]['verification_pending'])
        self.assertFalse(first[0]['ok'])
        monitor.snapshot(check, paths, max_age_hours=48)
        release.set()
        self.assertTrue(completed.wait(1))
        deadline = time.monotonic() + 1
        while monitor._running and time.monotonic() < deadline:
            time.sleep(0.001)
        verified = monitor.snapshot(check, paths, max_age_hours=48)
        self.assertEqual(verified, result)
        verified[0]['detail'] = 'client mutation'
        self.assertEqual(monitor.snapshot(check, paths, max_age_hours=48), result)
        with mock.patch('backend.backup_status.time.monotonic', return_value=monitor._checked_at + 61):
            pending = monitor.snapshot(check, paths, max_age_hours=48)
        self.assertTrue(pending[1]['verification_pending'])
        self.assertIn('缺 6 個來源', pending[1]['detail'])
        self.assertFalse(pending[0]['ok'])

    def test_backup_monitor_reports_failed_verification_without_claiming_health(self):
        from backend.backup_status import BackupHealthMonitor
        monitor = BackupHealthMonitor()
        check = mock.Mock(side_effect=OSError('fixture failure'))
        paths = (self.root / 'db', self.root / 'backups', None, None, None)
        monitor.snapshot(check, paths, max_age_hours=48)
        deadline = time.monotonic() + 1
        while monitor._running and time.monotonic() < deadline:
            time.sleep(0.001)
        failed = monitor.snapshot(check, paths, max_age_hours=48)
        self.assertFalse(failed[0]['ok'])
        self.assertFalse(failed[0]['verification_pending'])
        self.assertIn('OSError', failed[0]['detail'])
        self.assertEqual(check.call_count, 1)

    def test_slow_metrics_does_not_block_jobs_liveness_or_readiness(self):
        self.check_nonblocking('/metrics')

    def test_slow_health_does_not_block_jobs_liveness_or_readiness(self):
        self.check_nonblocking('/health')

    def test_concurrent_snapshot_reads_verify_once_and_changed_file_invalidates_cache(self):
        from backend import maintenance
        snapshot = self.root / 'snapshot.zip'
        snapshot.write_bytes(b'fixture')
        entered, release = threading.Event(), threading.Event()
        result = {'ok': True, 'path': str(snapshot)}

        def verify(path):
            entered.set()
            release.wait(1)
            maintenance._remember_record_snapshot_verification(path, result)
            return result.copy()

        with mock.patch.object(maintenance, 'verify_record_snapshot', side_effect=verify) as verifier:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(maintenance._cached_record_snapshot_verification, snapshot)
                self.assertTrue(entered.wait(2))
                second = pool.submit(maintenance._cached_record_snapshot_verification, snapshot)
                time.sleep(0.05)
                release.set()
                self.assertEqual(first.result(timeout=2), result)
                self.assertEqual(second.result(timeout=2), result)
            self.assertEqual(verifier.call_count, 1)
            snapshot.write_bytes(b'changed fixture size')
            maintenance._cached_record_snapshot_verification(snapshot)
            self.assertEqual(verifier.call_count, 2)

    def test_dashboard_request_lifecycle_handles_timeouts_retries_and_body_reads(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for dashboard request behavior QC')
        result = subprocess.run([node, str(ROOT / 'tests/dashboard_requests_test.js')],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('dashboard requests OK', result.stdout)

    def test_dashboard_loaders_use_bounded_single_flight_requests(self):
        html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        for marker in ('src="/static/dashboard-requests.js"',
                       "DashboardRequests.begin('jobs', 15000)",
                       "DashboardRequests.begin('metrics', 60000)",
                       "request.json(`${API}/jobs?limit=20`)",
                       "request.json(`${API}/metrics`)",
                       "request.json(`${API}/health`)",
                       'onclick="loadJobs(); loadMetrics()"'):
            self.assertTrue(marker in html, marker)
        self.assertEqual(html.count('request.finish();'), 2)


if __name__ == '__main__':
    unittest.main()
