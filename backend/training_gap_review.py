"""Read-only projection of an operator-reviewed, source-bound gap snapshot."""

import hashlib
import json
from pathlib import Path
import re

from backend.transcription_windows import Window

DIRECTORY = "training_gap_reviews"
SAMPLE_RATE = 16000
MAX_MANIFEST_BYTES = 128 * 1024


def _ranges(value):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("Invalid window list")
    result = []
    for raw in value:
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError("Invalid sample bounds")
        window = Window(*raw)
        if window.end > SAMPLE_RATE * 7 * 86400 or (result and window.start < result[-1].end):
            raise ValueError("Unordered, overlapping, or out-of-bounds windows")
        result.append(window)
    return result


def review_file(output_dir: Path, course_id: int, name: str) -> Path:
    root = (output_dir / DIRECTORY).resolve()
    path = (root / str(course_id) / name).resolve()
    if path.parent != root / str(course_id) or path.name != name:
        raise ValueError("Unexpected review path")
    return path


def project_review_windows(output_dir: Path, course: dict) -> str | None:
    """Attach playback coordinates only; return the snapshot digest to local callers."""
    for part in course["parts"]:
        part.pop("review_windows", None)
    try:
        manifest = review_file(output_dir, course["id"], "review.json")
        with manifest.open("rb") as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            return
        bundle = json.loads(data)
        if (not isinstance(bundle, dict) or bundle.get("schema") != 1
                or bundle.get("course_id") != course["id"] or bundle.get("sample_rate") != SAMPLE_RATE
                or not isinstance(bundle.get("parts"), dict)):
            return
    except (OSError, ValueError, RuntimeError):
        return
    for part in course["parts"]:
        entry = bundle["parts"].get(part["id"])
        if (part["state"] != "failed" or not isinstance(entry, dict)
                or entry.get("job_id") != part["job_id"]
                or entry.get("source_sha256") != part["source_sha256"]
                or not re.fullmatch(r"[a-f0-9]{64}", part["source_sha256"] or "")):
            continue
        try:
            pending = _ranges(entry.get("review_frames"))
            retained = _ranges(entry.get("verified_frames"))
            if not retained or any(a.overlaps(b) for a in pending for b in retained):
                continue
        except ValueError:
            continue
        if pending:
            part["review_windows"] = [{"start_seconds": w.start / SAMPLE_RATE, "end_seconds": w.end / SAMPLE_RATE}
                                      for w in pending]
    return hashlib.sha256(data).hexdigest()
