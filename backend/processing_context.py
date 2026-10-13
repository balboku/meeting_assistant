"""Compose job-scoped vocabulary, local speech and outbound policy."""
from functools import wraps
from inspect import signature
from pathlib import Path

from backend import database
from backend.company_vocabulary import vocabulary_context
from backend.local_speech import speech_context
from backend.processing_policy import outbound_context, source_mode


def processing_context(function):
    @wraps(function)
    def run(*args, **kwargs):
        values = signature(function).bind(*args, **kwargs).arguments
        path = Path(values["audio_path"])
        payload = (database.get_job(values["job_id"]) or {}).get("payload") or {}
        mode = payload.get("processing_mode", "cloud")
        if path.is_file():
            mode = source_mode(path, mode)
        with vocabulary_context(payload.get("vocabulary_snapshot")), speech_context(path), outbound_context(mode == "local"):
            if mode == "local":
                from backend.local_processing import LocalProcessingStopped, process_local
                try:
                    return process_local(values)
                except LocalProcessingStopped:
                    return None
            return function(*args, **kwargs)
    return run
