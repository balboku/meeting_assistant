"""Read/write visual supplements without modifying transcript or decision fields."""
from __future__ import annotations

from collections import defaultdict
import re


_DISCUSSION = re.compile(r"^##[ \t]+[^\n]*(?:討論摘要|Discussion Summary)[^\n]*$", re.M | re.I)
_NEXT_SECTION = re.compile(r"^##[ \t]+", re.M)
_TOPIC = re.compile(r"^###[ \t]+(D\d+)\.[ \t]+([^\n]+)$", re.M)
_RULE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.M)
VISUAL_LINE = re.compile(
    r"- 畫面補充(（待確認）)?：(.+?)｜畫面來源：(.+?)｜錄影：\[(\d{2,3}:[0-5]\d)\]"
    r"(?:｜逐字稿：(.+?))?"
)


def _topic_blocks(content: str):
    start = _DISCUSSION.search(content)
    if start is None:
        return
    end = _NEXT_SECTION.search(content, start.end())
    if end is None:
        return
    stop = end.start()
    if rule := _RULE.search(content, start.end(), stop):
        stop = rule.start()
    headings = list(_TOPIC.finditer(content, start.end(), stop))
    for index, heading in enumerate(headings):
        boundary = headings[index + 1].start() if index + 1 < len(headings) else stop
        yield heading[1], heading[2].strip(), heading.end(), boundary


def topics_for_visual_analysis(content: str) -> dict[str, str]:
    blocks = list(_topic_blocks(content))
    if len({key for key, *_ in blocks}) != len(blocks):
        return {}
    return {key: f"{title}\n{content[start:end].strip()[:900]}".rstrip()
            for key, title, start, end in blocks}


def read_visual_notes(content: str) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for key, _, start, end in _topic_blocks(content):
        for line in content[start:end].splitlines():
            match = VISUAL_LINE.fullmatch(line.strip())
            if match and bool(match[1]) == bool(match[5]):
                pending, observed, source, code, quote = match.groups()
                minutes, seconds = map(int, code.split(":"))
                grouped[key].append({"observed": observed, "source": source,
                                     "timecode": code, "seconds": minutes * 60 + seconds,
                                     "pending": bool(pending), "transcript_quote": quote})
    return dict(grouped)


def visual_note_line(note: dict) -> str:
    pending = "（待確認）" if note["pending"] else ""
    quote = f"｜逐字稿：{note['transcript_quote']}" if note["pending"] else ""
    return (f"- 畫面補充{pending}：{note['observed']}｜畫面來源：{note['source']}"
            f"｜錄影：[{note['timecode']}]{quote}")


def insert_visual_notes(content: str, notes: dict[str, list[dict]]) -> str:
    insertions = []
    for key, _, start, end in _topic_blocks(content):
        if key in notes:
            offset = start + len(content[start:end].rstrip())
            insertions.append((offset, "\n" + "\n".join(map(visual_note_line, notes[key]))))
    for offset, text in reversed(insertions):
        content = content[:offset] + text + content[offset:]
    return content
