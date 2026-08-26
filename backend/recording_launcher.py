"""Downloadable client launcher for Chrome screen recordings on Windows."""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.access_tokens import create_access_token


router = APIRouter(tags=["錄製工具"])

_DNS_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_DOWNLOAD_NAME = "meeting-assistant-recording-chrome.cmd"


def _safe_origin(request: Request) -> str:
    """Build a command-safe HTTP(S) origin from structured URL fields."""
    scheme = request.url.scheme.lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="不支援的連線協定。")
    try:
        hostname = request.url.hostname or ""
        port = request.url.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="伺服器網址格式不正確。") from exc
    try:
        parsed_ip = ipaddress.ip_address(hostname)
    except ValueError:
        if not _DNS_HOST_RE.fullmatch(hostname) or ".." in hostname:
            raise HTTPException(status_code=400, detail="伺服器名稱格式不正確。")
        formatted_host = hostname
    else:
        formatted_host = f"[{hostname}]" if parsed_ip.version == 6 else hostname
    if port is not None and not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="伺服器連接埠超出範圍。")
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    return f"{scheme}://{formatted_host}{f':{port}' if port and not default_port else ''}"


def build_windows_recording_launcher(origin: str, bootstrap_token: str = "") -> bytes:
    """Return an ASCII/CRLF launcher; ``origin`` must already be validated."""
    secure_context_arg = ""
    if origin.startswith("http://"):
        secure_context_arg = f' "--unsafely-treat-insecure-origin-as-secure={origin}"'
    meeting_url = f"{origin}/history"
    if bootstrap_token:
        meeting_url += f"?bootstrap_token={quote(bootstrap_token, safe='')}"
    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        f'set "MEETING_ORIGIN={origin}"',
        f'set "MEETING_URL={meeting_url}"',
        'set "CHROME_EXE="',
        'if defined MEETING_ASSISTANT_CHROME_PATH if exist "%MEETING_ASSISTANT_CHROME_PATH%" set "CHROME_EXE=%MEETING_ASSISTANT_CHROME_PATH%"',
        'if not defined CHROME_EXE if exist "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME_EXE=%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe"',
        'if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe"',
        'if not defined CHROME_EXE if exist "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME_EXE=%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"',
        "if not defined CHROME_EXE (",
        "  echo Google Chrome was not found. Install Chrome and run this launcher again.",
        "  pause",
        "  exit /b 1",
        ")",
        'set "PROFILE_DIR=%LOCALAPPDATA%\\MeetingAssistant\\ChromeRecordingProfile"',
        'start "" "%CHROME_EXE%" "--user-data-dir=%PROFILE_DIR%" --no-first-run --no-default-browser-check --enable-features=GetDisplayMediaAudioSelection --system-audio-capture-default_checked'
        + secure_context_arg
        + ' --new-window "%MEETING_URL%"',
        "endlocal",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


@router.get(
    "/downloads/windows-recording-launcher.cmd",
    summary="下載 Windows 錄製專用 Chrome 啟動器",
)
async def download_windows_recording_launcher(request: Request) -> Response:
    origin = _safe_origin(request)
    api_key = os.getenv("APP_API_KEY", "").strip()
    bootstrap_token = (
        create_access_token(api_key, "bootstrap", ttl_seconds=5 * 60)
        if api_key
        else ""
    )
    return Response(
        content=build_windows_recording_launcher(origin, bootstrap_token),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{_DOWNLOAD_NAME}"',
            "Cache-Control": "no-store",
        },
    )
