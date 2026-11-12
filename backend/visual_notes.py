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
    r"(?:｜逐字稿：(.+?))?(?: <!-- visual-frame:([0-9a-f]{64}) -->)?"
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


def _indexed_notes(content: str):
    for key, _, start, end in _topic_blocks(content):
        first_line = len(content[:start].splitlines()) - 1
        for index, line in enumerate(content[start:end].splitlines(), start=first_line):
            match = VISUAL_LINE.fullmatch(line.strip())
            if match and bool(match[1]) == bool(match[5]):
                pending, observed, source, code, quote, frame = match.groups()
                minutes, seconds = map(int, code.split(":"))
                note = {"observed": observed, "source": source,
                        "timecode": code, "seconds": minutes * 60 + seconds,
                        "pending": bool(pending), "transcript_quote": quote}
                if frame:
                    note["frame_sha256"] = frame
                yield key, index, note


def read_visual_notes(content: str) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for key, _, note in _indexed_notes(content):
        grouped[key].append(note)
    return dict(grouped)


def cited_notes_by_line(content: str) -> dict[int, dict]:
    return {index: note for _, index, note in _indexed_notes(content) if note.get("frame_sha256")}


def visual_note_line(note: dict, *, include_frame: bool = False) -> str:
    pending = "（待確認）" if note["pending"] else ""
    quote = f"｜逐字稿：{note['transcript_quote']}" if note["pending"] else ""
    frame = f" <!-- visual-frame:{note['frame_sha256']} -->" if include_frame and note.get("frame_sha256") else ""
    return (f"- 畫面補充{pending}：{note['observed']}｜畫面來源：{note['source']}"
            f"｜錄影：[{note['timecode']}]{quote}{frame}")


def referenced_frames(content: str) -> set[str]:
    return {note["frame_sha256"] for group in read_visual_notes(content).values()
            for note in group if "frame_sha256" in note}


def strip_frame_markers(content: str) -> str:
    return re.sub(r" <!-- visual-frame:[0-9a-f]{64} -->(?=\r?$)", "", content, flags=re.M)


def insert_visual_notes(content: str, notes: dict[str, list[dict]]) -> str:
    insertions = []
    for key, _, start, end in _topic_blocks(content):
        if key in notes:
            offset = start + len(content[start:end].rstrip())
            insertions.append((offset, "\n" + "\n".join(
                visual_note_line(note, include_frame=True) for note in notes[key])))
    for offset, text in reversed(insertions):
        content = content[:offset] + text + content[offset:]
    return content
