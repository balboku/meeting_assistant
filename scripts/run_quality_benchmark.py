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


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


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


OMISSION_NOTICE_PATTERNS = (
    r"為節省篇幅",
    r"省略[^。\n]{0,20}逐字稿",
    r"逐字稿[^。\n]{0,20}省略",
    r"已過濾[^。\n]{0,20}逐字稿",
    r"自動過濾後續重複內容",
    r"只保留摘要化片段",
)


def _omission_notice(transcript: str) -> str:
    for pattern in OMISSION_NOTICE_PATTERNS:
        match = re.search(pattern, transcript, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def _normalize_turn_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return re.sub(r"[，。,.、；;：:！!？?\-—~「」『』（）()\[\]【】\"'`*_]+", "", normalized)


def _turn_texts(transcript: str) -> list[str]:
    turns: list[str] = []
    pattern = re.compile(
        r"\[\d{1,3}:[0-5]\d\]\s*(?:\*\*\[[^\]]+\]\*\*|\[[^\]]+\])?\s*[：:]?\s*(?P<text>.+)"
    )
    for line in transcript.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        normalized = _normalize_turn_text(match.group("text"))
        if len(normalized) >= 8:
            turns.append(normalized)
    return turns


def _max_consecutive_repeated_turns(transcript: str) -> tuple[int, str]:
    max_run = 0
    max_text = ""
    current_text = ""
    current_run = 0
    for text in _turn_texts(transcript):
        if text == current_text:
            current_run += 1
        else:
            current_text = text
            current_run = 1
        if current_run > max_run:
            max_run = current_run
            max_text = text
    return max_run, max_text


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
    omission_notice = _omission_notice(transcript)
    max_repeated_turns, repeated_turn_text = _max_consecutive_repeated_turns(transcript)
    repeated_turn_limit = int(expected.get("max_consecutive_repeated_turns", 3))

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
        _check(not omission_notice, "transcript_has_no_omission_notice", 3, omission_notice),
        _check(
            max_repeated_turns <= repeated_turn_limit,
            "transcript_has_no_repeated_turn_loop",
            3,
            f"max_run={max_repeated_turns}; limit={repeated_turn_limit}; text={repeated_turn_text[:40]}",
        ),
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
            "omission_notice": omission_notice,
            "max_consecutive_repeated_turns": max_repeated_turns,
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


def _markdown_files(scan_dir: Path, recursive: bool = False, limit: int = 0) -> list[Path]:
    pattern = "**/*.md" if recursive else "*.md"
    paths = [path for path in scan_dir.glob(pattern) if path.is_file()]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:limit] if limit > 0 else paths


def run_scan_dir(
    scan_dir: Path,
    min_score: float,
    *,
    recursive: bool = False,
    limit: int = 0,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = _markdown_files(scan_dir, recursive=recursive, limit=limit)
    results = []
    for markdown_path in paths:
        result = score_markdown(_read_text(markdown_path), expected or {})
        result.update({"id": markdown_path.stem, "markdown_path": str(markdown_path)})
        results.append(result)
    average = round(sum(result["score"] for result in results) / len(results), 1) if results else 0.0
    return {
        "scan_dir": str(scan_dir),
        "recursive": recursive,
        "case_count": len(results),
        "average_score": average,
        "min_score": min_score,
        "passed": bool(results) and all(result["score"] >= min_score for result in results),
        "results": results,
    }


def format_summary(report: dict[str, Any]) -> str:
    lines = [
        (
            f"passed={report['passed']} "
            f"case_count={report['case_count']} "
            f"average_score={report['average_score']} "
            f"min_score={report['min_score']}"
        )
    ]
    for result in report.get("results", []):
        failed = ", ".join(item["name"] for item in result.get("failed", [])) or "-"
        path = result.get("markdown_path", "")
        lines.append(f"{result['score']:>5.1f}  {result['id']}  failed={failed}  path={path}")
    return "\n".join(lines)


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Run offline meeting-note quality benchmark.")
    parser.add_argument("manifest", nargs="?", type=Path, help="Path to benchmark manifest JSON.")
    parser.add_argument("--scan-dir", type=Path, help="Scan generated Markdown files in a directory.")
    parser.add_argument("--recursive", action="store_true", help="Recursively scan --scan-dir for Markdown files.")
    parser.add_argument("--limit", type=int, default=0, help="Limit scan mode to the newest N Markdown files.")
    parser.add_argument("--min-score", type=float, default=80.0, help="Minimum passing score per case.")
    parser.add_argument("--format", choices=("json", "summary"), default="json", help="Output format.")
    args = parser.parse_args()

    if args.scan_dir:
        report = run_scan_dir(
            args.scan_dir,
            args.min_score,
            recursive=args.recursive,
            limit=max(0, args.limit),
        )
    else:
        if not args.manifest:
            parser.error("manifest is required unless --scan-dir is provided")
        report = run_manifest(args.manifest, args.min_score)
    if args.format == "summary":
        print(format_summary(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
