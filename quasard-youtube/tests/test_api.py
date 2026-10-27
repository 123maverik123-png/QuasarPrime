from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer

from quasard_youtube.streaming import StreamHub, create_app


class FakeControl:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def play(self, query: str, *, directive: str | None = None) -> dict[str, Any]:
        self.queries.append(query)
        return {"title": "t", "query": query, "session": {}}

    async def stop(self) -> dict[str, Any]:
        return {"stopped": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "stream": None}

    async def handle_text(self, text: str) -> dict[str, Any]:
        return {"matched": False, "spoken_text": text}


def test_play_endpoint() -> None:
    async def run() -> None:
        control = FakeControl()
        server = TestServer(create_app(StreamHub(), control))
        await server.start_server()
        async with aiohttp.ClientSession() as client:
            response = await client.post(
                server.make_url("/api/play"), json={"query": "Кино"}
            )
            payload = await response.json()
            assert response.status == 200
            assert payload["ok"] is True
            assert control.queries == ["Кино"]

            status = await client.get(server.make_url("/api/status"))
            assert status.status == 200
        await server.close()

    asyncio.run(run())


def test_api_token_is_enforced() -> None:
    async def run() -> None:
        server = TestServer(create_app(StreamHub(), FakeControl(), api_token="secret"))
        await server.start_server()
        async with aiohttp.ClientSession() as client:
            unauthorized = await client.get(server.make_url("/api/status"))
            assert unauthorized.status == 401
            authorized = await client.get(
                server.make_url("/api/status"), headers={"X-Api-Token": "secret"}
            )
            assert authorized.status == 200
            health = await client.get(server.make_url("/healthz"))
            assert health.status == 200
        await server.close()

    asyncio.run(run())
