"""Регрессия на «Алиса, продолжай»: трек должен вставать на паузу и оживать.

Сценарии взяты из реальных логов 27.10.2026 (см. docs/RESUME_FIX.md):

* 20:33:17 «Пауза» -> 20:33:25 «Поставь на паузу» -> 20:33:43 «Продолжай»:
  audio_play ушёл, поток Станция так и не переоткрыла, а все следующие
  «продолжай» уходили в Glagol ``{"command":"play"}`` и ничего не делали.
* 20:33:25 позиция паузы уехала со 97405 мс на 105311 мс просто от того, что
  прошивка держала поток в паузе 8 секунд.
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
CONTENT_LENGTH = 12 * 24000  # (duration + padding 2 c) * 24000


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
    """Записывает команды и решает, «переподключилась» ли Станция."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.reconnect_on_play: list[bool] = []

    def set_reconnects(self, *flags: bool) -> None:
        self.reconnect_on_play = list(flags)

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
        service = _HOLDER["service"]
        if self.reconnect_on_play:
            reconnect = self.reconnect_on_play.pop(0)
        else:
            reconnect = True
        if reconnect and service.hub.current is not None:
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

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]


_HOLDER: dict[str, Any] = {}


async def _fake_produce(config, resolver, track, buffer, content_length, **kwargs):
    await buffer.write(b"A" * content_length)
    await buffer.finish()
    return content_length


def _make_service(monkeypatch: pytest.MonkeyPatch) -> tuple[service_module.PlaybackService, FakeGlagol]:
    monkeypatch.setattr(service_module, "YoutubeResolver", lambda config: FakeResolver())
    monkeypatch.setattr(service_module, "produce_mp3", _fake_produce)

    config = Config()
    config.station.host = "192.168.50.104"
    config.station.device_id = "device"
    config.station.session_id = "session"
    config.server.advertise_host = "192.168.50.95"
    config.playback.directive = "audio_play"
    config.playback.resume_connect_timeout = 0.2
    service = service_module.PlaybackService(config)
    fake = FakeGlagol()
    # Подменяем только транспорт: сам restart_stream остаётся настоящим, чтобы
    # тест проверял реальную последовательность stop -> audio_play.
    glagol = service.glagol
    glagol.play = fake.play  # type: ignore[method-assign]
    glagol.stop = fake.stop  # type: ignore[method-assign]
    glagol.pause = fake.pause  # type: ignore[method-assign]
    glagol.resume = fake.resume  # type: ignore[method-assign]
    _HOLDER["service"] = service
    return service, fake


def _simulate_playback(session: StreamSession, seconds: float) -> None:
    """Станция читала поток `seconds` секунд и перестала (пауза)."""
    now = time.time()
    session.started_sending_at = now - seconds
    session.stream_start_byte = 0
    session.bytes_sent = int(seconds * session.content_length / DURATION)
    session.last_sent_at = now


def test_pause_keeps_position_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторная пауза не двигает offset_ms (в логе уезжало на 8 секунд)."""

    async def run() -> None:
        service, _ = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            _simulate_playback(session, 4.0)

            first = await service.pause()
            assert first["paused"] is True
            # Часы идут, но Станция уже ничего не читает: проходит 5 секунд.
            session.started_sending_at -= 5.0
            second = await service.pause()

            assert second["offset_ms"] == first["offset_ms"]
            assert 3500 <= first["offset_ms"] <= 4500, first
            assert session.paused_at_bytes == session.bytes_sent
        finally:
            await service.close()

    asyncio.run(run())


def test_resume_after_voice_pause_reconnects_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пауза голосом -> «продолжай»: stop + audio_play(offset_ms) с новым url."""

    async def run() -> None:
        service, fake = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            old_url = session.url
            _simulate_playback(session, 4.0)
            paused = await service.pause()
            # Станция закрыла поток, как это делает прошивка.
            session.connected.clear()

            fake.calls.clear()
            fake.set_reconnects(True)
            result = await service.resume(force=True)

            assert result["resumed"] is True
            assert result["replayed"] is False
            assert result["offset_ms"] == paused["offset_ms"]
            assert fake.actions() == ["stop", "play"], fake.calls
            _, url, offset_ms = fake.calls[-1]
            assert offset_ms == paused["offset_ms"]
            assert url != old_url, "для переподключения нужен новый url"
            assert session.accepts(url.rsplit("/", 1)[-1][: -len(".mp3")])
        finally:
            await service.close()

    asyncio.run(run())


def test_resume_retries_and_keeps_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Первая попытка не открыла поток — вторая (с новым url) открыла.

    Позиция при этом сохраняется: перезапуск трека с нуля — последняя ступень.
    """

    async def run() -> None:
        service, fake = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            _simulate_playback(session, 4.0)
            paused = await service.pause()
            session.connected.clear()

            fake.calls.clear()
            fake.set_reconnects(False, True)
            result = await service.resume()

            assert result["resumed"] is True
            assert result["replayed"] is False
            assert result["reconnected"] is True
            assert result["attempts"] == 2
            assert result["offset_ms"] == paused["offset_ms"]
            plays = [call for call in fake.calls if call[0] == "play"]
            assert len(plays) == 2, fake.calls
            assert plays[0][2] == plays[1][2] == paused["offset_ms"]
            assert plays[0][1] != plays[1][1], "каждая попытка — новый url"
            assert service.hub.current is session
        finally:
            await service.close()

    asyncio.run(run())


def test_resume_escalates_when_station_ignores_audio_play(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Станция не переоткрыла поток ни разу — трек перезапускается целиком."""

    async def run() -> None:
        service, fake = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            _simulate_playback(session, 4.0)
            await service.pause()
            session.connected.clear()

            fake.calls.clear()
            # Две попытки переподключения прошивка проигнорировала, перезапуск — сработал.
            fake.set_reconnects(False, False, True)
            result = await service.resume()

            assert result["resumed"] is True
            assert result["replayed"] is True
            assert result["reconnected"] is False
            assert fake.calls[0][0] == "stop"
            assert service.hub.current is not None
            assert service.hub.current is not session
        finally:
            await service.close()

    asyncio.run(run())


def test_resume_does_nothing_when_stream_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Поток живой и Станция его читает: не дёргаем плеер и не шлём Glagol play."""

    async def run() -> None:
        service, fake = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            fake.calls.clear()

            result = await service.resume()

            assert result["resumed"] is True
            assert result["reconnected"] is False
            assert fake.calls == [], fake.calls
        finally:
            await service.close()

    asyncio.run(run())


def test_resume_does_not_send_useless_transport_play(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ни одна ветка продолжения не отправляет Glagol {"command":"play"}.

    Именно он раньше «съедал» повторные «продолжай»: httpx получал 200, а
    Станция ничего не делала.
    """

    async def run() -> None:
        service, fake = _make_service(monkeypatch)
        try:
            await service.play("тест")
            session = service.hub.current
            assert session is not None
            _simulate_playback(session, 2.0)
            # Мёртвое состояние из лога 20:33:43: пауза снята, потока нет.
            session.paused = False
            session.connected.clear()
            session.played_ms = 0

            fake.calls.clear()
            await service.resume(send_command=True)
            await service.resume(send_command=True)
            assert "resume" not in fake.actions(), fake.calls
            # Вместо пустого {"command":"play"} — реальное переподключение.
            assert fake.calls[0][0] == "stop", fake.calls
        finally:
            await service.close()

    asyncio.run(run())


def test_buffered_station_is_not_treated_as_paused() -> None:
    """Станция молчит в сокете, пока доигрывает буфер — это не пауза.

    Тест на живой колонке 27.10 показал: после resume Станция забрала 2.8 МБ
    (118 с звука) и 13 с не читала новые байты, продолжая играть. Порог по
    тишине в сокете дал бы ложное «на паузе» и лишнее переподключение.
    """
    buffer = StreamBuffer(max_buffer_bytes=1024 * 1024)
    session = StreamSession(
        token="t",
        url="http://127.0.0.1/stream/t.mp3",
        query="q",
        title="t",
        artist="a",
        duration=DURATION,
        content_length=CONTENT_LENGTH,
        buffer=buffer,
    )
    rate = CONTENT_LENGTH / DURATION
    session.started_sending_at = time.time() - 5.0
    session.stream_start_byte = 0
    session.last_sent_at = time.time() - 5.0  # в сокет давно не писали
    session.bytes_sent = int(120 * rate)  # но Станция забрала 120 с звука
    assert session.behind_s() < 0, session.behind_s()

    # А вот реальный простой: байты стоят, время идёт.
    session.started_sending_at = time.time() - 30.0
    session.bytes_sent = int(5 * rate)
    assert session.behind_s() > 20.0, session.behind_s()


def test_resume_without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(service_module, "YoutubeResolver", lambda config: FakeResolver())
        config = Config()
        service = service_module.PlaybackService(config)
        service.glagol = FakeGlagol()  # type: ignore[assignment]
        try:
            result = await service.resume()
            assert result["resumed"] is False
            assert result["reason"] == "nothing is playing"
        finally:
            await service.close()

    asyncio.run(run())


def test_stream_buffer_alias_serves_same_session() -> None:
    """Поток по новому url отдаётся из той же сессии и того же буфера."""

    async def run() -> None:
        buffer = StreamBuffer(max_buffer_bytes=1024 * 1024)
        session = StreamSession(
            token="first",
            url="http://127.0.0.1/stream/first.mp3",
            query="q",
            title="t",
            artist="a",
            duration=1.0,
            content_length=1200,
            buffer=buffer,
        )
        session.aliases.append("second")
        assert session.accepts("first") and session.accepts("second")
        assert not session.accepts("third")

    asyncio.run(run())
