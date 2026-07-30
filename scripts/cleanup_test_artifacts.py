#!/usr/bin/env python3
"""Back up and remove one precisely identified leaked test fixture.

Dry-run is the default. ``--apply`` creates a ZIP containing every candidate
and a JSON manifest before deleting only unreferenced exact-hash matches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATTACHMENT_ROOT = ROOT / "output" / "attachments"
DEFAULT_DB_PATH = ROOT / "meetings.db"
DEFAULT_BACKUP_DIR = ROOT / "backups"
FIXTURE_SHA256 = "ea80334363eed145dfeee51ebae7dc3f1cd7d0c7879f8bfd2070c061d3c33f56"
FIXTURE_SIZE = 9


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def find_candidates(attachment_root: Path) -> list[Path]:
    root = attachment_root.resolve()
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for path in root.rglob("quote*.png"):
        if (
            path.is_file()
            and path.stat().st_size == FIXTURE_SIZE
            and _resolved_within(path, root)
            and _sha256(path) == FIXTURE_SHA256
        ):
            candidates.append(path.resolve())
    return sorted(candidates)


def referenced_paths(db_path: Path) -> set[Path]:
    if not db_path.is_file():
        return set()
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "meeting_evidence" not in tables:
            return set()
        values = conn.execute(
            "SELECT stored_path FROM meeting_evidence WHERE stored_path IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {
        Path(str(row[0])).resolve()
        for row in values
        if str(row[0] or "").strip()
    }


def _remove_empty_parents(path: Path, attachment_root: Path) -> None:
    root = attachment_root.resolve()
    current = path.parent.resolve()
    while current != root and _resolved_within(current, root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def cleanup(
    *,
    attachment_root: Path,
    db_path: Path,
    backup_dir: Path,
    apply: bool,
) -> dict[str, object]:
    candidates = find_candidates(attachment_root)
    referenced = referenced_paths(db_path)
    blocked = [path for path in candidates if path in referenced]
    removable = [path for path in candidates if path not in referenced]
    result: dict[str, object] = {
        "apply": bool(apply),
        "attachment_root": str(attachment_root.resolve()),
        "matched": len(candidates),
        "referenced_blocked": len(blocked),
        "removable": len(removable),
        "removed": 0,
        "backup_path": None,
        "fixture_sha256": FIXTURE_SHA256,
    }
    if not apply or not removable:
        return result

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"test_attachment_cleanup_{timestamp}.zip"
    manifest = {
        **result,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": [
            {
                "relative_path": path.relative_to(attachment_root.resolve()).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in removable
        ],
    }
    with zipfile.ZipFile(
        backup_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        for path in removable:
            archive.write(
                path,
                arcname=(
                    "attachments/"
                    + path.relative_to(attachment_root.resolve()).as_posix()
                ),
            )

    for path in removable:
        if (
            not path.is_file()
            or path.stat().st_size != FIXTURE_SIZE
            or _sha256(path) != FIXTURE_SHA256
            or path in referenced
        ):
            raise RuntimeError(f"candidate changed before deletion: {path}")
        path.unlink()
        _remove_empty_parents(path, attachment_root)

    result["removed"] = len(removable)
    result["backup_path"] = str(backup_path.resolve())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--attachment-root",
        type=Path,
        default=DEFAULT_ATTACHMENT_ROOT,
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    args = parser.parse_args()
    try:
        result = cleanup(
            attachment_root=args.attachment_root,
            db_path=args.db_path,
            backup_dir=args.backup_dir,
            apply=args.apply,
        )
    except (OSError, sqlite3.Error, RuntimeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
