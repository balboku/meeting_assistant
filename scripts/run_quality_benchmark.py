#!/usr/bin/env python3
"""Offline meeting-note quality benchmark.

This script does not call any AI service.  It scores already-generated Markdown
against expectation manifests so prompt/model changes can be compared without
spending API quota.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading_terms: tuple[str, ...], next_terms: tuple[str, ...] = ()) -> str:
    heading_pattern = "|".join(re.escape(term) for term in heading_terms)
    next_pattern = "|".join(re.escape(term) for term in next_terms)
    if next_pattern:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*?)(?=^##\s*[^\n]*(?:{next_pattern})|\Z)"
    else:
        pattern = rf"^##\s*[^\n]*(?:{heading_pattern})[^\n]*\n(?P<body>.*)\Z"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group("body").strip() if match else ""


def _ids(text: str, prefix: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(prefix)}\d+\b", text))


def _check(condition: bool, name: str, weight: int = 1, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "weight": weight, "detail": detail}


def score_markdown(markdown: str, expected: dict[str, Any]) -> dict[str, Any]:
    summary = _section(markdown, ("討論摘要", "Discussion Summary"), ("最終決議", "Final Decisions"))
    decisions = _section(markdown, ("最終決議", "Final Decisions"), ("待辦事項", "Action Items"))
    actions = _section(markdown, ("待辦事項", "Action Items"), ("完整逐字稿", "Verbatim Transcript"))
    transcript = _section(markdown, ("完整逐字稿", "Verbatim Transcript"))

    summary_ids = _ids(summary, "D")
    decision_ids = _ids(decisions, "R")
    action_ids = _ids(actions, "A")
    all_discussion_refs = _ids(decisions + "\n" + actions, "D")
    decision_refs_in_actions = _ids(actions, "R")
    timestamp_count = len(re.findall(r"\[\d{1,3}:[0-5]\d\]", transcript))
    segment_heading_count = len(re.findall(r"(?m)^#{1,6}\s*(?:【第\s*\d+\s*段|\[Segment\s+\d+)", transcript))

    checks: list[dict[str, Any]] = [
        _check(bool(summary.strip()), "has_discussion_summary", 3),
        _check(bool(decisions.strip()), "has_final_decisions", 3),
        _check(bool(actions.strip()), "has_action_items", 3),
        _check(bool(transcript.strip()), "has_transcript", 3),
        _check(bool(summary_ids), "summary_uses_d_ids", 2),
        _check(all(ref in summary_ids for ref in all_discussion_refs), "decisions_actions_link_existing_d_ids", 2),
        _check(bool(action_ids) or "未提及" in actions, "actions_are_explicit", 1),
        _check(all(ref in decision_ids for ref in decision_refs_in_actions), "actions_link_existing_r_ids", 1),
        _check(timestamp_count >= int(expected.get("min_timecodes", 1)), "transcript_has_enough_timecodes", 2, str(timestamp_count)),
        _check(segment_heading_count >= int(expected.get("min_segments", 0)), "transcript_has_expected_segments", 1, str(segment_heading_count)),
    ]

    for term in expected.get("required_terms", []):
        checks.append(_check(term in markdown, f"required_term:{term}", 2))
    for term in expected.get("forbidden_terms", []):
        checks.append(_check(term not in markdown, f"forbidden_term:{term}", 2))
    for topic in expected.get("required_discussion_topics", []):
        checks.append(_check(topic in summary, f"required_topic:{topic}", 2))

    total_weight = sum(item["weight"] for item in checks)
    passed_weight = sum(item["weight"] for item in checks if item["passed"])
    score = round((passed_weight / total_weight) * 100, 1) if total_weight else 0.0
    return {
        "score": score,
        "passed": [item for item in checks if item["passed"]],
        "failed": [item for item in checks if not item["passed"]],
        "metrics": {
            "discussion_ids": sorted(summary_ids),
            "decision_ids": sorted(decision_ids),
            "action_ids": sorted(action_ids),
            "timestamp_count": timestamp_count,
            "segment_heading_count": segment_heading_count,
        },
    }


def run_manifest(manifest_path: Path, min_score: float) -> dict[str, Any]:
    manifest = json.loads(_read_text(manifest_path))
    cases = manifest.get("cases", [])
    root = manifest_path.parent
    results = []
    for case in cases:
        markdown_path = Path(case["markdown_path"])
        if not markdown_path.is_absolute():
            markdown_path = root / markdown_path
        result = score_markdown(_read_text(markdown_path), case.get("expected", {}))
        result.update({"id": case.get("id") or markdown_path.stem, "markdown_path": str(markdown_path)})
        results.append(result)
    average = round(sum(result["score"] for result in results) / len(results), 1) if results else 0.0
    return {
        "manifest": str(manifest_path),
        "case_count": len(results),
        "average_score": average,
        "min_score": min_score,
        "passed": all(result["score"] >= min_score for result in results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline meeting-note quality benchmark.")
    parser.add_argument("manifest", type=Path, help="Path to benchmark manifest JSON.")
    parser.add_argument("--min-score", type=float, default=80.0, help="Minimum passing score per case.")
    args = parser.parse_args()

    report = run_manifest(args.manifest, args.min_score)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
