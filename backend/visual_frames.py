"""Small, per-record JPEG bundles for frames that the final minutes cite."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zipfile import BadZipFile, ZipFile


logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 400_000
MAX_FRAMES = 8
MAX_BUNDLE_BYTES = MAX_FRAME_BYTES * MAX_FRAMES + 4096  # ZIP headers only
FRAME_ID = re.compile(r"[0-9a-f]{64}")


def bundle_path(markdown: Path) -> Path:
    return markdown.parent.resolve() / (markdown.name + ".visual.zip")


def _jpeg(data: bytes) -> bool:
    return (4 < len(data) <= MAX_FRAME_BYTES and data.startswith(b"\xff\xd8")
            and data.endswith(b"\xff\xd9"))


def attach_referenced_frames(markdown: Path, notes: dict, frames: dict) -> int:
    """Publish only approved frames; leave notes unchanged if storage fails."""
    images: dict[str, bytes] = {}
    links = []
    for group in notes.values():
        for note in group:
            frame = frames.get(note.get("frame_id"))
            if not frame or not _jpeg(frame[1]):
                continue
            data = frame[1]
            digest = hashlib.sha256(data).hexdigest()
            if digest not in images and len(images) >= MAX_FRAMES:
                continue
            images[digest] = data
            links.append((note, digest))
    if not images:
        return 0
    temporary = None
    try:
        target = bundle_path(markdown)
        if target.is_symlink():
            raise OSError("visual bundle may not be a symbolic link")
        with NamedTemporaryFile(dir=target.parent, prefix=".visual-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            with ZipFile(stream, "w") as bundle:
                for digest, data in images.items():
                    bundle.writestr(digest + ".jpg", data)
        temporary.replace(target)
    except OSError:
        logger.warning("Visual screenshots could not be stored; retaining text and playback references")
        return 0
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not clean up the temporary visual screenshot bundle")
    for note, digest in links:
        note["frame_sha256"] = digest
    return len(images)


def read_frame(markdown: Path, digest: str) -> bytes | None:
    if not FRAME_ID.fullmatch(digest):
        return None
    try:
        path = bundle_path(markdown)
        if path.is_symlink() or path.stat().st_size > MAX_BUNDLE_BYTES:
            return None
        with ZipFile(path) as bundle:
            item = bundle.getinfo(digest + ".jpg")
            if not 4 < item.file_size <= MAX_FRAME_BYTES:
                return None
            with bundle.open(item) as stream:
                data = stream.read(MAX_FRAME_BYTES + 1)
        return data if _jpeg(data) and hashlib.sha256(data).hexdigest() == digest else None
    except (OSError, KeyError, ValueError, BadZipFile, RuntimeError, NotImplementedError):
        return None


def discard_frame_bundle(markdown: Path) -> None:
    try:
        bundle_path(markdown).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the meeting's screenshot bundle")
