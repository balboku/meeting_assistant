"""Optional CPU-only speech models. Inference never downloads or contacts a service."""
from __future__ import annotations

import importlib.util
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from backend.speech_process import SpeechWorkerError, SpeechWorkerTimeout, run_speech, timeout_seconds

ACTIVE_SPEECH = ContextVar("meeting_speech", default=None)


def model_paths():
    root = Path(os.getenv("MEETING_LOCAL_MODELS", Path(__file__).resolve().parents[1] / "models"))
    return {"segmentation": root / "segmentation.onnx", "embedding": root / "embedding.onnx",
            "encoder": root / "whisper-encoder.onnx", "decoder": root / "whisper-decoder.onnx",
            "tokens": root / "whisper-tokens.txt"}


def capabilities():
    installed = importlib.util.find_spec("sherpa_onnx") is not None
    paths = model_paths()
    missing = [name for name, path in paths.items() if not path.is_file()]
    diarization = installed and not any(name in missing for name in ("segmentation", "embedding"))
    return {"diarization": diarization, "local_transcription": installed and not missing,
            "missing": missing, "runtime_installed": installed,
            "message": "本地語音模型已就緒。" if installed and not missing else "本地模型尚未就緒，請執行管理員的本地模型安裝步驟。"}


def processing_profile():
    """Invalidate stage caches if model installation or clustering settings change."""
    files = {}
    for name, path in model_paths().items():
        if name in {"segmentation", "embedding"}:
            stat = path.stat() if path.is_file() else None
            files[name] = [str(path), stat.st_size, stat.st_mtime_ns] if stat else None
    return {"version": 3, "enabled": os.getenv("MEETING_DIARIZATION_ENABLED", "1"),
            "timeout_seconds": timeout_seconds("diarize"),
            "threshold": os.getenv("SPEAKER_CLUSTER_THRESHOLD", "0.9"), "models": files}


@contextmanager
def speech_context(path):
    token = ACTIVE_SPEECH.set({"path": Path(path), "report": {"status": "pending"}})
    try:
        yield
    finally:
        ACTIVE_SPEECH.reset(token)


def set_speech_check(check):
    state = ACTIVE_SPEECH.get()
    if state is not None:
        state["check_cancelled"] = check


def infer_segments(path):
    check = (ACTIVE_SPEECH.get() or {}).get("check_cancelled", lambda: None)
    return run_speech("diarize", path, models=model_paths(), check_cancelled=check)


TURN = re.compile(r"^(\[(\d{1,3}):([0-5]\d)\]\s*\*{0,2})\[[^\]\n]+\](\*{0,2}\s*[：:]\s*.*)$", re.MULTILINE)


def align_speakers(transcript, segments):
    """Only replace labels; ambiguous/mixed intervals never receive a single identity."""
    matches = list(TURN.finditer(transcript))
    labels = {}
    for segment in sorted(segments, key=lambda s: s["start"]):
        if segment["speaker"] not in labels:
            number = len(labels)
            labels[segment["speaker"]] = f"發言者 {chr(65 + number) if number < 26 else number + 1}"
    replacements = []
    for i, match in enumerate(matches):
        start = int(match[2]) * 60 + int(match[3])
        following = int(matches[i + 1][2]) * 60 + int(matches[i + 1][3]) if i + 1 < len(matches) else max(
            (segment["end"] for segment in segments), default=start + 10)
        # Include the entire turn, including the tail of 25-second offline ASR
        # chunks. Examining only its first seconds can falsely label mixed speech.
        end = max(start + 0.5, following)
        durations = {}
        for segment in segments:
            overlap = max(0, min(end, segment["end"]) - max(start, segment["start"]))
            if overlap:
                durations[segment["speaker"]] = durations.get(segment["speaker"], 0) + overlap
        ranked = sorted(durations.items(), key=lambda item: -item[1])
        total = sum(durations.values())
        label = "發言者不明"
        if ranked and total >= 0.35 * (end - start):
            if ranked[0][1] / total >= 0.8:
                label = labels[ranked[0][0]]
            elif len(ranked) > 1:
                label = "多人／切換待確認"
        replacements.append((match.start(), match.end(), f"{match[1]}[{label}]{match[4]}"))
    for start, end, text in reversed(replacements):
        transcript = transcript[:start] + text + transcript[end:]
    return transcript


def diarize_transcript(transcript):
    state = ACTIVE_SPEECH.get()
    if state is None:
        return transcript
    if os.getenv("MEETING_DIARIZATION_ENABLED", "1") == "0":
        state["report"] = {"status": "disabled"}
        return transcript
    if "segments" not in state:
        if not capabilities()["diarization"]:
            state.update(segments=None, report={"status": "unavailable", "message": "未安裝本地分群模型；片段代號不代表跨片段同一人。"})
        else:
            try:
                state["segments"] = infer_segments(state["path"])
                state["report"] = {"status": "completed", "method": "local-acoustic-clustering",
                    "speakers": len({s["speaker"] for s in state["segments"]}),
                    "segments": state["segments"], "identity_scope": "this_meeting_only"}
                if not state["segments"]:
                    state["report"].update(status="no_speech", message="分群未取得有效人聲區間，發言人保留不明，請人工核對。")
            except (SpeechWorkerError, OSError, ValueError) as exc:
                status = "timed_out" if isinstance(exc, SpeechWorkerTimeout) else "failed"
                state.update(segments=[], report={"status": status,
                    "message": "發言人辨識逾時，已保留逐字稿並繼續整理；發言人待人工確認。" if status == "timed_out"
                    else "發言人辨識未完成，已保留逐字稿並繼續整理；發言人待人工確認。",
                    "error_type": type(exc).__name__, "timeout_seconds": timeout_seconds("diarize")})
    return align_speakers(transcript, state["segments"]) if state["segments"] is not None else transcript


def speech_report():
    return dict((ACTIVE_SPEECH.get() or {}).get("report") or {"status": "not_run"})


def transcribe_locally(path, *, check_cancelled=lambda: None):
    if not capabilities()["local_transcription"]:
        raise RuntimeError(capabilities()["message"])
    set_speech_check(check_cancelled)
    text = run_speech("transcribe", path, models=model_paths(), check_cancelled=check_cancelled)
    return diarize_transcript(text)
