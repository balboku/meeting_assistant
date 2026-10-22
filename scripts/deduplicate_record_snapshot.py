#!/usr/bin/env python3
"""Convert a complete legacy snapshot to a NEW v3 snapshot; never delete sources.

ZIP and sibling media_objects must remain together. Retirement of old ZIPs is
deliberately NOT part of this command and requires a separate verified gate.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.maintenance import verify_record_snapshot
from backend.snapshot_media import file_digest, object_path, safe_member, shared_objects


def publish_new(temporary: Path, destination: Path) -> None:
    """Atomically publish an independently written copy without replacement."""
    if os.name == "nt":
        temporary.rename(destination)
    else:
        os.link(temporary, destination)


def checked_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member")
        for name in names:
            safe_member(name)
        manifest = json.loads(archive.read("manifest.json"))
        entries = manifest.get("entries", [])
        by_name = {entry["archive_path"]: entry for entry in entries}
        if not entries or len(by_name) != len(entries):
            raise ValueError("Empty or duplicate manifest entries")
        external = shared_objects(path, manifest)
        actual = {info.filename for info in archive.infolist() if not info.is_dir()} | set(external)
        if actual != set(by_name) | {"manifest.json"}:
            raise ValueError("Unmanifested or missing content; migration refused")
        for name, entry in by_name.items():
            safe_member(name)
            if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
                raise ValueError("Invalid manifest length")
            if name not in external and archive.getinfo(name).file_size != entry["bytes"]:
                raise ValueError("ZIP length differs from manifest")
            if entry.get("category") == "source_media":
                if not name.startswith("source_media/"):
                    raise ValueError("Media category outside source_media")
                object_path(path, entry["sha256"])
        return manifest


def assert_equivalent(source: Path, destination: Path) -> dict:
    before, after = checked_manifest(source), checked_manifest(destination)
    for manifest in (before, after):
        manifest.pop("format_version", None)
        manifest.pop("shared_media", None)
    if before != after:
        raise ValueError("Historical manifest changed beyond storage format")
    canonical = json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"equivalent": True, "entries": len(before["entries"]),
            "logical_manifest_sha256": hashlib.sha256(canonical).hexdigest()}


def store_member(archive: zipfile.ZipFile, entry: dict, snapshot: Path) -> None:
    destination = object_path(snapshot, entry["sha256"])
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size != entry["bytes"] or file_digest(destination) != entry["sha256"]:
            raise ValueError("Existing shared object is corrupt; NOT overwritten")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        digest, size = hashlib.sha256(), 0
        with archive.open(entry["archive_path"]) as original, temporary.open("xb") as target:
            while chunk := original.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != entry["sha256"] or size != entry["bytes"]:
            raise ValueError("Extracted shared object failed SHA/length validation")
        publish_new(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def convert_snapshot(source: Path, destination: Path) -> dict:
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.resolve() != source or destination.resolve() != destination:
        raise ValueError("Linked snapshot paths refused")
    if destination.exists():
        raise FileExistsError("Destination exists; NOT overwritten")
    before = source.stat()
    verification = verify_record_snapshot(source)
    if not verification.get("recoverability_complete"):
        raise ValueError(f"Incomplete source: {verification.get('detail')}")
    manifest = checked_manifest(source)
    if manifest.get("format_version") not in (1, 2):
        raise ValueError("Only legacy v1/v2 snapshots require conversion")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    media = {e["archive_path"]: e for e in manifest["entries"] if e.get("category") == "source_media"}
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED) as output:
            output.comment = original.comment
            for entry in media.values():
                store_member(original, entry, destination)
            for info in original.infolist():
                if info.filename == "manifest.json" or info.filename in media:
                    continue
                with original.open(info) as member, output.open(copy.copy(info), "w", force_zip64=True) as target:
                    while chunk := member.read(1024 * 1024):
                        target.write(chunk)
            manifest.update(format_version=3, shared_media={
                name: {"sha256": entry["sha256"], "bytes": entry["bytes"]} for name, entry in media.items()})
            output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        new_check = verify_record_snapshot(temporary)
        if not new_check.get("recoverability_complete"):
            raise ValueError(f"Converted snapshot invalid: {new_check.get('detail')}")
        equivalence = assert_equivalent(source, temporary)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Source changed during conversion")
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        publish_new(temporary, destination)
        return {"source": str(source), "destination": str(destination), "verification": new_check,
                "sha256": file_digest(destination), "bytes": destination.stat().st_size, **equivalence}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(convert_snapshot(args.snapshot, args.destination), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
