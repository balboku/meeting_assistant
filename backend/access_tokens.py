"""Short-lived, purpose-scoped HMAC capabilities for browser bootstrap."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional


def create_access_token(
    secret: str,
    purpose: str,
    *,
    ttl_seconds: int,
    now: Optional[int] = None,
) -> str:
    normalized_secret = str(secret or "")
    normalized_purpose = str(purpose or "").strip()
    if not normalized_secret or not normalized_purpose:
        raise ValueError("secret 與 purpose 不可為空")
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + max(1, int(ttl_seconds))
    nonce = secrets.token_urlsafe(12)
    payload = f"{normalized_purpose}.{expires_at}.{nonce}"
    signature = hmac.new(
        normalized_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def validate_access_token(
    token: Optional[str],
    secret: str,
    purpose: str,
    *,
    now: Optional[int] = None,
) -> bool:
    if not token or not secret or not purpose:
        return False
    try:
        token_purpose, expires_text, nonce, supplied_signature = str(token).split(
            ".",
            3,
        )
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return False
    if token_purpose != str(purpose).strip() or not nonce:
        return False
    current_time = int(time.time() if now is None else now)
    if expires_at < current_time:
        return False
    payload = f"{token_purpose}.{expires_at}.{nonce}"
    expected_signature = hmac.new(
        str(secret).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)
