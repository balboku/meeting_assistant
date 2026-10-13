"""Read-only backup verification, separate from foreground dashboard requests."""
from copy import deepcopy
import threading
import time


class BackupHealthMonitor:
    def __init__(self, refresh_seconds=60):
        self._lock = threading.Lock()
        self._refresh_seconds = refresh_seconds
        self._key = None
        self._result = None
        self._checked_at = 0
        self._running = False

    def snapshot(self, check, paths, *, max_age_hours):
        key = (check, paths, max_age_hours)
        launch = False
        with self._lock:
            if key != self._key:
                # At most one verifier per process, even during config changes.
                if self._running:
                    return self._pending(None, paths[-1] is not None)
                self._key, self._result = key, None
            if self._result is not None and time.monotonic() - self._checked_at < self._refresh_seconds:
                return deepcopy(self._result)
            if not self._running:
                self._running = launch = True
            pending = self._pending(self._result, paths[-1] is not None)
        if launch:
            # Only reads immutable backups. A stalled remote disk cannot hold
            # API thread-pool slots or delay process shutdown.
            threading.Thread(target=self._refresh, args=(check, paths, max_age_hours),
                             name='backup-health-verifier', daemon=True).start()
        return pending

    def _refresh(self, check, paths, max_age_hours):
        try:
            result = check(*paths, max_age_hours=max_age_hours)
        except Exception as error:
            result = tuple({'ok': False, 'verification_pending': False,
                            'detail': f'備份驗證失敗：{type(error).__name__}'} for _ in range(2))
        with self._lock:
            self._result = deepcopy(result)
            self._checked_at = time.monotonic()
            self._running = False

    @staticmethod
    def _pending(previous, configured):
        results = deepcopy(previous) if previous is not None else ({}, {})
        for index, status in enumerate(results):
            # Never turn unchecked/stale backup evidence into a healthy signal.
            status['ok'] = False
            status['verification_pending'] = True
            status['configured'] = index == 0 or configured
            detail = str(status.get('detail') or '').strip()
            status['detail'] = f'{detail}；' if detail else ''
            status['detail'] += '完整驗證進行中；可繼續使用會議功能'
        return results
