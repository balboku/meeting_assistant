"""Keep sampling settings compatible with the model actually being called."""

from __future__ import annotations

import re

from google.genai import types


def for_model(model: str, config: types.GenerateContentConfig) -> types.GenerateContentConfig:
    """Use provider-default sampling for Gemini 3, without mutating callers.

    Low temperature can cause Gemini 3 to loop. The affected English audio
    reproduced thousands of repeated words at temperature=0, whereas default
    sampling returned a bounded transcript with normal completion.
    https://ai.google.dev/gemini-api/docs/gemini-3#temperature
    """
    name = str(model or "").strip().lower().removeprefix("models/")
    if not re.match(r"^gemini-3(?:[.-]|$)", name):
        return config
    return config.model_copy(update={"temperature": None, "top_p": None, "top_k": None})
