"""Bound native inference to disposable processes, never the web server's GIL."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class SpeechWorkerError(RuntimeError):
    pass


class SpeechWorkerTimeout(SpeechWorkerError):
    pass


def timeout_seconds(operation):
    name, default = (("MEETING_DIARIZATION_TIMEOUT_SECONDS", 300) if operation == "diarize"
                     else ("MEETING_LOCAL_ASR_TIMEOUT_SECONDS", 3600))
    try:
        value = float(os.getenv(name, str(default)))
        return min(7200, max(1, value)) if math.isfinite(value) else default
    except ValueError:
        return default


def _run(command, *, deadline, check_cancelled, log, env=None):
    check_cancelled()
    # File-backed diagnostics cannot fill a pipe and deadlock the parent.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                               env=env, creationflags=flags)
    try:
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SpeechWorkerTimeout("本地語音處理超過時間上限。")
            try:
                code = process.wait(timeout=min(0.5, remaining))
            except subprocess.TimeoutExpired:
                continue
            check_cancelled()
            if code:
                raise SpeechWorkerError(f"本地語音子程序未完成（exit={code}）。")
            return
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def run_speech(operation, path, *, models, check_cancelled=lambda: None):
    if operation not in {"diarize", "transcribe"}:
        raise ValueError("Unsupported local speech operation")
    deadline = time.monotonic() + timeout_seconds(operation)
    with tempfile.TemporaryDirectory(prefix="meeting-speech-") as folder:
        root = Path(folder)
        audio, result = root / "audio.wav", root / "result.json"
        with (root / "worker.log").open("wb") as log:
            # Decode in a separately supervised process. The inference child
            # never spawns ffmpeg, so timeout/cancel cannot leave an orphan tree.
            _run([os.getenv("FFMPEG_BINARY", "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                  "-i", str(Path(path).resolve()), "-vn", "-ac", "1", "-ar", "16000",
                  "-c:a", "pcm_s16le", str(audio)], deadline=deadline, check_cancelled=check_cancelled, log=log)
            env = dict(os.environ, PYTHONUTF8="1", MEETING_ALLOW_CLOUD="0")
            _run([sys.executable, "-X", "utf8", str(Path(__file__).with_name("speech_worker.py")),
                  operation, str(audio), str(result), json.dumps({k: str(v.resolve()) for k, v in models.items()})],
                 deadline=deadline, check_cancelled=check_cancelled, log=log, env=env)
        check_cancelled()
        try:
            value = json.loads(result.read_text(encoding="utf-8"))
            if operation == "transcribe":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Empty transcript")
            else:
                if not isinstance(value, list):
                    raise ValueError("Invalid segments")
                for segment in value:
                    start, end = float(segment["start"]), float(segment["end"])
                    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end
                            and type(segment["speaker"]) is int):
                        raise ValueError("Invalid segment bounds")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SpeechWorkerError("本地語音子程序回傳無效結果。") from exc
