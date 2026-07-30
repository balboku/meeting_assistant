"""Dry-run or apply deterministic D/R/A backfill for historical meetings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database  # noqa: E402
from backend.confirmation_queue import apply_structured_minutes_backfill  # noqa: E402
from backend.maintenance import backup_database, verify_database_backup  # noqa: E402
from backend.structured_minutes import parse_standardized_minutes  # noqa: E402


def build_report() -> tuple[dict[str, object], list[tuple[int, dict[str, object]]]]:
    parsed: list[tuple[int, dict[str, object]]] = []
    meetings: list[dict[str, object]] = []
    with database.get_db() as conn:
        rows = conn.execute(
            """SELECT id, output_path, structured_summary_json
                 FROM meetings
                ORDER BY id"""
        ).fetchall()
    for row in rows:
        meeting_id = int(row["id"])
        output_path = Path(str(row["output_path"] or ""))
        entry: dict[str, object] = {
            "meeting_id": meeting_id,
            "output_path": str(output_path),
            "existing_structured_summary": bool(row["structured_summary_json"]),
        }
        if not output_path.is_file():
            entry["errors"] = ["missing markdown file"]
            meetings.append(entry)
            continue
        result = parse_standardized_minutes(
            output_path.read_text(encoding="utf-8"),
            source_path=output_path,
        )
        counts = {
            "D": len(result["discussion_summary"]),
            "R": len(result["final_decisions"]),
            "A": len(result["action_items"]),
        }
        entry["counts"] = counts
        entry["errors"] = list(result["parse_errors"])
        entry["confirmation_tasks"] = sum(
            len(item.get("confirmation_required") or [])
            for field in ("discussion_summary", "final_decisions", "action_items")
            for item in result[field]
        )
        meetings.append(entry)
        if not entry["errors"] and not entry["existing_structured_summary"]:
            parsed.append((meeting_id, result))

    totals = {
        key: sum(int((entry.get("counts") or {}).get(key, 0)) for entry in meetings)
        for key in ("D", "R", "A")
    }
    report = {
        "mode": "dry-run",
        "meeting_count": len(meetings),
        "eligible_count": len(parsed),
        "error_count": sum(bool(entry["errors"]) for entry in meetings),
        "existing_structured_summary_count": sum(
            bool(entry["existing_structured_summary"]) for entry in meetings
        ),
        "totals": totals,
        "confirmation_tasks": sum(
            int(entry.get("confirmation_tasks") or 0) for entry in meetings
        ),
        "meetings": meetings,
    }
    return report, parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply eligible rows after a verified database backup",
    )
    args = parser.parse_args()
    report, parsed = build_report()
    if args.apply:
        if report["error_count"] or report["eligible_count"] != report["meeting_count"]:
            raise SystemExit("dry-run 未通過，拒絕套用")
        database.init_db()
        backup_path = backup_database(database.DB_PATH, ROOT / "backups")
        backup_verification = verify_database_backup(backup_path)
        if not backup_verification["ok"]:
            raise SystemExit(f"套用前備份驗證失敗：{backup_verification['detail']}")
        applied = apply_structured_minutes_backfill(
            parsed,
            actor_email="local-admin@meeting-assistant.local",
        )
        report["mode"] = "apply"
        report["backup"] = backup_verification
        report["result"] = applied
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["error_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
