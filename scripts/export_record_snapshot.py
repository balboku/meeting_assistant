#!/usr/bin/env python3
"""Export a shared-media backup as a standalone legacy-compatible v2 ZIP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.maintenance import verify_record_snapshot
from backend.snapshot_media import shared_objects


def export_snapshot(source: Path, destination: Path) -> Path:
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError("匯出目標已存在，未覆寫")
    verification = verify_record_snapshot(source)
    if not verification.get("recoverability_complete"):
        raise ValueError(f"來源備份不完整：{verification['detail']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED) as output:
            manifest = json.loads(original.read("manifest.json"))
            for name in original.namelist():
                if name == "manifest.json":
                    continue
                with original.open(name) as member, output.open(name, "w", force_zip64=True) as target:
                    shutil.copyfileobj(member, target, 1024 * 1024)
            for name, media in shared_objects(source, manifest).items():
                output.write(media, name)
            manifest.update(format_version=2, shared_media={})
            output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if not verify_record_snapshot(temporary).get("recoverability_complete"):
            raise ValueError("匯出內容驗證失敗，未發布")
        # No overwrite of any existing target, on Windows or POSIX.
        import os
        if os.name == "nt":
            temporary.rename(destination)
        else:
            os.link(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(export_snapshot(args.snapshot, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
