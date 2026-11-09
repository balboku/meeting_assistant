"""Read-only course draft: reviewed notes, or literal extracts without inference."""

import hashlib
import json
import re
from pathlib import Path

from backend.training_gap_review import project_review_windows, review_file

MAX_DRAFT_BYTES = 128 * 1024
_TIMESTAMPED = re.compile(r"^\[\d{1,3}:\d{2}(?::\d{2})?\]\s+\S")


def source_fingerprint(course: dict, review_sha256: str | None) -> str:
    """Bind an operator-reviewed draft to the exact course, recordings and transcripts."""
    parts = [[p["id"], p["position"], p["job_id"], p["state"], p["source_sha256"],
              hashlib.sha256(p["transcript"].encode("utf-8")).hexdigest()] for p in course["parts"]]
    source = [course["id"], course["title"], course["version"], review_sha256, parts]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False).encode("utf-8")).hexdigest()


def _reviewed_markdown(output_dir: Path, course: dict, review_sha256: str | None) -> str:
    # A reviewed note is a self-contained snapshot, prepared only from checked inputs.
    # Never search raw caches or follow file paths supplied by the draft.
    try:
        with review_file(output_dir, course["id"], "draft.json").open("rb") as stream:
            data = stream.read(MAX_DRAFT_BYTES + 1)
        if len(data) > MAX_DRAFT_BYTES:
            return ""
        snapshot = json.loads(data)
        if (isinstance(snapshot, dict) and snapshot.get("schema") == 1
                and snapshot.get("source_fingerprint") == source_fingerprint(course, review_sha256)
                and isinstance(snapshot.get("markdown"), str)):
            return snapshot["markdown"].strip()
    except (OSError, ValueError, RuntimeError):
        pass
    return ""


def _extract(transcript: str) -> list[str]:
    """Pick intact timestamped lines from across a transcript, not a generated summary."""
    lines = [line.strip() for line in transcript.splitlines()
             if _TIMESTAMPED.match(line) and len(line) <= 700]
    if len(lines) <= 5:
        return lines
    return [lines[i * (len(lines) - 1) // 4] for i in range(5)]


def build_draft(output_dir: Path, course: dict) -> dict:
    if course["notes_current"]:
        return {"kind": "official"}
    review_sha256 = project_review_windows(output_dir, course)
    markdown = _reviewed_markdown(output_dir, course, review_sha256)
    parts = []
    for part in course["parts"]:
        transcript = part["transcript"]
        parts.append({
            "id": part["id"],
            "position": part["position"],
            "complete": bool(transcript),
            "extracts": [] if markdown else _extract(transcript),
            "unconfirmed_windows": part.get("review_windows", []),
        })
    return {"kind": "reviewed" if markdown else "extracts", "markdown": markdown, "parts": parts}
