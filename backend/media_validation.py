"""
Small media magic-byte checks for uploaded audio/video files.

The backend still relies on Gemini for actual decoding. This only blocks obvious
extension spoofing before files reach the queue.
"""

from __future__ import annotations


def _is_mp3(header: bytes) -> bool:
    return header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
    )


def _is_wav(header: bytes) -> bool:
    return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE"


def _is_flac(header: bytes) -> bool:
    return header.startswith(b"fLaC")


def _is_ogg(header: bytes) -> bool:
    return header.startswith(b"OggS")


def _is_mp4_family(header: bytes) -> bool:
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _is_webm_or_mkv(header: bytes) -> bool:
    return header.startswith(b"\x1a\x45\xdf\xa3")


def _is_aac(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF0) == 0xF0


MAGIC_CHECKS = {
    ".mp3": _is_mp3,
    ".wav": _is_wav,
    ".m4a": _is_mp4_family,
    ".mp4": _is_mp4_family,
    ".mov": _is_mp4_family,
    ".aac": _is_aac,
    ".flac": _is_flac,
    ".ogg": _is_ogg,
    ".webm": _is_webm_or_mkv,
    ".mkv": _is_webm_or_mkv,
}


def validate_media_magic(suffix: str, header: bytes) -> str | None:
    """Return an error message when header bytes do not match the extension."""
    checker = MAGIC_CHECKS.get(suffix.lower())
    if checker is None:
        return None
    if checker(header):
        return None
    return "檔案內容與副檔名不符，請確認媒體檔格式後再上傳。"
