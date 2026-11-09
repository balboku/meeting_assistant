"""Organize literal course excerpts by a few auditable cues, without inference."""
from collections import defaultdict
import re

from backend.training_gap_review import SAMPLE_RATE

_TIME = re.compile(r"^\[(\d{1,4}:\d{2}(?::\d{2})?)\]\s*(\S.*)$")
_UNCERTAIN = re.compile(
    r"[\[（(【［<]\s*(?:聽不清[^\]）)】］>]*|聽不懂|不清楚|無法辨識|未確認|"
    r"inaudible|unintelligible|indistinct(?: chatter)?|unclear)\s*[\]）)】］>]", re.I)
_NOISE = re.compile(r"^(?:\[[^\]]+\]|\([^()]+\)|[（【][^）】]+[）】])\s*[。.!]?$")
_SPEAKER = re.compile(r"^(?:\*\*)?\[(?:發言者|講者|說話人|speaker)[^\]]*\](?:\*\*)?\s*[:：]?\s*", re.I)
_CUES = (
    ("提醒與限制", re.compile(r"注意|提醒|限制|避免|不要|不得|務必|風險|\b(?:avoid|caution|must not|limit)\b", re.I)),
    ("操作與示例", re.compile(r"步驟|操作|示範|例如|首先|點選|選擇|設定|輸入|\b(?:first|select|step|example|click)\b", re.I)),
    ("概念與說明", re.compile(r"定義|原理|目的是|意思是|作用是|用來|是指|差異|\b(?:means|definition|purpose)\b", re.I)),
)
_MAX_POINTS = 100


def _seconds(code):
    components = [int(piece) for piece in code.split(":")]
    if components[-1] >= 60 or (len(components) == 3 and components[-2] >= 60):
        return None
    seconds = 0
    for value in components:
        seconds = seconds * 60 + value
    return seconds


def _candidates(transcript, window):
    """Return only intact source lines, never join across an unconfirmed window."""
    for raw in transcript.splitlines():
        line = raw.strip()
        match = _TIME.fullmatch(line)
        seconds = _seconds(match[1]) if match else None
        if match and seconds is None:
            continue
        if window is not None and (seconds is None or not window.start <= seconds * SAMPLE_RATE < window.end):
            continue
        text = _SPEAKER.sub("", match[2] if match else line).strip()
        if _UNCERTAIN.search(text):
            yield seconds, "", True
        elif 2 <= len(text) <= 620 and len(line) <= 700 and not text.startswith(("#", chr(96) * 3, "|", ">")) and not _NOISE.fullmatch(text):
            yield seconds, line, False


def _spread(values, count):
    if len(values) <= count:
        return values
    return [values[i * (len(values) - 1) // (count - 1)] for i in range(count)]


def extract_notes(sources):
    by_topic = defaultdict(list)
    seen = set()
    points = set()
    untimed = False
    for window, transcript in sources:
        for seconds, line, uncertain in _candidates(transcript, window):
            if uncertain:
                if seconds is None:
                    untimed = True
                elif len(points) < _MAX_POINTS:
                    points.add(seconds)
                elif seconds not in points:
                    untimed = True
                continue
            text = _SPEAKER.sub("", _TIME.sub(r"\2", line)).strip()
            key = re.sub(r"\s+", " ", text).casefold()
            if key in seen:
                continue
            seen.add(key)
            topic = next((name for name, rule in _CUES if rule.search(text)), "其他原文重點")
            by_topic[topic].append((len(seen), line))
    sections = []
    all_lines = []
    single_topic = len(by_topic) == 1
    for name in ("概念與說明", "操作與示例", "提醒與限制", "其他原文重點"):
        chosen = _spread(by_topic.get(name, []), 5 if single_topic else 2)
        if chosen:
            sections.append({"label": name, "lines": [line for _, line in chosen]})
            all_lines.extend(chosen)
    return [line for _, line in sorted(all_lines)], sections, sorted(points), untimed
