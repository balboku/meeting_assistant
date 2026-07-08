"""Helpers for retaining uploaded source audio without storing duplicates."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
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
    """Move a validated upload into place, or reuse an existing file with the same SHA256."""
    with _SOURCE_AUDIO_DEDUP_LOCK:
        duplicate = find_source_audio_by_sha256(
            final_path.parent,
            digest,
            size_bytes,
            supported_suffixes,
            exclude=temp_path,
        )
        if duplicate is not None:
            temp_path.unlink(missing_ok=True)
            logger.info(
                "♻️  上傳內容 SHA256 已存在，重用原始檔：%s（sha256=%s）",
                duplicate,
                digest,
            )
            return duplicate, False

        candidate = final_path
        while candidate.exists():
            candidate = final_path.with_name(f"{final_path.stem}_{uuid.uuid4().hex[:8]}{final_path.suffix}")
        temp_path.replace(candidate)
        return candidate, True
