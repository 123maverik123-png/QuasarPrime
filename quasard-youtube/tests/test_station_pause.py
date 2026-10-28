"""Пауза кольцом/кнопкой на Станции и точная позиция из её состояния.

Реальные логи 28.10.2026 (maind.log станции):

* кольцо на колонке и Glagol ``stop`` дают одно и то же — ``audio_stop``,
  ``player_finish`` и стирание сессии проигрывателя; мягкой паузы у прошивки
  для audio_play нет;
* при этом HTTP-поток кольцо не закрывает, байты просто перестают забираться,
  поэтому панель продолжала считать время;
* зато Станция раз в секунду присылает ``state.playing`` и
  ``playerState.progress`` — по ним и позиция, и пауза.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from quasard_youtube import service as service_module
from quasard_youtube.config import Config
from quasard_youtube.streaming import StreamBuffer, StreamSession
from quasard_youtube.youtube import TrackInfo

DURATION = 10.0
CONTENT_LENGTH = 240_000  # 10 c * 24000 байт/с


def _session(**kwargs: Any) -> StreamSession:
    return StreamSession(
        token="tok",
        url="http://127.0.0.1/stream/tok.mp3",
        query="q",
        title="Тестовый трек",
        artist="Тестовый исполнитель",
        duration=DURATION,
        content_length=CONTENT_LENGTH,
        buffer=StreamBuffer(max_buffer_bytes=1024),
        **kwargs,
    )


def test_station_pause_detected_and_position_frozen() -> None:
    session = _session()
    assert session.apply_station_state(True, 4000, now=100.0) is False
    assert session.seen_playing is True

    # Меньше порога тишины — ещё не пауза.
    assert session.apply_station_state(False, 4000, now=101.0) is False
    assert session.paused is False

    assert session.apply_station_state(False, 4000, now=103.0) is True
    assert session.paused is True
    assert session.paused_by == "station"
    assert session.played_ms == 4000
    # Байт для продолжения — по позиции, а не по скачанному вперёд буферу.
    assert session.resume_offset == 4 * 24000


def test_station_resume_clears_pause() -> None:
    session = _session()
    session.apply_station_state(True, 4000, now=100.0)
    session.apply_station_state(False, 4000, now=101.0)
    session.apply_station_state(False, 4000, now=103.0)
    assert session.paused is True

    # Станция договорила и продолжила сама.
    assert session.apply_station_state(True, 4300, now=110.0) is False
    assert session.paused is False
    assert session.paused_by == ""


def test_station_pause_ignored_before_first_play() -> None:
    """Пока Станция ни разу не играла наш поток, playing=false — не пауза."""
    session = _session()
    assert session.apply_station_state(False, -1, now=100.0) is False
    assert session.apply_station_state(False, -1, now=110.0) is False
    assert session.paused is False
    assert session.paused_by == ""


def test_stale_station_progress_ignored_until_play() -> None:
    """Сразу после audio_play Станция ещё присылает позицию прошлого трека."""
    session = _session()
    # Тот же трек, пауза на 8-й секунде: duration совпадает, playing=false.
    session.apply_station_state(False, 8000, now=100.0)
    assert session.station_progress_ms == -1
    assert session.current_played_ms() < 8000
    assert session.paused is False


def test_freeze_at_pause_uses_position_not_downloaded_bytes() -> None:
    session = _session()
    # Станция уже скачала 100 КБ вперёд, а играет 4-ю секунду.
    session.bytes_sent = 100_000
    session.freeze_at_pause(4000)
    assert session.played_ms == 4000
    assert session.paused_at_bytes == 100_000
    assert session.resume_offset == 4 * 24000
    assert session.resume_position() == (4 * 24000, 4000)


def test_current_played_ms_prefers_station_progress() -> None:
    session = _session()
    session.started_sending_at = time.time() - 7.0
    session.bytes_sent = 200_000
    session.apply_station_state(True, 4200, now=time.time())
    assert 4100 <= session.current_played_ms() <= 4300


class FakeResolver:
    async def resolve(
        self, query: str, *, skip: frozenset[str] = frozenset()
    ) -> TrackInfo:
        return TrackInfo(
            video_id="abc",
            title="Тестовый трек",
            artist="Тестовый исполнитель",
            duration=DURATION,
            webpage_url="https://www.youtube.com/watch?v=abc",
            query=query,
        )


async def _fake_produce(config, resolver, track, buffer, content_length, **kwargs):
    await buffer.write(b"A" * content_length)
    await buffer.finish()
    return content_length


def _make_service(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    """Сервис без сети: транспорт Glagol подменён, поток пишется сразу."""
    monkeypatch.setattr(service_module, "YoutubeResolver", lambda config: FakeResolver())
    monkeypatch.setattr(service_module, "produce_mp3", _fake_produce)

    config = Config()
    config.station.host = "192.168.50.104"
    config.station.device_id = "device"
    config.station.session_id = "session"
    config.server.advertise_host = "192.168.50.95"
    config.playback.directive = "audio_play"
    service = service_module.PlaybackService(config)
    calls: list[str] = []

    async def play(url, title="", subtitle="", *, directive=None, offset_ms=0):
        calls.append("play")
        session = service.hub.current
        if session is not None:
            session.connected.set()
        return {"status": "SENT"}

    async def stop() -> dict[str, Any]:
        calls.append("stop")
        return {"status": "SENT"}

    async def pause() -> dict[str, Any]:
        # Внутри glagol.pause() уходит {"command": "stop"} — жёсткая пауза.
        calls.append("pause")
        return {"status": "SENT"}

    service.glagol.play = play  # type: ignore[method-assign]
    service.glagol.stop = stop  # type: ignore[method-assign]
    service.glagol.pause = pause  # type: ignore[method-assign]
    return service, calls


def test_pause_reports_paused_at_station_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кнопка паузы: команда `stop` уходит, позиция — из progress Станции."""

    async def run() -> None:
        service, calls = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            state = service.glagol.state
            state.connected = True
            state.player_state = {"duration": DURATION}
            state.playing = True
            state.progress_s = 4.0
            state.progress_at = time.time()

            paused = await service.pause()
            assert paused["paused"] is True
            assert "pause" in calls, "без команды Станция продолжала играть"
            assert paused["offset_ms"] == 4000
            assert paused["offset_bytes"] == session.position_byte(4000)
            assert session.paused_by == "panel"

            stream = service.status()["stream"]
            assert stream is not None
            assert stream["state"] == "paused"
            assert stream["played_ms"] == 4000
        finally:
            await service.close()

    asyncio.run(run())


def test_ring_pause_seen_in_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кольцо: станция прислала playing=false — статус становится «пауза»."""

    async def run() -> None:
        service, _ = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            state = service.glagol.state
            state.connected = True
            state.player_state = {"duration": DURATION}
            state.playing = True
            state.progress_s = 3.0
            service._apply_station_state(session)
            assert session.seen_playing is True

            # Кольцо: звук пропал, progress замер, HTTP-поток жив.
            state.playing = False
            state.progress_s = 3.0
            session.station_silent_since = time.time() - 5.0

            stream = service.status()["stream"]
            assert stream is not None
            assert stream["state"] == "paused"
            assert stream["paused_by"] == "station"
            assert stream["station_playing"] is False
            assert stream["played_ms"] == 3000
        finally:
            await service.close()

    asyncio.run(run())
