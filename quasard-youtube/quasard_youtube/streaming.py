"""HTTP-раздача MP3 для Яндекс Станции.

Ключевые ограничения прошивки:
* только HTTP (не HTTPS) на локальном адресе;
* обязателен известный ``Content-Length`` — chunked/infinite stream Станция
  не принимает;
* поток должен быть конечным, поэтому длительность трека известна заранее,
  а недостающие байты в конце добиваются тишиной.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiohttp import web

_LOGGER = logging.getLogger(__name__)


class BufferClosed(RuntimeError):
    pass


class StreamBuffer:
    """Ограниченный буфер между ffmpeg и HTTP-клиентом.

    Ограничение даёт backpressure: ffmpeg не убегает на десятки минут вперёд.
    До подключения Станции буфер заполняется до max_buffer_bytes и producer
    засыпает — это и есть prebuffer.
    """

    def __init__(self, max_buffer_bytes: int) -> None:
        self.max_buffer_bytes = max_buffer_bytes
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._eof = False
        self._closed = False
        self._error: BaseException | None = None
        self._cond = asyncio.Condition()

    @property
    def buffered_bytes(self) -> int:
        return self._buffered

    @property
    def error(self) -> BaseException | None:
        return self._error

    async def write(self, data: bytes) -> None:
        if not data:
            return
        async with self._cond:
            while self._buffered >= self.max_buffer_bytes and not self._closed:
                await self._cond.wait()
            if self._closed:
                raise BufferClosed("буфер закрыт")
            if self._error is not None:
                raise BufferClosed(str(self._error))
            self._chunks.append(bytes(data))
            self._buffered += len(data)
            self._cond.notify_all()

    async def read(self) -> bytes | None:
        async with self._cond:
            await self._cond.wait_for(
                lambda: bool(self._chunks) or self._eof or self._closed or self._error
            )
            if self._chunks:
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._cond.notify_all()
                return chunk
            if self._error is not None:
                raise self._error
            return None

    async def wait_for_level(self, target: int, timeout: float) -> bool:
        """Дождаться target байт. True — уровень достигнут, False — таймаут."""
        target = max(0, min(target, self.max_buffer_bytes))

        def reached() -> bool:
            return (
                self._buffered >= target
                or self._eof
                or self._closed
                or self._error is not None
            )

        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait_for(reached), timeout)
        except asyncio.TimeoutError:
            return False
        if self._error is not None:
            raise self._error
        return self._buffered >= target or (self._eof and self._buffered > 0)

    async def finish(self) -> None:
        async with self._cond:
            self._eof = True
            self._cond.notify_all()

    async def fail(self, error: BaseException) -> None:
        async with self._cond:
            self._error = error
            self._closed = True
            self._cond.notify_all()

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            self._chunks.clear()
            self._buffered = 0
            self._cond.notify_all()


@dataclass(slots=True)
class StreamSession:
    token: str
    url: str
    query: str
    title: str
    artist: str
    duration: float
    content_length: int
    buffer: StreamBuffer
    created_at: float = field(default_factory=time.time)
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    consumer_claimed: bool = False
    bytes_sent: int = 0
    started_sending_at: float = 0.0
    client_disconnected: bool = False
    cancelled: bool = False
    # ── пауза/продолжение с сохранением позиции ─────────────────────────
    # Станция на паузу закрывает HTTP-поток, поэтому всё отданное храним в
    # history: новый GET после resume отдаём с байтового смещения
    # resume_offset (позиция, на которой остановились).
    paused: bool = False
    paused_at_bytes: int = 0
    resume_offset: int = 0
    # номер «эпохи»: увеличивается при каждом resume. Старый HTTP-handler,
    # завершившись, не должен затирать состояние нового соединения.
    resume_epoch: int = 0
    history: bytearray = field(default_factory=bytearray)
    # Дополнительные имена того же потока: после «мягкой» паузы Станция может
    # проигнорировать audio_play с уже загруженным url и не переоткрыть поток
    # (лог 20:13:13 и 20:33:43). Переподключение выдаёт новый url — при этом его
    # обслуживает та же сессия из того же буфера/history.
    aliases: list[str] = field(default_factory=list)
    # Задача текущего HTTP-обработчика потока. При переподключении её нужно
    # снять: пока Станция держит старый сокет, новый audio_play она игнорирует
    # (лог 21:24:17 — stop и audio_play в одну секунду, старый поток закрылся
    # только в 21:24:22, новую ссылку Станция так и не открыла).
    handler_task: asyncio.Task | None = field(default=None, repr=False)
    # Время последней успешной записи в сокет: по нему отличаем «станция
    # реально читает» от «станция встала на паузу, но соединение не закрыла»
    # (тогда bytes_sent замирает, а state остаётся streaming).
    last_sent_at: float = 0.0
    # Байт, с которого началось текущее соединение (Range при подключении):
    # нужно, чтобы считать позицию как «старт + сколько реально читали».
    stream_start_byte: int = 0
    # Оценка реально проигранной позиции (мс) на момент паузы: bytes_sent
    # считает байты, отданные в сокет, а станция буферизует вперёд на минуты,
    # поэтому позиция для offset_ms = min(bytes, сколько прошло с подключения).
    played_ms: int = 0
    producer_task: asyncio.Task[None] | None = None
    error: str = ""

    @property
    def offset_ms(self) -> int:
        """Логическая позиция (мс) по байтовому смещению — для Glagol offset_ms."""
        if self.content_length <= 0 or self.duration <= 0:
            return 0
        return int(self.resume_offset / self.content_length * self.duration * 1000)

    def estimate_played_ms(self) -> int:
        """Сколько примерно уже проиграно (мс), а не отдано в буфер станции.

        Станция забирает поток рывком (буфер ~1–3 МБ = минуты звука), поэтому
        сырой ``bytes_sent`` завышает позицию в разы. Считаем по времени от
        подключения: байт старта Range + сколько секунд прошло с этого
        подключения, но не больше, чем реально отдано в сокет.

        Часы — настенные: при нормальном воспроизведении позиция растёт
        линейно от подключения, даже когда Станция молчит и играет из своего
        буфера. Ошибка на паузе лечится не часами, а фиксацией позиции ровно
        один раз (``freeze_at_pause``): раньше каждое повторное «пауза»
        пересчитывало её по текущему времени, и offset_ms уезжал вперёд
        (лог 20:33:17 — 97405 мс, 20:33:25 — уже 105311 мс).
        """
        if self.content_length <= 0 or self.duration <= 0:
            return 0
        rate = self.content_length / max(self.duration, 0.001)  # байт в секунду
        elapsed_s = max(0.0, time.time() - self.started_sending_at) if self.started_sending_at else 0.0
        played_bytes = min(float(self.bytes_sent), self.stream_start_byte + elapsed_s * rate)
        return max(0, int(played_bytes / self.content_length * self.duration * 1000))

    def behind_s(self, now: float | None = None) -> float:
        """Насколько Станция отстала от реального времени, секунды.

        Больше нуля — поток не читается: пауза, локальная пауза кольцом или
        поток уже закрыт. Около нуля или меньше — Станция забрала звук вперёд
        и просто играет из своего буфера: это НОРМА, хотя байты в сокет не
        идут (поэтому ``stalled_for`` для такой проверки не годится).
        """
        if not self.started_sending_at or self.content_length <= 0 or self.duration <= 0:
            return 0.0
        rate = self.content_length / max(self.duration, 0.001)
        clock = now if now is not None else time.time()
        expected = self.stream_start_byte + max(0.0, clock - self.started_sending_at) * rate
        return (expected - self.bytes_sent) / rate

    def freeze_at_pause(self) -> None:
        """Зафиксировать позицию паузы ровно один раз.

        Повторная пауза («пауза», потом «поставь на паузу») не должна двигать
        позицию: Станция в это время уже ничего не читает.
        """
        if self.paused:
            return
        self.paused = True
        self.paused_at_bytes = self.bytes_sent
        self.resume_offset = self.bytes_sent
        self.played_ms = self.estimate_played_ms()

    def resume_position(self) -> tuple[int, int]:
        """(байт, мс) — откуда продолжать трек после паузы/обрыва потока.

        Байт: сколько Станция уже забрала (после паузы это ``paused_at_bytes``).
        Мс: замороженная позиция (``played_ms``) — она уже согласована с тем,
        что видит Станция, поэтому пересчитывать её по байтам нельзя.
        """
        limit = max(0, self.content_length - 1)
        raw = self.paused_at_bytes if self.paused else self.bytes_sent
        offset_bytes = min(max(0, raw), limit)
        played_ms = self.played_ms or self.estimate_played_ms()
        played_ms = min(max(0, int(played_ms)), max(0, int(self.duration * 1000)))
        return offset_bytes, played_ms

    def accepts(self, token: str) -> bool:
        """Принадлежит ли токен этому потоку (включая url после переподключений)."""
        if secrets.compare_digest(token, self.token):
            return True
        return any(secrets.compare_digest(token, alias) for alias in self.aliases)

    def resumable(self) -> bool:
        """Есть ли что продолжать: сессия жива и у неё есть трек."""
        return not self.cancelled

    def stream_held(self) -> bool:
        """Держит ли кто-то HTTP-поток прямо сейчас."""
        task = self.handler_task
        return task is not None and not task.done()

    def release_stream(self) -> None:
        """Снять текущий HTTP-handler: освободить Станции сокет для нового GET.

        Состояние сессии при этом не трогается — handler сам увидит, что его
        эпоха устарела, и выйдет без изменений (``resume_epoch`` уже увеличен).
        """
        task = self.handler_task
        if task is not None and not task.done():
            task.cancel()

    def stalled_for(self, now: float | None = None) -> float:
        """Сколько секунд станция не забирает байты (0 — читает или ещё не подключалась)."""
        if not self.last_sent_at or not self.connected.is_set():
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self.last_sent_at)


    def status(self) -> dict[str, Any]:
        elapsed = time.time() - self.created_at
        return {
            "token": self.token,
            "state": (
                "paused"
                if self.paused
                else "finished"
                if self.finished.is_set()
                else "streaming"
                if self.connected.is_set()
                else "waiting_station"
            ),
            "query": self.query,
            "title": self.title,
            "artist": self.artist,
            "duration": round(self.duration, 2),
            "content_length": self.content_length,
            "buffered": self.buffer.buffered_bytes,
            "bytes_sent": self.bytes_sent,
            "offset_ms": self.offset_ms,
            "played_ms": self.played_ms or self.estimate_played_ms(),
            "stalled_s": round(self.stalled_for(), 1),
            # Отрицательное/около нуля — Станция играет из своего буфера;
            # положительное — поток реально не читается.
            "behind_s": round(self.behind_s(), 1),
            "resume_offset": self.resume_offset,
            "paused": self.paused,
            "station_connected": self.connected.is_set(),
            # «Есть что продолжать» — для кнопки play/pause и кольца Станции.
            # Поток после паузы может быть уже закрыт (state=finished или
            # waiting_station), но трек продолжается с offset_ms, поэтому
            # ориентироваться только на state нельзя.
            "resumable": self.resumable(),
            "resume_epoch": self.resume_epoch,
            "elapsed": round(elapsed, 2),
            "error": self.error,
        }

    async def terminate(self) -> None:
        self.cancelled = True
        self.finished.set()
        if self.producer_task is not None and not self.producer_task.done():
            self.producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.producer_task
        self.producer_task = None
        await self.buffer.close()
        self.finished.set()


class StreamHub:
    def __init__(self, silence: bytes = b"\x00" * 65_536) -> None:
        self.current: StreamSession | None = None
        self.silence = silence or b"\x00" * 4096

    def install(self, session: StreamSession) -> None:
        self.current = session

    def clear(self, session: StreamSession | None = None) -> None:
        if session is None or self.current is session:
            self.current = None

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name", "")
        token = name[:-4] if name.endswith(".mp3") else name
        session = self.current
        if session is None or not session.accepts(token):
            _LOGGER.warning("HTTP: запрос устаревшего/неизвестного потока %s", token[:8])
            return web.Response(status=410, text="stream is not active")

        # Станция после audio_play с offset_ms может запросить Range: считаем
        # этот диапазон стартовой позицией (перекрывает resume_offset).
        range_header = request.headers.get("Range", "")
        start = min(session.resume_offset, session.content_length)
        partial = False
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if match:
                start = min(int(match.group(1)), session.content_length)
                partial = True
        remaining_total = session.content_length - start
        headers = {
            "Content-Type": "audio/mpeg",
            # Content-Length — остаток трека от позиции resume_offset: после
            # паузы Станция открывает поток заново, а байты до смещения уже
            # были отданы в прошлом соединении.
            "Content-Length": str(remaining_total),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        if partial:
            headers["Content-Range"] = (
                f"bytes {start}-{session.content_length - 1}/{session.content_length}"
            )
        _LOGGER.info(
            "HTTP: запрос потока token=%s Range=%r -> start=%d, статус=%d",
            token[:8],
            range_header,
            start,
            206 if partial else 200,
        )
        status = 206 if partial else 200
        # HEAD не должен «занимать» поток и мешать последующему GET Станции.
        if request.method == "HEAD":
            response = web.StreamResponse(status=status, reason="Partial Content" if partial else "OK",
                                          headers=headers)
            response.force_close()
            await response.prepare(request)
            await response.write_eof()
            return response

        if session.consumer_claimed:
            return web.Response(status=409, text="stream already being consumed")
        session.consumer_claimed = True
        session.handler_task = asyncio.current_task()
        session.connected.set()
        session.started_sending_at = time.time()
        session.stream_start_byte = start
        epoch = session.resume_epoch
        _LOGGER.info(
            "HTTP: Станция подключилась (token=%s, offset=%d, осталось %d байт)",
            token[:8],
            start,
            remaining_total,
        )

        response = web.StreamResponse(status=status, reason="Partial Content" if partial else "OK",
                                      headers=headers)
        response.force_close()
        await response.prepare(request)

        remaining = remaining_total
        silence_offset = 0
        pos = start
        try:
            # 1. Уже отданное до паузы — из history, начиная со смещения.
            if start < len(session.history) and remaining > 0:
                backlog = bytes(session.history[start:])
                if len(backlog) > remaining:
                    backlog = backlog[:remaining]
                if backlog:
                    await response.write(backlog)
                    session.last_sent_at = time.time()
                    pos += len(backlog)
                    remaining -= len(backlog)
            # 2. Дальше — живой поток ffmpeg.
            while remaining > 0:
                if session.resume_epoch != epoch:
                    # Нас сменило новое соединение после resume — не читаем буфер.
                    break
                chunk = await session.buffer.read()
                if chunk is None:
                    if session.cancelled:
                        break
                    # Источник закончился раньше расчётной длины — добиваем
                    # валидной MP3-тишиной (или нулями, если ffmpeg не смог).
                    while remaining > 0:
                        take = min(remaining, len(self.silence) - silence_offset)
                        piece = self.silence[silence_offset : silence_offset + take]
                        await response.write(piece)
                        session.history.extend(piece)
                        session.last_sent_at = time.time()
                        silence_offset += take
                        remaining -= take
                        pos += take
                        session.bytes_sent = pos
                        if silence_offset >= len(self.silence):
                            silence_offset = 0
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                await response.write(chunk)
                session.history.extend(chunk)
                session.last_sent_at = time.time()
                remaining -= len(chunk)
                pos += len(chunk)
                session.bytes_sent = pos
            await response.write_eof()
        except (ConnectionResetError, ConnectionAbortedError, ConnectionError, asyncio.CancelledError):
            # Станция закрыла/оборвала поток (пауза кольцом, переподключение) —
            # это не ошибка: сессию оставляем пригодной для продолжения.
            session.client_disconnected = True
            _LOGGER.info(
                "HTTP: Станция закрыла поток досрочно (%d/%d байт)",
                session.bytes_sent,
                session.content_length,
            )
        except Exception as exc:  # noqa: BLE001 - поток не должен ронять сервер
            session.client_disconnected = True
            session.error = repr(exc)
            _LOGGER.warning("HTTP: ошибка отдачи потока: %r", exc)
        finally:
            if session.handler_task is asyncio.current_task():
                session.handler_task = None
            if session.cancelled or remaining <= 0 or session.error:
                session.finished.set()
                _LOGGER.info(
                    "HTTP: поток завершён (%d/%d байт, %.1f c)",
                    session.bytes_sent,
                    session.content_length,
                    time.time() - session.started_sending_at,
                )
                await session.buffer.close()
                if session.producer_task is not None and not session.producer_task.done():
                    session.producer_task.cancel()
            else:
                # Станция закрыла поток (пауза кольцом/голосом): буфер НЕ
                # закрываем — после /api/resume отдадим остаток с этого байта.
                if epoch != session.resume_epoch:
                    # Пока это соединение умирало, уже случился resume и работает
                    # новый handler: его состояние не трогаем.
                    _LOGGER.info(
                        "HTTP: старое соединение закрыто после resume (epoch %d -> %d)",
                        epoch,
                        session.resume_epoch,
                    )
                    return response
                session.freeze_at_pause()
                session.consumer_claimed = False
                session.connected.clear()
                _LOGGER.info(
                    "HTTP: станция отключилась на %d/%d байт — сессия на паузе "
                    "(позиция %d мс, в буфере до %d мс)",
                    session.bytes_sent,
                    session.content_length,
                    session.played_ms,
                    session.offset_ms,
                )
        return response


class ControlApi(Protocol):
    async def play(self, query: str, *, directive: str | None = None) -> dict[str, Any]: ...
    async def stop(self) -> dict[str, Any]: ...
    async def pause(self, *, send_command: bool | None = None) -> dict[str, Any]: ...
    async def resume(
        self, *, send_command: bool | None = None, force: bool = False
    ) -> dict[str, Any]: ...
    async def next_track(self) -> dict[str, Any]: ...
    async def volume(
        self, *, delta: float | None = None, level: float | None = None
    ) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    async def handle_text(self, text: str) -> dict[str, Any]: ...


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        dumps=lambda obj: json.dumps(obj, ensure_ascii=False),
    )


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    supplied = request.headers.get("X-Api-Token", "")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, token)


def create_app(hub: StreamHub, control: ControlApi, api_token: str = "") -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)

    async def require_auth(request: web.Request) -> None:
        if request.path.startswith("/api/") and not _authorized(request, api_token):
            raise web.HTTPUnauthorized(text="invalid api token")

    async def index(_: web.Request) -> web.Response:
        return _json(
            {
                "service": "quasard-youtube",
                "ok": True,
                "stream_active": hub.current is not None,
            }
        )

    async def health(_: web.Request) -> web.Response:
        session = hub.current
        return _json(
            {
                "ok": True,
                "stream": session.status() if session else None,
            }
        )

    async def status(request: web.Request) -> web.Response:
        await require_auth(request)
        return _json(control.status())

    async def play(request: web.Request) -> web.Response:
        await require_auth(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - ContentTypeError/JSONDecodeError
            return _json({"ok": False, "error": "invalid json"}, 400)
        query = str(payload.get("query", "")).strip()
        if not query:
            return _json({"ok": False, "error": "query is required"}, 400)
        try:
            result = await control.play(query, directive=payload.get("directive"))
        except Exception as exc:  # noqa: BLE001 - API должен вернуть JSON
            _LOGGER.exception("API /api/play: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def stop(request: web.Request) -> web.Response:
        await require_auth(request)
        try:
            result = await control.stop()
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def command(request: web.Request) -> web.Response:
        await require_auth(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - ContentTypeError/JSONDecodeError
            return _json({"ok": False, "error": "invalid json"}, 400)
        text = str(payload.get("text", ""))
        try:
            result = await control.handle_text(text)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/command: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    # ── управление воспроизведением ─────────────────────────────────────

    async def body(request: web.Request) -> dict[str, Any]:
        """Тело необязательно: пустой POST — валидный запрос."""
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - ContentTypeError/JSONDecodeError
            return {}
        return payload if isinstance(payload, dict) else {}

    async def pause(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        send = payload.get("send_command")
        try:
            result = await control.pause(send_command=None if send is None else bool(send))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/pause: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def resume(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        send = payload.get("send_command")
        # force=true — не верить флагам сессии и переподключить поток целиком.
        force = bool(payload.get("force"))
        try:
            result = await control.resume(send_command=None if send is None else bool(send), force=force)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/resume: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def next_track(request: web.Request) -> web.Response:
        await require_auth(request)
        try:
            result = await control.next_track()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/next: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def volume(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        try:
            if payload.get("level") is not None:
                result = await control.volume(level=float(payload["level"]))
            else:
                result = await control.volume(delta=float(payload.get("delta", 1)))
        except (TypeError, ValueError) as exc:
            return _json({"ok": False, "error": f"invalid level/delta: {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/volume: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/play", play)
    app.router.add_post("/api/stop", stop)
    app.router.add_post("/api/command", command)
    app.router.add_post("/api/pause", pause)
    app.router.add_post("/api/resume", resume)
    app.router.add_post("/api/next", next_track)
    app.router.add_post("/api/volume", volume)
    app.router.add_get("/stream/{name}", hub.handle_stream, allow_head=True)
    return app
