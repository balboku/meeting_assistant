"""Project-scoped, durable quota cooldowns. No credentials or transcript stored."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.retry_policy import next_pacific_daily_reset_utc

WAIT_MARKER = "MODEL_QUOTA_WAIT:"
# Explicitly vetted free-tier candidates, not a provider catalog wildcard.
AUDIO_MODELS = (
    "gemini-3.5-transcribe", "gemini-3.8-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
)
TEXT_MODELS = ("gemma-4-31b-it", *AUDIO_MODELS[1:])
# Conservative spacing for the quotas observed in the user's free project.
LIMITS = {
    "gemini-3.5-transcribe": (3, 10000, 25),
    "gemini-3.8-flash": (5, 250000, 20),
    "gemini-3.5-flash": (5, 250000, 20),
    "gemini-3.5-flash-lite": (15, 250000, 500),
    "gemini-3.1-flash-lite": (15, 250000, 500),
    "gemma-4-31b-it": (30, 16000, 14400),
}


def is_quota_error(error: object) -> bool:
    message = str(error).casefold().replace("_", " ")
    return "429" in message or "resource exhausted" in message or "rate limit" in message


def daily_quota(error: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(error).casefold())
    return "perday" in normalized and ("quota" in normalized or "429" in normalized)


def is_model_unavailable(error: object) -> bool:
    message = str(error).casefold()
    return ("model" in message and (
        "404" in message or "not supported" in message or "unsupported model" in message
    ))


class ModelsQuotaWaiting(RuntimeError):
    def __init__(self, retry_at: float, models: list[str]):
        super().__init__(WAIT_MARKER + json.dumps({"retry_at": retry_at, "models": models}))


def wait_for_quota_error(error, models):
    until = next_pacific_daily_reset_utc().timestamp() if daily_quota(error) else time.time() + 65
    return ModelsQuotaWaiting(until, models)


def quota_wait_until(detail: str, now: float | None = None) -> float | None:
    """Validate our marker; a malformed/hostile timestamp must not stall a job."""
    current = time.time() if now is None else now
    if WAIT_MARKER not in str(detail):
        return None
    try:
        value = float(json.loads(str(detail).split(WAIT_MARKER, 1)[1])["retry_at"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not math.isfinite(value):
        return None
    return min(current + 86400, max(current + 1, value))


class QuotaStore:
    """Atomic request reservations shared by workers/restarts using the same DB."""
    def __init__(self, db_path: Path, project: str, clock=time.time):
        self.db_path, self.project, self.clock = db_path, project, clock

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS model_quota_state (
                project TEXT NOT NULL, model TEXT NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0, next_request_at REAL NOT NULL DEFAULT 0,
                day_reset REAL NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project, model))""")
            with conn:
                yield conn
        finally:
            conn.close()

    def reserve(self, model: str, input_tokens: int = 0) -> float | None:
        now = self.clock()
        rpm, tpm, rpd = LIMITS.get(model, (60, 1000000, 1000000))
        if input_tokens > tpm:
            raise ValueError(f"{model}: input exceeds configured free-tier TPM; use another model")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            capability = conn.execute("SELECT blocked_until FROM model_quota_state WHERE project=? AND model=?",
                                      ("capability:" + self.project, model)).fetchone()
            if capability and capability[0] > now:
                raise ValueError(f"{model}: model endpoint temporarily unsupported")
            conn.execute("INSERT OR IGNORE INTO model_quota_state(project,model) VALUES (?,?)",
                         (self.project, model))
            blocked, next_at, reset, count = conn.execute(
                "SELECT blocked_until,next_request_at,day_reset,requests FROM model_quota_state "
                "WHERE project=? AND model=?", (self.project, model)).fetchone()
            if reset <= now:
                reset = next_pacific_daily_reset_utc(datetime.fromtimestamp(now, timezone.utc)).timestamp()
                count = 0
            available_at = max(blocked, next_at, reset if count >= rpd else 0)
            if available_at > now:
                return available_at
            spacing = max(60 / rpm, max(0, input_tokens) * 60 / tpm) + 1
            conn.execute("UPDATE model_quota_state SET next_request_at=?,day_reset=?,requests=? "
                         "WHERE project=? AND model=?",
                         (now + spacing, reset, count + 1, self.project, model))
        return None

    def block(self, model: str, error: object) -> float:
        now = self.clock()
        until = now + 65
        if daily_quota(error):
            until = next_pacific_daily_reset_utc(datetime.fromtimestamp(now, timezone.utc)).timestamp()
        else:
            delays = re.findall(r'(?:retryDelay[\"\s:]+|retry in\s+)(\d+(?:\.\d+)?)s', str(error), re.I)
            if delays:
                until = now + max(65, min(86400, float(delays[0]) + 2))
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO model_quota_state(project,model) VALUES (?,?)",
                         (self.project, model))
            conn.execute("UPDATE model_quota_state SET blocked_until=MAX(blocked_until,?) "
                         "WHERE project=? AND model=?", (until, self.project, model))
        return until

    def block_unavailable(self, model: str):
        # Capability failures are not quota waits. Keep them in a separate
        # namespace so all-misconfigured pools fail boundedly, not wait forever.
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO model_quota_state(project,model,blocked_until) VALUES (?,?,?)",
                         ("capability:" + self.project, model, self.clock() + 900))


def generate_with_pool(models, generate, *, store=None, before_call=None,
                       on_switch=None, is_transient=None, input_tokens=0):
    """At most one request per candidate; quotas wait, real failures stay bounded."""
    candidates = list(dict.fromkeys(model for model in models if model))
    waits, last_error, provider_error = [], None, None
    for index, model in enumerate(candidates):
        if store is not None:
            try:
                available_at = store.reserve(model, input_tokens(model) if callable(input_tokens) else input_tokens)
            except ValueError as exc:
                last_error = exc
                continue
            if available_at is not None:
                waits.append(available_at)
                continue
        # Check cancellation and request budget outside provider-error handling.
        if before_call is not None:
            before_call()
        if index and on_switch is not None:
            on_switch(model)
        try:
            return generate(model), model
        except Exception as exc:
            last_error = exc
            if is_model_unavailable(exc):
                if store:
                    store.block_unavailable(model)
                continue
            provider_error = exc
            if is_quota_error(exc):
                waits.append(store.block(model, exc) if store else time.time() + 65)
            elif is_transient is not None and not is_transient(exc):
                raise
    if waits and (provider_error is None or is_quota_error(provider_error)):
        raise ModelsQuotaWaiting(min(waits), candidates)
    if last_error is not None:
        raise last_error
    raise ValueError("No eligible model configured")
