"""Role and route-permission policy without persistence dependencies."""

from __future__ import annotations

from typing import Optional


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


def normalize_role(role: Optional[str], *, default: Optional[str] = None) -> str:
    normalized = str(role or default or "").strip().lower()
    if normalized not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {normalized or '<empty>'}")
    return normalized


def permission_for_request(method: str, path: str) -> Optional[str]:
    """Return the least privilege required for one HTTP operation."""
    verb = str(method or "GET").upper()
    route = "/" + str(path or "").lstrip("/")
    if (
        route in {
            "/",
            "/history",
            "/favicon.ico",
            "/health",
            "/livez",
            "/readyz",
            "/config",
        }
        or route.startswith(("/static/", "/docs", "/redoc", "/openapi.json"))
    ):
        return None

    if route.startswith("/admin/users"):
        return "user:manage"
    if route.startswith("/admin/audit-logs"):
        return "audit:read"
    if route == "/metrics":
        return "job:read"
    if route in {"/upload-media", "/upload-audio"}:
        return "meeting:write"
    if route.startswith("/status/"):
        return "job:read"
    if route == "/jobs" or route.startswith("/jobs/"):
        return "job:read" if verb in {"GET", "HEAD"} else "job:manage"

    if route.startswith("/source-media"):
        if verb in {"GET", "HEAD"}:
            return "meeting:read"
        if verb == "DELETE" or route.endswith("/archive-unlinked"):
            return "meeting:delete"
        return "meeting:write"

    if route == "/meetings" or route.startswith("/meetings/"):
        if verb in {"GET", "HEAD"}:
            return "meeting:export" if "/export/" in route else "meeting:read"
        if verb == "DELETE":
            return "meeting:delete"
        if (
            route.endswith("/rerun")
            or route.endswith("/previous-minutes-rerun")
            or "/quality/" in route
            or route == "/meetings/quality/recheck-all"
        ):
            return "meeting:rerun"
        return "meeting:write"

    return None
