"""Small composition layer for task stages, capability pools and quota storage."""
from __future__ import annotations

import os

from backend import database
from backend.model_availability import AUDIO_MODELS, TEXT_MODELS, QuotaStore


def quota_store(client):
    # Injected test/provider clients retain their original contract. Production
    # SDK clients share durable project state, including text and audio stages.
    if not type(client).__module__.startswith("google.genai"):
        return None
    if os.getenv("MODEL_QUOTA_ROUTING_ENABLED", "1") == "0":
        return None
    project = os.getenv("GEMINI_PROJECT_ID", "configured-gemini-project").strip()
    return QuotaStore(database.DB_PATH, project or "configured-gemini-project")


def candidates(primary, fallback, *, audio, expanded=True):
    allowed = AUDIO_MODELS if audio else TEXT_MODELS
    values = [primary, fallback]
    if expanded and primary in allowed:
        values.extend(allowed)
    # Do not feed audio to text-only Gemma, or summarize with Transcribe.
    prohibited = {"gemma-4-31b-it", "gemma-4-26b-it"} if audio else {AUDIO_MODELS[0]}
    return list(dict.fromkeys(m for m in values if m and m not in prohibited))


def cache_candidates(primary, fallback, context):
    """Identical audio/prompt identity, with isolated actual-model provenance."""
    return [("fallback_cache", {**context, "model": m})
            for m in candidates(primary, fallback, audio=True, expanded=primary in AUDIO_MODELS)
            if m != str(context.get("model") or "")]


def estimate_input_tokens(contents, audio_seconds=0):
    # Conservative input estimate for admission/spacing, not billing or usage.
    text = sum(len(value) for value in contents if isinstance(value, str))
    return max(1, (text + 2) // 3 + int(max(0, audio_seconds) * 32))
