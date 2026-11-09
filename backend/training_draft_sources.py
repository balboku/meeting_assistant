"""Provenance-checked text sources for a local, read-only training draft."""
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import uuid

from backend.training_gap_review import SAMPLE_RATE, ReviewedPart, review_file
from backend.transcription_windows import Window, _union

_LOGGER = logging.getLogger(__name__)
_INDEX = "training-draft-index.json"
_MAX_SLICES = 512
_MAX_FILE_BYTES = 256 * 1024
_MAX_SECONDS = 7 * 86400
_SHA = re.compile(r"[0-9a-f]{64}")
_PART = re.compile(r"[A-Za-z0-9-]{1,64}")


def _checksum(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class DraftReadBudget:
    remaining: int = 8 * 1024 * 1024

    def read(self, path: Path, digest: str | None = None) -> dict:
        if path.resolve() != path or self.remaining <= 0:
            raise ValueError("Unsafe or over-budget draft source")
        limit = min(_MAX_FILE_BYTES, self.remaining)
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        self.remaining -= len(raw)
        if len(raw) > limit or (digest is not None and hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError("Unusable draft source")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Expected a draft source object")
        return value


def _read_index(path, budget):
    envelope = budget.read(path)
    value = envelope.get("value")
    if not isinstance(value, dict) or envelope.get("sha256") != _checksum(value):
        raise ValueError("Draft index checksum mismatch")
    return value


def _bounds(value):
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_SLICES:
        raise ValueError("Invalid slice bounds")
    windows = []
    for bounds in value:
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("Invalid slice window")
        window = Window(*bounds)
        if window.end > _MAX_SECONDS or (windows and (
                window.start < windows[-1].start or window.end <= windows[-1].end)):
            raise ValueError("Invalid slice order")
        windows.append(window)
    return windows


def record_verified_slice(root, job_id, part_id, source_sha, index, context, checkpoint):
    """Index a checkpoint only after the existing full-slice quality gate saved it."""
    bounds = context.get("segment_bounds")
    if (not bounds or context.get("source_audio_sha256") != source_sha
            or not _SHA.fullmatch(source_sha or "") or not _PART.fullmatch(part_id)):
        return
    temporary = None
    try:
        windows = _bounds(bounds)
        if len(windows) != context.get("total_segments") or type(index) is not int or not 0 <= index < len(windows):
            return
        root = root.absolute()
        checkpoint = checkpoint.absolute()
        if root.resolve() != root or checkpoint.parent != root or checkpoint.resolve() != checkpoint:
            return
        with checkpoint.open("rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            return
        header = {"schema": 1, "job_id": job_id, "part_id": part_id, "source_sha256": source_sha,
                  "bounds": bounds, "plan": _checksum(context)}
        manifest = root / _INDEX
        try:
            previous = _read_index(manifest, DraftReadBudget())
        except (OSError, ValueError, RuntimeError):
            previous = {}
        entries = previous.get("slices")
        if not isinstance(entries, dict) or len(entries) > len(windows) or any(
                previous.get(key) != value for key, value in header.items()):
            entries = {}
        entries[str(index)] = [checkpoint.name, hashlib.sha256(raw).hexdigest()]
        value = {**header, "slices": entries}
        temporary = manifest.with_name(f".{uuid.uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump({"value": value, "sha256": _checksum(value)}, stream, ensure_ascii=False)
        temporary.replace(manifest)
    except (OSError, ValueError, RuntimeError):
        _LOGGER.warning("Could not index a verified training slice for draft preview")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def verified_slices(output_dir, part, budget):
    """Read just the indexed current job/source, never search generic caches."""
    job, part_id, source = part.get("job_id"), part["id"], part.get("source_sha256")
    if not isinstance(job, str) or not job or not _SHA.fullmatch(source or ""):
        return None, []
    root = output_dir.resolve() / "job_checkpoints" / hashlib.sha256(job.encode()).hexdigest()
    try:
        manifest = _read_index(root / _INDEX, budget)
        if any(manifest.get(k) != v for k, v in {
                "schema": 1, "job_id": job, "part_id": part_id, "source_sha256": source}.items()):
            return None, []
        bounds = _bounds(manifest.get("bounds"))
        entries = manifest.get("slices")
        if not isinstance(entries, dict) or len(entries) > len(bounds):
            return None, []
    except (OSError, ValueError, RuntimeError):
        return None, []
    fragments = []
    for i, window in enumerate(bounds):
        entry = entries.get(str(i))
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        name, digest = entry
        if (not isinstance(name, str) or not name.endswith(".json") or not _SHA.fullmatch(name[:-5])
                or not isinstance(digest, str) or not _SHA.fullmatch(digest)):
            continue
        try:
            checkpoint = budget.read(root / name, digest)
            value = checkpoint.get("value")
            text = value.get("transcript") if isinstance(value, dict) else None
            if isinstance(text, str) and text.strip():
                fragments.append((Window(window.start * SAMPLE_RATE, window.end * SAMPLE_RATE), text))
        except (OSError, ValueError, RuntimeError):
            continue
    return Window(0, bounds[-1].end * SAMPLE_RATE), fragments


def reviewed_slices(output_dir, course_id, part, review: ReviewedPart, budget):
    """Read only exact artifacts named and checksummed by an operator review."""
    part_id = part["id"]
    if not _PART.fullmatch(part_id) or not isinstance(review.artifacts, list) or len(review.artifacts) > _MAX_SLICES:
        return []
    try:
        root = review_file(output_dir, course_id, "review.json").parent
    except (OSError, ValueError, RuntimeError):
        return []
    fragments = {}
    for entry in review.artifacts:
        if not isinstance(entry, dict):
            continue
        name, digest = entry.get("path"), entry.get("sha256")
        match = re.fullmatch(rf"{part_id}/verified-(\d{{1,11}})-(\d{{1,11}})\.json", name) if isinstance(name, str) else None
        if not match or not isinstance(digest, str) or not _SHA.fullmatch(digest):
            continue
        try:
            window = Window(*map(int, match.groups()))
            if (window in fragments or not any(w.start <= window.start < window.end <= w.end for w in review.retained)
                    or any(window.overlaps(w) for w in review.pending)):
                continue
            artifact = budget.read(root / name, digest)
            text = artifact.get("transcript")
            if (artifact.get("source_sha256") == part["source_sha256"] and artifact.get("rate") == SAMPLE_RATE
                    and artifact.get("range_frames") == [window.start, window.end]
                    and isinstance(text, str) and text.strip()):
                fragments[window] = text
        except (OSError, ValueError, RuntimeError):
            continue
    return sorted(fragments.items())


def uncovered(extent: Window, fragments):
    """Only coverage backed by a readable verified artifact can hide a missing range."""
    cursor = extent.start
    for window in _union(window for window, _ in fragments):
        if cursor < window.start:
            yield Window(cursor, window.start)
        cursor = window.end
    if cursor < extent.end:
        yield Window(cursor, extent.end)
