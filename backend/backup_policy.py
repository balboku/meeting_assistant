"""Separate large local ZIP retention from SQLite and offsite history."""
from __future__ import annotations

import os
from typing import Mapping


def local_snapshot_keep(legacy_keep: int, env: Mapping[str, str] | None = None) -> int:
    environment = os.environ if env is None else env
    raw = str(environment.get("MEETING_LOCAL_SNAPSHOT_KEEP") or "").strip()
    if not raw:
        return legacy_keep
    try:
        return max(2, int(raw))
    except ValueError:
        return max(2, legacy_keep)
