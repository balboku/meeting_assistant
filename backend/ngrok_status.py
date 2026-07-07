"""
Observe the local ngrok agent so the app can show whether LINE can reach it.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests


DEFAULT_NGROK_API_URL = "http://127.0.0.1:4040/api/tunnels"
DEFAULT_LINE_WEBHOOK_PATH = "/line-webhook"


def webhook_url_for(public_url: str, webhook_path: str = DEFAULT_LINE_WEBHOOK_PATH) -> str:
    return f"{public_url.rstrip('/')}/{webhook_path.lstrip('/')}"


def _tunnel_targets_port(tunnel: dict[str, Any], expected_port: Optional[int]) -> bool:
    if expected_port is None:
        return True

    addr = str(tunnel.get("config", {}).get("addr", ""))
    return str(expected_port) in addr


def _pick_tunnel(tunnels: list[dict[str, Any]], expected_port: Optional[int]) -> Optional[dict[str, Any]]:
    matching = [tunnel for tunnel in tunnels if _tunnel_targets_port(tunnel, expected_port)]
    candidates = matching or tunnels
    if not candidates:
        return None

    for tunnel in candidates:
        if tunnel.get("proto") == "https" and tunnel.get("public_url"):
            return tunnel

    for tunnel in candidates:
        if tunnel.get("public_url"):
            return tunnel

    return candidates[0]


def get_ngrok_status(
    expected_port: Optional[int] = None,
    api_url: Optional[str] = None,
    webhook_path: str = DEFAULT_LINE_WEBHOOK_PATH,
    request_timeout: float = 0.75,
) -> dict[str, Any]:
    """Return a UI-friendly snapshot of the local ngrok tunnel."""
    api_url = api_url or os.getenv("MEETING_ASSISTANT_NGROK_API_URL", DEFAULT_NGROK_API_URL)

    try:
        response = requests.get(api_url, timeout=request_timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return {
            "running": False,
            "public_url": None,
            "webhook_url": None,
            "message": "ngrok 未啟動或本機狀態 API 無法連線",
            "error": str(exc),
            "api_url": api_url,
        }
    except ValueError as exc:
        return {
            "running": False,
            "public_url": None,
            "webhook_url": None,
            "message": "ngrok 狀態 API 回傳格式無法解析",
            "error": str(exc),
            "api_url": api_url,
        }

    tunnels = payload.get("tunnels") or []
    tunnel = _pick_tunnel(tunnels, expected_port)
    if not tunnel:
        return {
            "running": False,
            "public_url": None,
            "webhook_url": None,
            "message": "ngrok 已啟動但尚未建立 tunnel",
            "error": None,
            "api_url": api_url,
        }

    public_url = tunnel.get("public_url")
    if not public_url:
        return {
            "running": False,
            "public_url": None,
            "webhook_url": None,
            "message": "ngrok tunnel 缺少公開 URL",
            "error": None,
            "api_url": api_url,
        }

    port_matches = _tunnel_targets_port(tunnel, expected_port)
    if expected_port is not None and not port_matches:
        return {
            "running": False,
            "public_url": public_url,
            "webhook_url": webhook_url_for(public_url, webhook_path),
            "message": f"ngrok 正在執行，但不是轉發到本機 port {expected_port}",
            "error": None,
            "api_url": api_url,
        }

    return {
        "running": True,
        "public_url": public_url,
        "webhook_url": webhook_url_for(public_url, webhook_path),
        "message": "ngrok tunnel 正在轉發到本機服務",
        "error": None,
        "api_url": api_url,
    }
