"""Version 3 snapshots: immutable, independently copied media shared per backup root.

The ZIP and its sibling media_objects directory form one recoverable backup.
No garbage collection or changes to live sources are performed here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
import zipfile

POOL_NAME = "media_objects"


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_member(name: str) -> None:
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or "\\" in name or ":" in name or any(p in {".", ".."} for p in parts):
        raise ValueError(f"不安全的快照路徑：{name}")


def object_path(snapshot: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("共用影音 SHA-256 格式無效")
    root = snapshot.parent.resolve()
    candidate = root / POOL_NAME / digest[:2] / digest
    # Refuse symlinks/junctions: backup copies must stay in this backup root.
    if candidate.resolve() != candidate:
        raise ValueError("共用影音路徑不可包含符號連結或接合點")
    return candidate


def copy_object(source: Path, destination: Path, digest: str, size: int) -> None:
    """Verify reuse, or publish a verified independent copy atomically."""
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size != size or file_digest(destination) != digest:
            raise ValueError(f"既存共用影音損壞，未覆寫：{destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)  # Never hardlink a live recording.
        if temporary.stat().st_size != size or file_digest(temporary) != digest:
            raise ValueError("影音複製期間內容改變，未發布備份")
        # Atomic, no-replace publication. The POSIX link is to the verified
        # temporary backup copy, never to the original recording.
        try:
            if os.name == "nt":
                temporary.rename(destination)
            else:
                os.link(temporary, destination)
        except FileExistsError:
            if destination.stat().st_size != size or file_digest(destination) != digest:
                raise ValueError("共用影音正在寫入或已損壞，請稍後重試")
    finally:
        temporary.unlink(missing_ok=True)


class SnapshotArchive(zipfile.ZipFile):
    def __init__(self, path: Path, *, shared_media: bool, validator, **kwargs):
        self.pending_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        super().__init__(self.pending_path, **kwargs)
        self.snapshot_path = path
        self.shared_media = shared_media
        self.validator = validator
        self.verification: dict[str, object] = {}
        self.media_objects: dict[str, dict[str, object]] = {}

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
            if exc_type is None:
                self.verification = self.validator(self.pending_path)
                if not self.verification.get("ok"):
                    raise ValueError(f"新快照驗證失敗，未發布：{self.verification.get('detail')}")
                # Objects and a verified, closed ZIP exist before it becomes
                # visible to rotation and health checks. Never overwrite history.
                if os.name == "nt":
                    self.pending_path.rename(self.snapshot_path)
                else:
                    os.link(self.pending_path, self.snapshot_path)
        finally:
            self.pending_path.unlink(missing_ok=True)

    def write(self, filename, arcname=None, compress_type=None, compresslevel=None):
        if self.shared_media and str(arcname or "").startswith("source_media/"):
            safe_member(str(arcname))
            source = Path(filename)
            digest, size = file_digest(source), source.stat().st_size
            copy_object(source, object_path(self.snapshot_path, digest), digest, size)
            self.media_objects[str(arcname)] = {"sha256": digest, "bytes": size}
            return
        return super().write(filename, arcname, compress_type, compresslevel)


def shared_objects(snapshot: Path, manifest: dict) -> dict[str, Path]:
    """Validate mapping against existing manifest entries, not arbitrary paths."""
    mapping = manifest.get("shared_media", {})
    if manifest.get("format_version") != 3:
        if mapping:
            raise ValueError("舊格式不可宣告外部影音")
        return {}
    if not isinstance(mapping, dict):
        raise ValueError("共用影音清單格式無效")
    entries = {e["archive_path"]: e for e in manifest.get("entries", [])}
    result = {}
    for name, metadata in mapping.items():
        safe_member(name)
        entry = entries.get(name, {})
        if not name.startswith("source_media/") or entry.get("category") != "source_media" or not isinstance(metadata, dict):
            raise ValueError("共用影音只能引用已列入 manifest 的來源影音")
        if metadata.get("sha256") != entry.get("sha256") or metadata.get("bytes") != entry.get("bytes"):
            raise ValueError("共用影音與 manifest 不一致")
        if type(metadata.get("bytes")) is not int or metadata["bytes"] < 0:
            raise ValueError("共用影音容量無效")
        result[name] = object_path(snapshot, str(metadata.get("sha256", "")))
    return result


def shared_signature(snapshot: Path, manifest: dict) -> tuple:
    """Cache invalidation includes objects, not just the small ZIP's stat."""
    return tuple((name, str(path), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                 for name, path in sorted(shared_objects(snapshot, manifest).items()))


def materialize_media(snapshot: Path, manifest: dict, target: Path) -> None:
    for name, source in shared_objects(snapshot, manifest).items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(f"快照包含重複影音路徑：{name}")
        metadata = manifest["shared_media"][name]
        copy_object(source, destination, metadata["sha256"], metadata["bytes"])


def replicate_media(snapshot: Path, manifest: dict, destination: Path) -> None:
    for name, source in shared_objects(snapshot, manifest).items():
        metadata = manifest["shared_media"][name]
        target = object_path(destination, metadata["sha256"])
        copy_object(source, target, metadata["sha256"], metadata["bytes"])
