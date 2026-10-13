"""Pure repeated-turn diagnostics for meeting detail; no runtime or API dependencies."""

import re
from typing import Optional


def transcript_turns(transcript: str) -> list[tuple[int, str, str]]:
    """Parse the existing explicit line-level turns without guessing speakers."""
    pattern = re.compile(
        r"^\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]\s*"
        r"(?:\*{1,2})?\[(?P<speaker>[^\]]+)\](?:\*{1,2})?\s*[：:]\s*(?P<text>.*)$",
        flags=re.MULTILINE,
    )
    return [(int(m["minutes"]) * 60 + int(m["seconds"]), m["speaker"].strip(), m["text"].strip())
            for m in pattern.finditer(transcript or "")]


def transcript_speaker_labels(transcript: str) -> list[str]:
    """Read labels, not voice identities: Markdown emphasis is optional.

    A timestamp may appear inside a paragraph in legacy output. Only labels at
    a turn prefix or line start count; names quoted in speech are not speakers.
    Missing labels remain missing rather than inheriting the preceding voice.
    """
    prefix = re.compile(
        r"(?:\[\d{1,3}:[0-5]\d(?::[0-5]\d)?\][ \t]*|^[ \t]*)"
        r"\*{0,2}\[([^\]\n]+)\]\*{0,2}[ \t]*[：:]",
        re.MULTILINE,
    )
    non_speakers = {"聽不清", "台語音訊不清晰", "發言者不明", "多人重疊"}
    labels = {re.sub(r"\s+", " ", m[1]).strip() for m in prefix.finditer(transcript or "")}
    return sorted(label for label in labels if label and label not in non_speakers)


def _normalize_detail_turn_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return re.sub(r"[，。,.、；;：:！!？?\-—~「」『』（）()\[\]【】\"'`*_]+", "", normalized)


def _detail_format_clock(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds or 0)), 60)
    return f"{minutes:02d}:{seconds:02d}"


_DETAIL_TRANSCRIPT_SEGMENT_HEADING_PATTERN = re.compile(
    r"^(?:#{1,6}\s*)?(?:"
    r"【第\s*(?P<zh_index>\d+)\s*段\s*[｜|]\s*"
    r"(?P<zh_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<zh_end>\d{1,3}:[0-5]\d|end)】"
    r"|\[?Segment\s+(?P<en_index>\d+)(?:/\d+)?\s*[|｜]\s*"
    r"(?P<en_start>\d{1,3}:[0-5]\d)\s*[–—-]\s*(?P<en_end>\d{1,3}:[0-5]\d|end)\]?)\s*$",
    flags=re.IGNORECASE,
)


def _detail_clock_text_to_seconds(value: object) -> Optional[int]:
    match = re.fullmatch(r"\s*(\d{1,3}):([0-5]\d)\s*", str(value or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _detail_heading_segment(line: str) -> Optional[dict]:
    match = _DETAIL_TRANSCRIPT_SEGMENT_HEADING_PATTERN.match(str(line or "").strip())
    if not match:
        return None
    raw_index = match.group("zh_index") or match.group("en_index")
    raw_start = match.group("zh_start") or match.group("en_start")
    raw_end = match.group("zh_end") or match.group("en_end")
    try:
        index = int(raw_index) - 1
    except (TypeError, ValueError):
        return None
    start_seconds = _detail_clock_text_to_seconds(raw_start)
    end_seconds = _detail_clock_text_to_seconds(raw_end)
    if index < 0 or start_seconds is None:
        return None
    if end_seconds is None or end_seconds <= start_seconds:
        end_seconds = start_seconds + 1
    return {
        "index": index,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
    }


def _detail_time_based_segment_matches(start_time: int, end_time: int, segment_seconds: int) -> list[dict]:
    first_index = max(0, start_time // segment_seconds)
    last_index = max(first_index, end_time // segment_seconds)
    return [
        {
            "index": index,
            "start_seconds": index * segment_seconds,
            "end_seconds": (index + 1) * segment_seconds,
        }
        for index in range(first_index, last_index + 1)
    ]


def _detail_repetition_segment_matches(
    timestamps: list[int],
    segments: Optional[list[dict]] = None,
    block_segments: Optional[list[dict]] = None,
    *,
    segment_seconds: int,
) -> list[dict]:
    if not timestamps:
        return []

    def segment_metadata_by_index(index: int, fallback: dict) -> dict:
        for position, segment in enumerate(segments or []):
            try:
                segment_index = int(segment.get("index", position))
            except (TypeError, ValueError, AttributeError):
                continue
            if segment_index != index:
                continue
            try:
                start_seconds = int(segment.get("start_seconds", fallback.get("start_seconds", 0)))
                end_seconds = int(segment.get("end_seconds", fallback.get("end_seconds", start_seconds + 1)))
            except (TypeError, ValueError, AttributeError):
                start_seconds = int(fallback.get("start_seconds", index * segment_seconds))
                end_seconds = int(fallback.get("end_seconds", start_seconds + segment_seconds))
            if end_seconds <= start_seconds:
                end_seconds = start_seconds + 1
            return {
                "index": index,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            }
        return {
            "index": index,
            "start_seconds": int(fallback.get("start_seconds", index * segment_seconds)),
            "end_seconds": int(fallback.get("end_seconds", (index + 1) * segment_seconds)),
        }

    if block_segments:
        counts: dict[int, int] = {}
        fallback_by_index: dict[int, dict] = {}
        for block_segment in block_segments:
            if not isinstance(block_segment, dict):
                continue
            try:
                index = int(block_segment.get("index", -1))
            except (TypeError, ValueError):
                continue
            if index < 0:
                continue
            counts[index] = counts.get(index, 0) + 1
            fallback_by_index.setdefault(index, block_segment)
        if counts:
            best_count = max(counts.values())
            return [
                segment_metadata_by_index(index, fallback_by_index[index])
                for index in sorted(counts)
                if counts[index] == best_count
            ]

    start_time = min(timestamps)
    end_time = max(timestamps)
    matched: list[tuple[int, dict]] = []
    seen_indices: set[int] = set()
    if not segments:
        return _detail_time_based_segment_matches(start_time, end_time, segment_seconds)

    for position, segment in enumerate(segments):
        try:
            index = int(segment.get("index", position))
            start_seconds = int(segment.get("start_seconds", 0))
            end_seconds = int(segment.get("end_seconds", start_seconds + 1))
        except (TypeError, ValueError, AttributeError):
            continue
        if end_seconds <= start_seconds:
            end_seconds = start_seconds + 1
        hit_count = sum(1 for timestamp in timestamps if start_seconds <= timestamp < end_seconds)
        overlaps = (
            hit_count > 0
            or (start_seconds <= start_time < end_seconds)
            or (start_seconds < end_time < end_seconds)
        )
        if overlaps and index not in seen_indices:
            matched.append((
                hit_count,
                {
                    "index": index,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                },
            ))
            seen_indices.add(index)
    if not matched:
        return _detail_time_based_segment_matches(start_time, end_time, segment_seconds)
    best_hit_count = max(hit_count for hit_count, _ in matched)
    if best_hit_count <= 0:
        return [match for _, match in matched]
    return [match for hit_count, match in matched if hit_count == best_hit_count]


def _detail_repetition_location(
    timestamps: list[int],
    segments: Optional[list[dict]] = None,
    segment_matches: Optional[list[dict]] = None,
    *,
    segment_seconds: int,
) -> str:
    if not timestamps:
        return ""

    start_time = min(timestamps)
    end_time = max(timestamps)
    time_range = f"重複時間：{_detail_format_clock(start_time)}-{_detail_format_clock(end_time)}"
    matches = segment_matches
    if matches is None:
        matches = _detail_repetition_segment_matches(timestamps, segments, segment_seconds=segment_seconds)
    if not matches:
        return time_range

    labels = [
        f"第 {match['index'] + 1} 段｜"
        f"{_detail_format_clock(match['start_seconds'])}-{_detail_format_clock(match['end_seconds'])}"
        for match in matches
    ]
    if len(labels) > 3:
        segment_text = "、".join(labels[:3]) + f" 等 {len(labels)} 段"
    else:
        segment_text = "、".join(labels)
    return f"疑似分段：{segment_text}；{time_range}"


def _detail_repeated_turn_runs(transcript: str, *, limit: int = 3) -> list[dict]:
    pattern = re.compile(
        r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)\]\s*"
        r"(?:\*\*\[[^\]]+\]\*\*|\[[^\]]+\])?\s*[：:]?\s*(?P<text>.+)"
    )
    runs: list[dict] = []
    current_text = ""
    current_run = 0
    current_timestamps: list[int] = []
    current_block_segments: list[dict] = []
    current_heading_segment: Optional[dict] = None

    def flush_current() -> None:
        if current_run <= limit or not current_timestamps:
            return
        runs.append({
            "text": current_text,
            "run": current_run,
            "timestamps": list(current_timestamps),
            "block_segments": list(current_block_segments),
        })

    for line in (transcript or "").splitlines():
        heading_segment = _detail_heading_segment(line)
        if heading_segment is not None:
            current_heading_segment = heading_segment
            continue
        match = pattern.search(line)
        if not match:
            continue
        normalized = _normalize_detail_turn_text(match.group("text"))
        timestamp = int(match.group("minutes")) * 60 + int(match.group("seconds"))
        if len(normalized) < 8:
            flush_current()
            current_text = ""
            current_run = 0
            current_timestamps = []
            current_block_segments = []
            continue
        if normalized == current_text:
            current_run += 1
            current_timestamps.append(timestamp)
            if current_heading_segment is not None:
                current_block_segments.append(dict(current_heading_segment))
        else:
            flush_current()
            current_text = normalized
            current_run = 1
            current_timestamps = [timestamp]
            current_block_segments = [dict(current_heading_segment)] if current_heading_segment is not None else []
    flush_current()
    return runs


def transcript_repeated_turn_diagnostic(
    transcript: str,
    *,
    limit: int = 3,
    segments: Optional[list[dict]] = None,
    segment_seconds: int,
) -> Optional[dict]:
    runs = _detail_repeated_turn_runs(transcript, limit=limit)
    if not runs:
        return None

    descriptions: list[str] = []
    all_segment_matches: list[dict] = []
    seen_match_keys: set[tuple[int, str]] = set()

    def segment_location_text(matches: list[dict]) -> str:
        labels = [
            f"第 {match['index'] + 1} 段｜"
            f"{_detail_format_clock(match['start_seconds'])}-{_detail_format_clock(match['end_seconds'])}"
            for match in matches
        ]
        if not labels:
            return ""
        if len(labels) > 3:
            return "、".join(labels[:3]) + f" 等 {len(labels)} 段"
        return "、".join(labels)

    for run in runs:
        timestamps = run.get("timestamps") or []
        if not timestamps:
            continue
        run_length = int(run.get("run") or 0)
        preview = str(run.get("text") or "")[:24]
        segment_matches = _detail_repetition_segment_matches(
            timestamps,
            segments,
            block_segments=run.get("block_segments") or [],
            segment_seconds=segment_seconds,
        )
        repeat_text = f"同一句連續重複 {run_length} 次：{preview}"
        time_text = (
            f"重複時間：{_detail_format_clock(min(timestamps))}-"
            f"{_detail_format_clock(max(timestamps))}"
        )
        location = segment_location_text(segment_matches)
        if location:
            descriptions.append(f"問題位置：{location}：{repeat_text}；{time_text}")
        else:
            descriptions.append(f"{repeat_text}；{time_text}")
        issue = (
            f"疑似連續重複轉錄；同一句連續重複 {run_length} 次：{preview}"
            f"；重複時間：{_detail_format_clock(min(timestamps))}-{_detail_format_clock(max(timestamps))}"
        )
        for match in segment_matches:
            key = (int(match["index"]), issue)
            if key in seen_match_keys:
                continue
            seen_match_keys.add(key)
            all_segment_matches.append({**match, "issue": issue})

    if not descriptions:
        return None
    warning_descriptions = descriptions[:5]
    if len(descriptions) > 5:
        warning_descriptions.append(f"另有 {len(descriptions) - 5} 處")
    warning = (
        "逐字稿品質警示：疑似連續重複轉錄；"
        f"{'；'.join(warning_descriptions)}。"
        "建議重跑上述分段或複核相關內容。"
    )
    issue = (
        str(all_segment_matches[0].get("issue"))
        if all_segment_matches
        else "疑似連續重複轉錄"
    )
    return {
        "warning": warning,
        "issue": issue,
        "segment_matches": all_segment_matches,
        "segment_indices": [
            int(match["index"])
            for match in all_segment_matches
        ],
    }


def transcript_repeated_turn_warning(
    transcript: str,
    *,
    limit: int = 3,
    segments: Optional[list[dict]] = None,
    segment_seconds: int,
) -> Optional[str]:
    diagnostic = transcript_repeated_turn_diagnostic(
        transcript,
        limit=limit,
        segments=segments,
        segment_seconds=segment_seconds,
    )
    return diagnostic["warning"] if diagnostic else None
