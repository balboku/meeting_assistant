"""Shared, side-effect-free helpers for ASGI API tests."""

import asyncio
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def asgi_request(app, method: str, path: str, asgi_client=("127.0.0.1", 123), **kwargs):
    async def _request():
        transport = httpx.ASGITransport(app=app, client=asgi_client)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_request())
