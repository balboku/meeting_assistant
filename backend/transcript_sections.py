"""Keep original utterances separate from reference translations and evidence."""

import re
from typing import Optional

from backend.transcript_translation import invalidate_translation_section


TRANSCRIPT_SECTION_HEADING = "## 📝 四、完整逐字稿 (Verbatim Transcript)"
TRANSCRIPT_SECTION_PATTERN = re.compile(
    r"^##\s*[^\n]*(?:Verbatim Transcript|完整逐字稿|逐字稿|蝔)[^\n]*\n",
    re.MULTILINE | re.IGNORECASE,
)
NEXT_TOP_LEVEL_SECTION_PATTERN = re.compile(r"^##\s+", re.MULTILINE)


def _replace_transcript_section(meeting_content: str, full_transcript: str) -> str:
    """Force the original transcript to remain verbatim, preserving appendices."""
    content = meeting_content or ""
    transcript = (full_transcript or "").strip()
    match = TRANSCRIPT_SECTION_PATTERN.search(content)
    if match:
        prefix = content[:match.start()].rstrip()
        suffix = _extract_post_transcript_sections(content)
        if _canonical_transcript_text(_extract_transcript_section_body(content) or "") != _canonical_transcript_text(transcript):
            suffix = invalidate_translation_section(suffix)
    else:
        prefix, suffix = content.rstrip(), ""
    prefix = re.sub(r"\n-{3,}\s*$", "", prefix).rstrip()
    separator = "\n\n---\n\n" if prefix else ""
    result = f"{prefix}{separator}{TRANSCRIPT_SECTION_HEADING}\n{transcript}\n"
    if suffix:
        result = f"{result.rstrip()}\n\n{suffix.rstrip()}\n"
    return result


def _canonical_transcript_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines()).strip()


def _extract_post_transcript_sections(meeting_content: str) -> str:
    match = TRANSCRIPT_SECTION_PATTERN.search(meeting_content or "")
    if not match:
        return ""
    next_section = NEXT_TOP_LEVEL_SECTION_PATTERN.search(meeting_content or "", match.end())
    if not next_section:
        return ""
    return (meeting_content or "")[next_section.start():].strip()


def _extract_transcript_section_body(meeting_content: str) -> Optional[str]:
    match = TRANSCRIPT_SECTION_PATTERN.search(meeting_content or "")
    if not match:
        return None
    next_section = NEXT_TOP_LEVEL_SECTION_PATTERN.search(meeting_content or "", match.end())
    end = next_section.start() if next_section else len(meeting_content or "")
    return (meeting_content or "")[match.end():end].strip()
