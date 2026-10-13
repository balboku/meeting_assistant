"""Dedicated speech endpoint, kept separate from generic generateContent models."""
from __future__ import annotations

import math
import os
import re
from types import SimpleNamespace

import httpx
from opencc import OpenCC

_TRADITIONAL_CHINESE = OpenCC("s2t")

MODEL = "gemini-3.5-transcribe"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"


def _seconds(value):
    result = float(str(value).removesuffix("s"))
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid transcription timestamp")
    return result


def render_transcription(payload, *, segment_index=0, total_segments=1, audio_scope=""):
    """Use only returned speech annotations; never invent times or speaker identity."""
    if payload.get("status") != "completed":
        raise ValueError("Dedicated transcription did not complete")
    words = []
    for step in payload.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content") or []:
            if content.get("type") == "text":
                annotations = [w for w in content.get("annotations") or [] if w.get("type") == "word_info"]
                if content.get("text"):
                    original = str(content["text"]).encode("utf-8")
                    # Returned indices are UTF-8 byte offsets, not Python
                    # character indices. Keep provider punctuation/spacing.
                    if annotations and all("start_index" in w and "end_index" in w for w in annotations):
                        cursor = 0
                        for word in annotations:
                            start, end = int(word["start_index"]), int(word["end_index"])
                            if not 0 <= cursor <= start <= end <= len(original):
                                raise ValueError("Invalid transcription text offsets")
                            if re.sub(r"[\s，。,.!?;:、；：！？…—\-()（）\"“”'’]+", "", original[cursor:start].decode("utf-8")):
                                raise ValueError("Transcription annotations skipped source words")
                            words.append(dict(word, text=original[cursor:end].decode("utf-8")))
                            cursor = end
                        if original[cursor:].strip():
                            raise ValueError("Transcription annotations omitted source text")
                        continue
                    if re.sub(r"\s", "", "".join(w.get("text", "") for w in annotations)) != re.sub(r"\s", "", content["text"]):
                        raise ValueError("Incomplete transcription word annotations")
                words.extend(annotations)
    if not words:
        raise ValueError("Dedicated transcription omitted required speaker/timestamp annotations")
    labels, lines, group = {}, [], []
    previous_time = -1.0

    def flush():
        if not group:
            return
        start, speaker, _ = group[0]
        label = labels.get(speaker, "發言者不明")
        text = "".join(word for _, _, word in group)
        lines.append(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] **[{label}]**：{text}")
        group.clear()

    for word in words:
        start, end = _seconds(word.get("start_offset")), _seconds(word.get("end_offset"))
        text = word.get("text")
        if not isinstance(text, str) or end < start or start < previous_time:
            raise ValueError("Invalid transcription word annotations")
        previous_time = start
        speaker = word.get("speaker") or ""
        if speaker and speaker not in labels:
            if not re.fullmatch(r"(?:spk_[1-8]|spk:[0-7])", speaker):
                raise ValueError("Invalid anonymous speaker identifier")
            # IDs are local to each request. Qualify multi-slice speakers rather
            # than falsely merging spk_1 into the same person across a meeting.
            scope = f"-{audio_scope}" if re.fullmatch(r"[a-f0-9]{8}", audio_scope) else ""
            suffix = f"（片段 {segment_index + 1}{scope}）" if total_segments > 1 or scope else ""
            labels[speaker] = f"發言者 {chr(65 + len(labels))}{suffix}"
        if group and (speaker != group[0][1] or start - group[0][0] >= 15):
            flush()
        if group and group[-1][2] and text and group[-1][2][-1].isascii() and text[0].isascii():
            if group[-1][2][-1].isalnum() and text[0].isalnum():
                text = " " + text
        group.append((start, speaker, text))
    flush()
    # Script conversion only; no cloud translation, terminology rewrite,
    # number conversion or English rewriting. Original audio is retained.
    return _TRADITIONAL_CHINESE.convert("\n\n".join(lines))


def generate_transcription(client, uploaded, *, segment_index=0, total_segments=1, audio_scope=""):
    # REST is official and avoids dropping new transcription_config fields in
    # the pinned SDK's older Interactions schema. SDK still manages file upload.
    api_key = getattr(getattr(client, "_api_client", None), "api_key", None) or os.getenv("GEMINI_API_KEY")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("Missing Gemini API credential")
    body = {
        "model": MODEL,
        "input": [{"type": "audio", "uri": uploaded.uri, "mime_type": uploaded.mime_type}],
        "generation_config": {"transcription_config": {
            "mode": {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]},
        }},
        "store": False,
    }
    with httpx.Client(timeout=float(os.getenv("GENAI_HTTP_TIMEOUT_SECONDS", "180"))) as transport:
        response = transport.post(ENDPOINT, headers={"x-goog-api-key": api_key}, json=body)
    if response.status_code >= 400:
        # No headers/request objects in errors (they contain credentials).
        raise RuntimeError(f"{response.status_code} Transcribe: {response.text[:4000]}")
    return SimpleNamespace(text=render_transcription(response.json(), segment_index=segment_index,
                                                   total_segments=total_segments, audio_scope=audio_scope))
