"""Disabled-by-default account, role, and audit helpers.

The current product still uses local-network/API-key access.  This module keeps
the future RBAC surface explicit without changing runtime behavior until
MEETING_AUTH_ENABLED is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


AUTH_FEATURE_ENABLED = _env_flag("MEETING_AUTH_ENABLED", default=False)
AUTH_USER_HEADER = os.getenv("MEETING_AUTH_USER_HEADER", "X-Meeting-User").strip() or "X-Meeting-User"
AUTH_DEFAULT_ROLE = os.getenv("MEETING_AUTH_DEFAULT_ROLE", "viewer").strip() or "viewer"

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "meeting:read",
        "meeting:write",
        "meeting:delete",
        "meeting:rerun",
        "meeting:export",
        "job:read",
        "job:manage",
        "user:manage",
        "audit:read",
    },
    "editor": {
        "meeting:read",
        "meeting:write",
        "meeting:rerun",
        "meeting:export",
        "job:read",
        "job:manage",
    },
    "viewer": {
        "meeting:read",
        "meeting:export",
        "job:read",
    },
}


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
        "default_role": AUTH_DEFAULT_ROLE,
        "roles": {
            role: sorted(permissions)
            for role, permissions in ROLE_PERMISSIONS.items()
        },
    }


def actor_from_request(request: Request) -> AuthActor:
    """Build an actor from the configured header when RBAC is enabled.

    This is intentionally not wired into request enforcement while the feature
    flag is false, preserving the current local-network/API-key behavior.
    """
    if not AUTH_FEATURE_ENABLED:
        return DISABLED_LOCAL_ACTOR

    email = (request.headers.get(AUTH_USER_HEADER) or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="缺少使用者身分標頭。")
    role = (request.headers.get("X-Meeting-Role") or AUTH_DEFAULT_ROLE).strip().lower()
    if role not in ROLE_PERMISSIONS:
        raise HTTPException(status_code=403, detail=f"未知角色：{role}")
    return AuthActor(email=email, role=role, enabled=True)


def require_permission(actor: AuthActor, permission: str) -> None:
    """Future RBAC guard. No-op for the disabled local actor."""
    if not actor.enabled:
        return
    if not actor.can(permission):
        raise HTTPException(status_code=403, detail=f"角色 {actor.role} 缺少權限：{permission}")
