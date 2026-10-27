from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer

from quasard_youtube.streaming import StreamBuffer, StreamHub, StreamSession, create_app


class FakeControl:
    async def play(self, query: str, *, directive: str | None = None) -> dict[str, Any]:
        return {"query": query}

    async def stop(self) -> dict[str, Any]:
        return {"stopped": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True}

    async def handle_text(self, text: str) -> dict[str, Any]:
        return {"matched": False, "text": text}


async def _scenario(head_first: bool) -> None:
    buffer = StreamBuffer(max_buffer_bytes=1024 * 1024)
    session = StreamSession(
        token="testtoken",
        url="http://127.0.0.1/stream/testtoken.mp3",
        query="q",
        title="t",
        artist="a",
        duration=1.0,
        content_length=1200,
        buffer=buffer,
    )
    hub = StreamHub(silence=b"Z" * 4096)
    hub.install(session)
    app = create_app(hub, FakeControl())
    server = TestServer(app)
    await server.start_server()
    url = server.make_url("/stream/testtoken.mp3")

    async with aiohttp.ClientSession() as client:
        if head_first:
            head = await client.head(url)
            assert head.status == 200
            assert head.headers["Content-Length"] == "1200"
            assert await head.read() == b""
            assert session.consumer_claimed is False

        await buffer.write(b"A" * 1000)
        await buffer.finish()
        response = await client.get(url)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("audio/mpeg")
        assert response.headers["Content-Length"] == "1200"
        assert "Transfer-Encoding" not in response.headers
        body = await response.read()
        assert len(body) == 1200
        assert body[:1000] == b"A" * 1000
        assert body[1000:] == b"Z" * 200

    await asyncio.sleep(0)
    assert session.connected.is_set()
    assert session.finished.is_set()
    await server.close()
    await session.terminate()


def test_stream_pads_to_content_length() -> None:
    async def run() -> None:
        buffer = StreamBuffer(max_buffer_bytes=1024 * 1024)
        session = StreamSession(
            token="testtoken",
            url="http://127.0.0.1/stream/testtoken.mp3",
            query="q",
            title="t",
            artist="a",
            duration=1.0,
            content_length=1200,
            buffer=buffer,
        )
        hub = StreamHub(silence=b"Z" * 4096)
        hub.install(session)
        app = create_app(hub, FakeControl())
        server = TestServer(app)
        await server.start_server()
        await buffer.write(b"A" * 1000)
        await buffer.finish()
        async with aiohttp.ClientSession() as client:
            response = await client.get(server.make_url("/stream/testtoken.mp3"))
            body = await response.read()
        assert response.status == 200
        assert len(body) == 1200
        assert body[:1000] == b"A" * 1000
        assert body[1000:] == b"Z" * 200
        await server.close()
        await session.terminate()

    asyncio.run(run())


def test_head_does_not_consume_stream() -> None:
    asyncio.run(_scenario(head_first=True))


def test_unknown_stream_token() -> None:
    async def run() -> None:
        hub = StreamHub()
        app = create_app(hub, FakeControl())
        server = TestServer(app)
        await server.start_server()
        async with aiohttp.ClientSession() as client:
            response = await client.get(server.make_url("/stream/nope.mp3"))
            assert response.status == 410
        await server.close()

    asyncio.run(run())
