"""Feature-gated persisted account, role, and audit helpers."""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request

from backend.access_tokens import validate_access_token
from backend.security_policy import (
    ROLE_PERMISSIONS,
    normalize_role,
    permission_for_request,
)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


AUTH_FEATURE_ENABLED = _env_flag("MEETING_AUTH_ENABLED", default=False)
AUTH_USER_HEADER = os.getenv("MEETING_AUTH_USER_HEADER", "X-Meeting-User").strip() or "X-Meeting-User"
AUTH_DEFAULT_ROLE = os.getenv("MEETING_AUTH_DEFAULT_ROLE", "viewer").strip() or "viewer"
AUTH_LOCAL_SESSION_USER = (
    os.getenv(
        "MEETING_AUTH_LOCAL_SESSION_USER",
        "local-admin@meeting-assistant.local",
    ).strip().lower()
)
AUTH_API_KEY = os.getenv("APP_API_KEY", "").strip()
AUTH_API_KEY_COOKIE_NAME = "meeting_assistant_api_key"
AUTH_TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(value.strip())
    for value in os.getenv(
        "MEETING_AUTH_TRUSTED_PROXY_NETWORKS",
        "127.0.0.0/8,::1/128",
    ).split(",")
    if value.strip()
)


@dataclass(frozen=True)
class AuthActor:
    email: str
    role: str
    user_id: Optional[int] = None
    enabled: bool = False

    @property
    def permissions(self) -> set[str]:
        return set(ROLE_PERMISSIONS.get(self.role, set()))

    def can(self, permission: str) -> bool:
        return permission in self.permissions


DISABLED_LOCAL_ACTOR = AuthActor(
    email="local-disabled-auth",
    role="admin",
    user_id=None,
    enabled=False,
)


def auth_config_payload() -> dict[str, Any]:
    """Expose non-secret auth configuration for health/config responses."""
    return {
        "enabled": AUTH_FEATURE_ENABLED,
        "user_header": AUTH_USER_HEADER,
        "local_session_user": AUTH_LOCAL_SESSION_USER,
        "default_role": AUTH_DEFAULT_ROLE,
        "trusted_proxy_networks": [
            str(network) for network in AUTH_TRUSTED_PROXY_NETWORKS
        ],
        "roles": {
            role: sorted(permissions)
            for role, permissions in ROLE_PERMISSIONS.items()
        },
    }


def _request_has_local_access_credential(request: Request) -> bool:
    """Accept loopback or a valid API/bootstrap/session credential."""
    client_host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(str(client_host or "").strip()).is_loopback:
            return True
    except ValueError:
        pass
    if not AUTH_API_KEY:
        return False
    supplied = (
        request.headers.get("x-api-key")
        or request.query_params.get("api_key")
    )
    if supplied and hmac.compare_digest(str(supplied), AUTH_API_KEY):
        return True
    if validate_access_token(
        request.query_params.get("bootstrap_token"),
        AUTH_API_KEY,
        "bootstrap",
    ):
        return True
    return validate_access_token(
        request.cookies.get(AUTH_API_KEY_COOKIE_NAME),
        AUTH_API_KEY,
        "session",
    )


def actor_from_request(request: Request) -> AuthActor:
    """Build an actor from a trusted header or local/API session.

    Roles are always loaded from app_users so clients cannot grant themselves
    access by sending a role header.
    """
    if not AUTH_FEATURE_ENABLED:
        return DISABLED_LOCAL_ACTOR

    header_email = (request.headers.get(AUTH_USER_HEADER) or "").strip().lower()
    if header_email:
        client_host = request.client.host if request.client else ""
        try:
            client_address = ipaddress.ip_address(str(client_host or "").strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail="無法驗證身分標頭來源。",
            ) from exc
        if not any(
            client_address in network for network in AUTH_TRUSTED_PROXY_NETWORKS
        ):
            raise HTTPException(
                status_code=401,
                detail="使用者身分標頭只能由可信任反向代理提供。",
            )
        email = header_email
    else:
        if not AUTH_LOCAL_SESSION_USER:
            raise HTTPException(status_code=401, detail="缺少使用者身分標頭。")
        if not _request_has_local_access_credential(request):
            raise HTTPException(
                status_code=401,
                detail="缺少有效的本機或 API session 身分憑證。",
            )
        email = AUTH_LOCAL_SESSION_USER

    from backend.database import get_app_user_by_email

    user = get_app_user_by_email(email)
    if not user:
        raise HTTPException(status_code=403, detail="此使用者尚未建立權限。")
    if not int(user.get("is_active") or 0):
        raise HTTPException(status_code=403, detail="此使用者帳號已停用。")
    try:
        role = normalize_role(user.get("role"), default=AUTH_DEFAULT_ROLE)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="此使用者角色設定無效。") from exc

    user_id = user.get("id")
    try:
        parsed_user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        parsed_user_id = None
    return AuthActor(
        email=str(user.get("email") or email).strip().lower(),
        role=role,
        user_id=parsed_user_id,
        enabled=True,
    )


def require_permission(actor: AuthActor, permission: str) -> None:
    """Enforce one permission; no-op only while the feature flag is disabled."""
    if not actor.enabled:
        return
    if not actor.can(permission):
        raise HTTPException(status_code=403, detail=f"角色 {actor.role} 缺少權限：{permission}")
