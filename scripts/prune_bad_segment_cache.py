#!/usr/bin/env python3
"""Find and optionally quarantine unsafe segment transcript cache files.

The task runner already rejects unsafe cache entries when it tries to load
them. This script moves old unsafe cache files to a recoverable quarantine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import tasks  # noqa: E402


DEFAULT_CACHE_DIR = ROOT / "output" / tasks.SEGMENT_CACHE_DIRNAME
DEFAULT_QUARANTINE_DIR = ROOT / "output" / "segment_cache_quarantine"


def _load_payload(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"快取 JSON 無法讀取：{exc}"]
    if not isinstance(payload, dict):
        return None, ["快取 JSON 格式不是物件"]
    return payload, []


def _segment_index_from_path(path: Path) -> int:
    try:
        return max(0, int(path.stem.split("_")[-1]) - 1)
    except (TypeError, ValueError):
        return 0


def _cache_quality_issues(path: Path, payload: dict[str, Any]) -> list[str]:
    transcript = payload.get("transcript")
    if not isinstance(transcript, str):
        return ["快取缺少逐字稿內容"]
    try:
        segment_index = int(payload.get("segment_index"))
    except (TypeError, ValueError):
        segment_index = _segment_index_from_path(path)
    return tasks._segment_cache_quality_issues(
        transcript=transcript,
        segment_index=segment_index,
        context=payload,
    )


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    try:
        current = path.resolve()
        stop_resolved = stop.resolve()
    except OSError:
        return
    while current != stop_resolved and stop_resolved in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def prune_bad_segment_cache(
    *,
    cache_dir: Path | None = None,
    apply: bool = False,
    quarantine_dir: Path | None = None,
) -> dict[str, Any]:
    root = cache_dir or DEFAULT_CACHE_DIR
    quarantine_root = quarantine_dir or (
        DEFAULT_QUARANTINE_DIR if cache_dir is None else root.parent / "segment_cache_quarantine"
    )
    run_directory = quarantine_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    records: list[dict[str, Any]] = []
    scanned = 0
    quarantined = 0

    if root.is_dir():
        for path in sorted(root.rglob("segment_*.json")):
            scanned += 1
            payload, read_issues = _load_payload(path)
            issues = read_issues if read_issues else _cache_quality_issues(path, payload or {})
            if not issues:
                continue
            records.append({
                "path": str(path),
                "issues": issues,
            })
            if apply:
                try:
                    relative = path.resolve().relative_to(root.resolve())
                    destination = run_directory / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(destination))
                except (OSError, shutil.Error) as exc:
                    records[-1]["quarantine_error"] = str(exc)
                    continue
                records[-1]["quarantine_path"] = str(destination)
                quarantined += 1
                _remove_empty_parents(path.parent, stop=root)

    manifest_path = None
    if apply and quarantined:
        manifest_path = run_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source_cache_dir": str(root),
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return {
        "cache_dir": str(root),
        "quarantine_dir": str(quarantine_root),
        "manifest_path": str(manifest_path) if manifest_path else None,
        "dry_run": not apply,
        "scanned": scanned,
        "would_delete": len(records),
        "would_quarantine": len(records),
        "quarantined": quarantined,
        "deleted": 0,
        "records": records,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quarantine unsafe meeting segment transcript cache files."
    )
    parser.add_argument("--apply", action="store_true", help="Move unsafe cache files into quarantine.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Segment cache directory to scan. Defaults to output/segment_cache.",
    )
    parser.add_argument(
        "--quarantine-dir",
        type=Path,
        default=None,
        help="Recoverable quarantine directory. Defaults next to the cache root.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    payload = prune_bad_segment_cache(
        cache_dir=args.cache_dir,
        apply=args.apply,
        quarantine_dir=args.quarantine_dir,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
