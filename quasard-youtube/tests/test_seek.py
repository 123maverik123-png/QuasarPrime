"""Перемотка: тот же трек, собранный заново с нужной секунды.

Проверяем, что seek не «двигает» уже отданный поток, а создаёт новую сессию
с Content-Length от позиции, start_ms в статусе и audio_play(offset_ms) для
Станции — иначе после перемотки поехали бы и позиция, и конец трека.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quasard_youtube import service as service_module
from quasard_youtube.config import Config
from quasard_youtube.youtube import TrackInfo

DURATION = 10.0
PADDING = 2.0
BPS = 24000
HOLDER: dict[str, Any] = {}


class FakeResolver:
    async def resolve(self, query: str, *, skip: frozenset[str] = frozenset()) -> TrackInfo:
        return TrackInfo(
            video_id="abc",
            title="Тестовый трек",
            artist="Тестовый исполнитель",
            duration=DURATION,
            webpage_url="https://www.youtube.com/watch?v=abc",
            query=query,
        )


class FakeGlagol:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def play(
        self,
        url: str,
        title: str = "",
        subtitle: str = "",
        *,
        directive: str | None = None,
        offset_ms: int = 0,
    ) -> dict[str, Any]:
        self.calls.append(("play", url, offset_ms))
        service = HOLDER["service"]
        if service.hub.current is not None:
            service.hub.current.connected.set()
        return {"status": "SENT"}

    async def stop(self) -> dict[str, Any]:
        self.calls.append(("stop", "", 0))
        return {"status": "SENT"}

    async def pause(self) -> dict[str, Any]:
        self.calls.append(("pause", "", 0))
        return {"status": "SENT"}

    async def resume(self) -> dict[str, Any]:
        self.calls.append(("resume", "", 0))
        return {"status": "SENT"}

    async def close(self) -> None:
        return None


def _make(monkeypatch: pytest.MonkeyPatch) -> tuple[service_module.PlaybackService, FakeGlagol, list[float]]:
    produced: list[float] = []

    async def fake_produce(config, resolver, track, buffer, content_length, *, start_seconds=0.0, **kwargs):
        produced.append(start_seconds)
        await buffer.write(b"A" * content_length)
        await buffer.finish()
        return content_length

    monkeypatch.setattr(service_module, "YoutubeResolver", lambda config: FakeResolver())
    monkeypatch.setattr(service_module, "produce_mp3", fake_produce)

    config = Config()
    config.station.host = "192.168.50.104"
    config.station.device_id = "device"
    config.station.session_id = "session"
    config.server.advertise_host = "192.168.50.95"
    config.playback.directive = "audio_play"
    service = service_module.PlaybackService(config)
    fake = FakeGlagol()
    service.glagol.play = fake.play  # type: ignore[method-assign]
    service.glagol.stop = fake.stop  # type: ignore[method-assign]
    service.glagol.close = fake.close  # type: ignore[method-assign]
    HOLDER["service"] = service
    return service, fake, produced


def test_seek_rebuilds_stream_from_position(monkeypatch: pytest.MonkeyPatch) -> None:
    service, fake, produced = _make(monkeypatch)

    async def run() -> None:
        try:
            first = await service.play("тест")
            assert first["start_ms"] == 0
            assert first["content_length"] == int((DURATION + PADDING) * BPS)
            old = service.hub.current
            assert old is not None

            result = await service.seek(4000)
            assert result["seeked"] is True, result
            assert result["position_ms"] == 4000
            assert result["start_ms"] == 4000
            assert result["content_length"] == int((DURATION - 4 + PADDING) * BPS)

            # конвейер собран с -ss 4, а Станции уходит offset 0: она считает
            # позицию внутри потока, который уже начинается с 4-й секунды
            assert produced == [0.0, 4.0]
            assert fake.calls[-1] == ("play", service.hub.current.url, 0)
            assert ("stop", "", 0) in fake.calls[:-1], "старый поток остановлен"

            session = service.hub.current
            assert session is not old
            assert session.start_ms == 4000
            status = service.status()["stream"]
            assert status["start_ms"] == 4000
            assert status["played_ms"] == 4000
            assert status["duration"] == DURATION
            assert status["content_length"] == int((DURATION - 4 + PADDING) * BPS)
            # байтов ещё никто не читал — позиция ровно с места перемотки
            assert status["state"] == "streaming"

            # пауза и «продолжай» после перемотки: наружу абсолютная позиция,
            # Станции — позиция внутри потока (0, поток начинается с 4-й секунды)
            paused = await service.pause()
            assert paused["paused"] is True
            resumed = await service.resume()
            assert resumed["resumed"] is True, resumed
            assert resumed["offset_ms"] == paused["offset_ms"] == 4000
            assert resumed["station_offset_ms"] == 0
            assert fake.calls[-1][2] == 0, "Станции отдан относительный offset"

            # ближе к концу не перематываем: остаток должен быть валидным
            high = await service.seek(999_999)
            assert high["seeked"] is True
            assert high["position_ms"] == int((DURATION - 3) * 1000)
            low = await service.seek(-1000)
            assert low["position_ms"] == 0

            await service.stop()
            nothing = await service.seek(1000)
            assert nothing["seeked"] is False
            assert nothing["reason"] == "nothing is playing"
        finally:
            await service.close()

    asyncio.run(run())


def test_seek_rejects_unknown_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    service, fake, _ = _make(monkeypatch)

    async def run() -> None:
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            session.duration = 0  # как у потока без известной длительности
            result = await service.seek(2000)
            assert result["seeked"] is False
            assert result["reason"] == "unknown duration"
        finally:
            await service.close()

    asyncio.run(run())


def test_ffmpeg_seek_argument() -> None:
    from quasard_youtube.audio import ffmpeg_args

    config = Config()
    plain = ffmpeg_args(config)
    assert "-ss" not in plain
    shifted = ffmpeg_args(config, 12.5)
    assert shifted[shifted.index("-ss") + 1] == "12.500"
    assert shifted.index("-ss") < shifted.index("-i")
