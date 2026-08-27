"""Create lossless WebM playback derivatives with a finite seekable timeline.

Chrome MediaRecorder output is a live-style WebM stream.  The bytes are valid
evidence and play from the beginning, but commonly omit the duration/cue data
that browser controls need for random access.  This module keeps those source
bytes untouched and caches an ffmpeg stream-copy remux for inline playback.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional


class SeekableMediaError(RuntimeError):
    """Raised when a seekable playback derivative cannot be prepared."""


_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_WEBM_MAGIC = b"\x1aE\xdf\xa3"
_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[str, threading.Lock] = {}


def _bounded_timeout_seconds() -> int:
    try:
        configured = int(os.getenv("MEETING_PLAYBACK_REMUX_TIMEOUT_SECONDS", "300"))
    except (TypeError, ValueError):
        configured = 300
    return min(max(configured, 10), 1800)


def _resolve_media_tool(*environment_names: str, fallback: str) -> Optional[str]:
    for name in environment_names:
        configured = str(os.getenv(name) or "").strip()
        if configured:
            return configured
    return shutil.which(fallback)


def _source_content_key(source_path: Path, original_name: Optional[str]) -> str:
    """Use the content-addressed filename when possible, otherwise hash bytes."""
    candidate_stem = Path(str(original_name or source_path.name)).stem
    if _CONTENT_HASH_RE.fullmatch(candidate_stem):
        return candidate_stem.lower()

    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SeekableMediaError(f"無法讀取原始媒體：{exc}") from exc
    return digest.hexdigest()


def _is_webm_file(source_path: Path) -> bool:
    if source_path.suffix.lower() != ".webm":
        return False
    try:
        with source_path.open("rb") as handle:
            return handle.read(len(_WEBM_MAGIC)) == _WEBM_MAGIC
    except OSError:
        return False


def _target_lock(target_path: Path) -> threading.Lock:
    key = str(target_path.resolve())
    with _LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(key, threading.Lock())


def _subprocess_creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _probe_duration_seconds(ffprobe: str, media_path: Path) -> float:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(_bounded_timeout_seconds(), 60),
            check=False,
            creationflags=_subprocess_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SeekableMediaError(f"無法檢查播放檔時長：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise SeekableMediaError(detail or "ffprobe 無法讀取播放檔時長")
    try:
        duration = float((result.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise SeekableMediaError("播放衍生檔沒有有效時長") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SeekableMediaError("播放衍生檔沒有有限且大於零的時長")
    return duration


def ensure_seekable_playback_media(
    source_path: Path,
    cache_dir: Path,
    *,
    original_name: Optional[str] = None,
) -> Path:
    """Return source media or a cached lossless, seekable WebM derivative.

    Only valid WebM sources need this treatment.  Other formats are returned
    unchanged because their container already carries a finalized timeline.
    """
    source_path = Path(source_path)
    if source_path.suffix.lower() != ".webm":
        return source_path
    if not _is_webm_file(source_path):
        raise SeekableMediaError("原始 WebM 檔案標頭不完整")

    content_key = _source_content_key(source_path, original_name)
    cache_dir = Path(cache_dir)
    target_path = cache_dir / f"{content_key}.seekable.webm"

    with _target_lock(target_path):
        try:
            if target_path.is_file() and target_path.stat().st_size > 0:
                return target_path
        except OSError:
            pass

        ffmpeg = _resolve_media_tool("FFMPEG_PATH", "FFMPEG_BINARY", fallback="ffmpeg")
        ffprobe = _resolve_media_tool("FFPROBE_PATH", "FFPROBE_BINARY", fallback="ffprobe")
        if not ffmpeg or not ffprobe:
            raise SeekableMediaError("缺少 ffmpeg 或 ffprobe，無法建立可拖曳播放版本")

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SeekableMediaError(f"無法建立播放快取資料夾：{exc}") from exc

        temp_path = cache_dir / f".{target_path.stem}.{uuid.uuid4().hex}.tmp.webm"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "webm",
            str(temp_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_bounded_timeout_seconds(),
                check=False,
                creationflags=_subprocess_creation_flags(),
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip()
                raise SeekableMediaError(detail or f"ffmpeg 結束碼 {result.returncode}")
            if not temp_path.is_file() or temp_path.stat().st_size <= 0:
                raise SeekableMediaError("ffmpeg 沒有產生播放衍生檔")
            _probe_duration_seconds(ffprobe, temp_path)
            temp_path.replace(target_path)
        except SeekableMediaError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise SeekableMediaError("建立可拖曳播放版本逾時") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise SeekableMediaError(f"建立可拖曳播放版本失敗：{exc}") from exc
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

        return target_path
