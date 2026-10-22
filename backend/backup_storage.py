"""Read-only local backup accounting; never scan an offsite/network root."""
from __future__ import annotations

import os
from pathlib import Path


def backup_storage_metrics(root: Path) -> dict[str, object]:
    groups = {name: 0 for name in ("snapshots", "shared_media", "release", "database", "other")}
    count, errors = 0, []
    root = Path(root)
    try:
        try:
            root.stat()
            present = True
        except FileNotFoundError:
            present = False
        if present:
            if not root.is_dir():
                raise NotADirectoryError(str(root))
            def on_error(error):
                errors.append(type(error).__name__)
            for parent, directories, filenames in os.walk(root, onerror=on_error, followlinks=False):
                base = Path(parent)
                for directory in directories[:]:
                    candidate = base / directory
                    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                        directories.remove(directory)
                        errors.append("linked_directory")
                for filename in filenames:
                    path = base / filename
                    if path.is_symlink():
                        errors.append("linked_file")
                        continue
                    parts = path.relative_to(root).parts
                    category = (
                        "shared_media" if "media_objects" in parts[:-1] else
                        "release" if parts[0].startswith(("release_", "hotfix_")) else
                        "snapshots" if len(parts) == 1 and filename.startswith("meeting_records_") and path.suffix == ".zip" else
                        "database" if path.suffix == ".db" else "other"
                    )
                    try:
                        groups[category] += path.stat().st_size
                        count += 1
                    except OSError as exc:
                        errors.append(type(exc).__name__)
    except OSError as exc:
        errors.append(type(exc).__name__)
    return {"complete": not errors, "total_bytes": sum(groups.values()) if not errors else None,
            "files": count, "categories": groups, "scope": "local_backup_directory",
            "excludes": ["live_media", "offsite", "recycle_bin"], "scan_errors": len(errors)}
