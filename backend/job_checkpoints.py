"""Job-local checkpoints for validated stages and forced-rerun quota resume."""
from __future__ import annotations

import hashlib
import inspect
import json
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from backend.model_availability import is_quota_error, quota_wait_until

ACTIVE_CHECKPOINTS = ContextVar("meeting_job_checkpoints", default=None)


def _read_checkpoint(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload["value"], sort_keys=True, ensure_ascii=False).encode()
    if payload.get("sha256") != hashlib.sha256(serialized).hexdigest():
        raise ValueError("Checkpoint checksum mismatch")
    return tuple(payload["value"]) if payload.get("tuple") else payload["value"]


def _write_checkpoint(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    checksum = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    temporary.write_text(json.dumps({"tuple": isinstance(value, tuple), "value": value, "sha256": checksum},
                                    ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class JobCheckpoints:
    def __init__(self, output_dir, job_id, source_sha256, error_detail="", forced=False):
        self.root = Path(output_dir) / "job_checkpoints" / hashlib.sha256(job_id.encode()).hexdigest()
        self.source_sha256, self.forced = source_sha256, forced
        self.quota_resume = is_quota_error(error_detail) or quota_wait_until(error_detail) is not None

    def path(self, stage, arguments):
        identity = {"version": 1, "source": self.source_sha256, "stage": stage, "arguments": arguments}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str,
                                          ensure_ascii=False).encode()).hexdigest()
        return self.root / f"{digest}.json"


def checkpointed(function):
    """Save only a stage's successful validated return, never a raw model draft."""
    signature = inspect.signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        scope = ACTIVE_CHECKPOINTS.get()
        if scope is None:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = {k: v for k, v in bound.arguments.items() if k != "client" and not callable(v)}
        path = scope.path(function.__name__, arguments)
        try:
            return _read_checkpoint(path)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        value = function(*args, **kwargs)
        _write_checkpoint(path, value)
        return value
    return call


def quota_resume():
    scope = ACTIVE_CHECKPOINTS.get()
    return bool(scope and scope.quota_resume)


def shared_cache_allowed():
    scope = ACTIVE_CHECKPOINTS.get()
    return not (scope and scope.quota_resume and scope.forced)


def load_verified_segment(index, context):
    scope = ACTIVE_CHECKPOINTS.get()
    if not scope or not scope.quota_resume:
        return None
    try:
        value = _read_checkpoint(scope.path("verified_segment", {"index": index, "context": context}))
        text = value["transcript"]
        return text if isinstance(text, str) and text.strip() else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_verified_segment(index, context, transcript, responding_models):
    scope = ACTIVE_CHECKPOINTS.get()
    if scope:
        _write_checkpoint(scope.path("verified_segment", {"index": index, "context": context}),
                          {"transcript": transcript, "responding_models": responding_models})
