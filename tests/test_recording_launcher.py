import asyncio
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from backend.recording_launcher import _safe_origin, build_windows_recording_launcher, router


def request_for_url(url: str) -> Request:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": parsed.scheme,
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": [(b"host", parsed.netloc.encode("ascii"))],
        "server": (host, parsed.port),
        "client": ("127.0.0.1", 123),
    }
    return Request(scope)


class RecordingLauncherTests(unittest.TestCase):
    def test_safe_origin_accepts_lan_ipv4_and_ipv6(self):
        self.assertEqual(
            _safe_origin(request_for_url("http://192.168.20.62:8001/download")),
            "http://192.168.20.62:8001",
        )
        self.assertEqual(
            _safe_origin(request_for_url("https://[2001:db8::1]:8443/download")),
            "https://[2001:db8::1]:8443",
        )

    def test_safe_origin_rejects_command_metacharacters(self):
        with self.assertRaises(HTTPException):
            _safe_origin(request_for_url("http://bad&calc:8001/download"))

    def test_launcher_uses_isolated_profile_and_audio_default_flags(self):
        content = build_windows_recording_launcher("http://192.168.20.62:8001").decode("ascii")

        self.assertIn("http://192.168.20.62:8001/history", content)
        self.assertIn("--user-data-dir=%PROFILE_DIR%", content)
        self.assertIn("--enable-features=GetDisplayMediaAudioSelection", content)
        self.assertIn("--system-audio-capture-default_checked", content)
        self.assertIn(
            "--unsafely-treat-insecure-origin-as-secure=http://192.168.20.62:8001",
            content,
        )

    def test_https_launcher_does_not_add_insecure_origin_override(self):
        content = build_windows_recording_launcher("https://meetings.example.test").decode("ascii")
        self.assertNotIn("unsafely-treat-insecure-origin-as-secure", content)

    def test_launcher_can_bootstrap_the_isolated_profile_without_permanent_api_key(self):
        content = build_windows_recording_launcher(
            "http://192.168.20.62:8001",
            "bootstrap.123.nonce.signature",
        ).decode("ascii")
        self.assertIn("?bootstrap_token=bootstrap.123.nonce.signature", content)
        self.assertNotIn("api_key=", content)

    def test_download_endpoint_uses_current_server_origin(self):
        app = FastAPI()
        app.include_router(router)

        async def get_launcher():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 123))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://192.168.20.62:8001",
            ) as client:
                return await client.get("/downloads/windows-recording-launcher.cmd")

        original_env_key = os.environ.get("APP_API_KEY")
        os.environ["APP_API_KEY"] = "test-launcher-secret"
        try:
            response = asyncio.run(get_launcher())
        finally:
            if original_env_key is None:
                os.environ.pop("APP_API_KEY", None)
            else:
                os.environ["APP_API_KEY"] = original_env_key
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertIn(b"http://192.168.20.62:8001/history", response.content)
        self.assertIn(b"?bootstrap_token=bootstrap.", response.content)
        self.assertNotIn(b"api_key=", response.content)

    def test_recording_ui_offers_standard_desktop_capture_and_explicit_audio_check(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        screen_options = html.split('id="rec-screen-options"', 1)[1].split("</div>", 1)[0]
        self.assertIn("/static/knowledge.html#recording", screen_options)
        self.assertIn("請確認分享音訊", screen_options)
        self.assertNotIn("才會優先預設勾選", screen_options)


if __name__ == "__main__":
    unittest.main()
