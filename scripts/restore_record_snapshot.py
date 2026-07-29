"""Restore a verified meeting-record snapshot into a new empty directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.maintenance import restore_record_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="meeting_records_*.zip")
    parser.add_argument("target", type=Path, help="new or empty restore target")
    args = parser.parse_args()
    result = restore_record_snapshot(args.snapshot, args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
