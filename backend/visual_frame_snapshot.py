"""Include referenced screenshot bundles in existing portable record snapshots."""
from pathlib import Path
import hashlib
import shutil
from zipfile import BadZipFile, ZipFile

from backend.visual_frames import MAX_BUNDLE_BYTES, bundle_path, read_frame
from backend.visual_notes import referenced_frames


def snapshot_visual_frames(archive, markdown, arcname, meeting_id, entries, missing):
    path = bundle_path(markdown)
    refs = referenced_frames(markdown.read_text(encoding="utf-8", errors="replace"))
    # A previously cited image may still be needed by a retained revision.
    if not refs and not path.exists():
        return
    try:
        if path.is_symlink() or not 0 < path.stat().st_size <= MAX_BUNDLE_BYTES:
            raise ValueError("invalid bundle")
        with ZipFile(path) as bundle:
            names = set(bundle.namelist())
        digests = {name[:-4] for name in names if name.endswith(".jpg")}
        if (len(names) != len(digests) or not digests or not refs <= digests
                or any(read_frame(markdown, digest) is None for digest in digests)):
            raise ValueError("invalid frame reference")
    except (OSError, BadZipFile, ValueError):
        missing.append({"category": "visual_frames", "meeting_id": meeting_id, "source_path": str(path)})
        return
    archived = arcname + ".visual.zip"
    archive.write(path, archived)
    entries.append({"category": "visual_frames", "meeting_id": meeting_id,
                    "archive_path": archived, "source_path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    return archived


def restore_visual_frames(target: Path, markdown_target: Path, markdown_arcname: str, frame_arcname: str) -> int:
    if frame_arcname != markdown_arcname + ".visual.zip":
        raise ValueError("截圖備份與會議文件的路徑不符")
    source = (target / frame_arcname).resolve()
    if not source.is_relative_to(target.resolve()):
        raise ValueError("截圖備份路徑不合法")
    shutil.copy2(source, bundle_path(markdown_target))
    return 1
