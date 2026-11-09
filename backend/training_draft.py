"""Read-only course draft: reviewed notes, or literal extracts without inference."""

import hashlib
import json
from pathlib import Path

from backend.training_draft_extract import extract_notes
from backend.training_draft_sources import DraftReadBudget, reviewed_slices, uncovered, verified_slices
from backend.training_gap_review import SAMPLE_RATE, project_review_windows, review_file

MAX_DRAFT_BYTES = 128 * 1024


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


def _automatic_part(output_dir, course_id, part, review, budget):
    complete = bool(part["transcript"].strip())
    windows = part.get("review_windows", [])
    untimed = False
    if complete:
        sources = [(None, part["transcript"])]
    else:
        extent, sources = verified_slices(output_dir, part, budget)
        if review is not None:
            sources = [(window, text) for window, text in sources
                       if any(w.start <= window.start and window.end <= w.end for w in review.retained)
                       and not any(window.overlaps(w) for w in review.pending)]
            sources = sorted([*sources, *reviewed_slices(output_dir, course_id, part, review, budget)])
        elif extent is not None:
            windows = [{"start_seconds": w.start / SAMPLE_RATE, "end_seconds": w.end / SAMPLE_RATE}
                       for w in uncovered(extent, sources)]
        untimed = not windows
    extracts, sections, points, unknown_markers = extract_notes(sources)
    return {"id": part["id"], "position": part["position"], "complete": complete,
            "basis": "complete" if complete else "accepted" if extracts else "none",
            "extracts": extracts, "sections": sections, "unconfirmed_windows": windows,
            "unconfirmed_points": points, "unconfirmed_untimed": untimed or unknown_markers}


def build_draft(output_dir: Path, course: dict) -> dict:
    if course["notes_current"]:
        return {"kind": "official"}
    reviewed = {}
    review_sha256 = project_review_windows(output_dir, course, reviewed=reviewed)
    markdown = _reviewed_markdown(output_dir, course, review_sha256)
    if markdown:
        parts = [{"id": p["id"], "position": p["position"], "complete": bool(p["transcript"]),
                  "extracts": [], "unconfirmed_windows": p.get("review_windows", [])} for p in course["parts"]]
        return {"kind": "reviewed", "markdown": markdown, "parts": parts}
    budget = DraftReadBudget()
    parts = [_automatic_part(output_dir, course["id"], part, reviewed.get(part["id"]), budget)
             for part in course["parts"]]
    return {"kind": "extracts", "markdown": "", "parts": parts}
