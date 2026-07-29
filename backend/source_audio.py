"""Helpers for retaining uploaded source audio without storing duplicates."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Collection

logger = logging.getLogger("MeetingAssistant.SourceAudio")

HASH_READ_CHUNK_BYTES = 1024 * 1024
_SOURCE_AUDIO_DEDUP_LOCK = threading.Lock()


def sha256_file(path: Path) -> str:
    """Return the SHA256 digest for a local file without loading it all into memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def content_addressed_source_path(final_path: Path, digest: str) -> Path:
    """Return the stable source-media object name for a verified SHA-256."""
    normalized_digest = str(digest or "").strip().lower()
    if (
        len(normalized_digest) != 64
        or any(character not in "0123456789abcdef" for character in normalized_digest)
    ):
        raise ValueError("source media SHA-256 格式不正確")
    return final_path.with_name(f"{normalized_digest}{final_path.suffix.lower()}")


def find_source_audio_by_sha256(
    source_dir: Path,
    digest: str,
    size_bytes: int,
    supported_suffixes: Collection[str],
    exclude: Path | None = None,
) -> Path | None:
    excluded: Path | None = None
    if exclude is not None:
        try:
            excluded = exclude.resolve()
        except OSError:
            excluded = exclude.absolute()

    normalized_suffixes = {suffix.lower() for suffix in supported_suffixes}
    for candidate in sorted(source_dir.iterdir()):
        if not candidate.is_file() or candidate.name.startswith(".upload_"):
            continue
        if candidate.suffix.lower() not in normalized_suffixes:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if excluded is not None and resolved == excluded:
            continue
        try:
            if candidate.stat().st_size != size_bytes:
                continue
        except OSError:
            continue
        if sha256_file(candidate) == digest:
            return candidate
    return None


def finalize_source_audio_upload(
    temp_path: Path,
    final_path: Path,
    digest: str,
    size_bytes: int,
    supported_suffixes: Collection[str],
) -> tuple[Path, bool]:
    """Commit one upload to a SHA-256 addressed object, reusing identical bytes."""
    with _SOURCE_AUDIO_DEDUP_LOCK:
        object_path = content_addressed_source_path(final_path, digest)
        if object_path.is_file():
            if (
                object_path.stat().st_size != int(size_bytes)
                or sha256_file(object_path) != digest.lower()
            ):
                raise OSError(f"內容定址媒體檔發生 SHA-256 衝突：{object_path}")
            temp_path.unlink(missing_ok=True)
            return object_path, False

        duplicate = find_source_audio_by_sha256(
            final_path.parent,
            digest,
            size_bytes,
            supported_suffixes,
            exclude=temp_path,
        )
        if duplicate is not None:
            # Preserve legacy filename references while exposing the same bytes
            # under the canonical object name. NTFS hard links avoid duplication;
            # copy2 is a safe fallback for filesystems that do not support links.
            try:
                os.link(duplicate, object_path)
            except OSError:
                shutil.copy2(duplicate, object_path)
            temp_path.unlink(missing_ok=True)
            logger.info(
                "♻️  上傳內容 SHA256 已存在，建立內容定址引用：%s（來源=%s）",
                object_path,
                duplicate,
            )
            return object_path, True

        if sha256_file(temp_path) != digest.lower():
            raise OSError(
                f"上傳暫存檔 SHA-256 驗證失敗：{temp_path}"
            )
        if temp_path.stat().st_size != int(size_bytes):
            raise OSError(
                f"上傳暫存檔大小驗證失敗：{temp_path}"
            )
        temp_path.replace(object_path)
        logger.info(
            "📦 已保存內容定址原始媒體：%s（sha256=%s）",
            object_path,
                digest,
        )
        return object_path, True
