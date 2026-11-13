"""Fail closed unless the source label and the claimed values are visible in that frame."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata


_OCR_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "visual_frame_ocr.ps1"
_WAITING = ("正在等待其他人加入", "等待其他人加入", "等待會議主持人", "waitingforotherstojoin",
            "waitingforthehost", "waitingforotherparticipantstojoin")
_NUMBERS = re.compile(r"(?<![\d.])[+−-]?\d+(?:[.,]\d+)*(?:[ \t]*%)?")
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "          ")


def _compact(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _numbers(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text.translate(_SUPERSCRIPTS))
    # OCR frequently inserts a space after a decimal point, but not across lines.
    normalized = re.sub(r"(?<=\d)([.,])[ \t]+(?=\d)", r"\1", normalized)
    return {m[0].replace("−", "-").replace(",", "").replace(" ", "").replace("\t", "").lstrip("+")
            for m in _NUMBERS.finditer(normalized)}


def note_has_frame_support(note: dict, text: str) -> bool:
    visible = _compact(text)
    source = _compact(note["source"])
    if len(source) < 3 or source not in visible or any(term in visible for term in _WAITING):
        return False
    numbers = _numbers(note["observed"])
    if numbers:
        return numbers <= _numbers(text)
    observed = _compact(note["observed"])
    return len(observed) >= 3 and observed in visible


def grounded_notes(notes: dict, images: dict[str, bytes], *, frame_key: str = "frame_id") -> dict:
    cited = {note[frame_key] for group in notes.values() for note in group if note.get(frame_key) in images}
    text_by_frame = read_frame_text({key: data for key, data in images.items() if key in cited})
    result = {}
    for topic, group in notes.items():
        supported = [note for note in group if note_has_frame_support(note, text_by_frame.get(note.get(frame_key), ""))]
        if supported:
            result[topic] = supported
    return result


def read_frame_text(frames: dict[str, bytes]) -> dict[str, str]:
    """Local OCR only. No engine, language pack, or reliable output means no evidence."""
    if not frames or len(frames) > 8 or any(len(data) > 400_000 for data in frames.values()):
        return {}
    try:
        if os.name == "nt":
            body = json.dumps({key: base64.b64encode(data).decode("ascii") for key, data in frames.items()})
            output = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(_OCR_SCRIPT)],
                input=body.encode("ascii"), capture_output=True, timeout=25, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            result = json.loads(output)
            return {key: value for key, value in result.items() if key in frames and isinstance(value, str)}
        if command := shutil.which("tesseract"):
            result = {}
            for key, data in frames.items():
                try:
                    result[key] = subprocess.run(
                        [command, "stdin", "stdout", "-l", os.getenv("MEETING_VISUAL_OCR_LANG", "chi_tra+eng"),
                         "--psm", "11"], input=data, capture_output=True, timeout=3, check=True,
                    ).stdout.decode("utf-8", errors="replace")
                except subprocess.SubprocessError:
                    continue
            return result
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    return {}
