from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quasard_youtube import service as service_module
from quasard_youtube.config import Config
from quasard_youtube.youtube import TrackInfo


class FakeResolver:
    async def resolve(self, query: str, *, skip: frozenset[str] = frozenset()) -> TrackInfo:
        return TrackInfo(
            video_id="abc",
            title="Тестовый трек",
            artist="Тестовый исполнитель",
            duration=10.0,
            webpage_url="https://www.youtube.com/watch?v=abc",
            query=query,
        )


def test_play_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, Any] = {}

    async def fake_produce(config, resolver, track, buffer, content_length, **kwargs):
        await buffer.write(b"A" * content_length)
        await buffer.finish()
        return content_length

    async def fake_play(self, url, title="", subtitle="", *, directive=None, offset_ms=0):
        # Станция «подключилась» к потоку сразу после команды.
        holder["service"].hub.current.connected.set()
        return {"status": "SENT"}

    monkeypatch.setattr(service_module, "YoutubeResolver", lambda config: FakeResolver())
    monkeypatch.setattr(service_module, "produce_mp3", fake_produce)
    monkeypatch.setattr(service_module.GlagolClient, "play", fake_play)

    async def run() -> None:
        config = Config()
        config.station.host = "192.168.50.104"
        config.station.device_id = "device"
        config.station.session_id = "session"
        config.server.advertise_host = "192.168.50.95"
        service = service_module.PlaybackService(config)
        holder["service"] = service
        try:
            result = await service.play("тест")
            assert result["title"] == "Тестовый трек"
            assert result["artist"] == "Тестовый исполнитель"
            assert result["stream_url"].startswith("http://192.168.50.95:8099/stream/")
            assert result["content_length"] == 12 * 24000
            assert service.hub.current is not None
            assert service.hub.current.connected.is_set()
        finally:
            await service.close()

    asyncio.run(run())
