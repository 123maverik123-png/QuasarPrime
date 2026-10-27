"""Оркестрация: поиск -> MP3-поток -> radio_play -> ожидание Станции."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any

from aiohttp import web

from .audio import ensure_ffmpeg, generate_silence, produce_mp3
from .command import parse_command
from .config import Config
from .glagol import GlagolClient
from .streaming import StreamBuffer, StreamHub, StreamSession, create_app
from .youtube import ResolveError, YoutubeResolver, looks_like_url

_LOGGER = logging.getLogger(__name__)

# Насколько Станция должна отстать от реального времени, чтобы считать поток
# нечитаемым. Станция забирает звук рывками на минуты вперёд, поэтому смотрим
# не на тишину в сокете, а на отставание позиции (StreamSession.behind_s).
_STALL_S = 3.0

# Сколько секунд ждём, пока закроется старый HTTP-поток Станции перед новым
# audio_play. Обычно мы снимаем свой handler сами, и это мгновенно; таймаут —
# страховка от зависшей записи в сокет.
_RELEASE_S = 2.0


class PlaybackError(RuntimeError):
    pass


class PlaybackService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.resolver = YoutubeResolver(config)
        self.glagol = GlagolClient(config)
        self.hub = StreamHub()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._closing = False
        # Текущий поисковый запрос и уже сыгранные video_id: по ним работает
        # «следующая» (повторный поиск по тому же запросу + пропуск сыгранного).
        self._query = ""
        self._played: list[str] = []
        self._paused = False

    async def start(self) -> None:
        ensure_ffmpeg(self.config)
        self.hub.silence = await generate_silence(self.config)
        app = create_app(self.hub, self, api_token=self.config.server.api_token)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.config.server.host,
            port=self.config.server.port,
            shutdown_timeout=3.0,
        )
        await self._site.start()
        _LOGGER.info(
            "HTTP-сервер слушает %s:%d, для Станции: %s",
            self.config.server.host,
            self.config.server.port,
            self.config.resolve_advertise_host(),
        )

    async def close(self) -> None:
        self._closing = True
        async with self._lock:
            await self._cancel_watch()
            session = self.hub.current
            if session is not None:
                await self._terminate_session(session)
                self.hub.clear(session)
        await self.glagol.close()
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None

    async def play(
        self,
        query: str,
        *,
        directive: str | None = None,
        skip: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        if self._closing:
            raise PlaybackError("Сервис завершает работу")
        query = query.strip()
        if not query:
            raise PlaybackError("Пустой поисковый запрос")
        if directive and directive not in {"radio_play", "audio_play", "auto"}:
            raise PlaybackError(f"Неизвестная директива: {directive}")

        async with self._lock:
            started = time.monotonic()
            # 1. Проверяем/дополняем станцию до расчёта streamUrl.
            if not self.config.station.host or not self.config.station.device_id:
                await self.glagol.discover_device()

            previous = self.hub.current

            # Новый запрос — новая очередь; тот же запрос — продолжаем листать.
            if query != self._query:
                self._query = query
                self._played = []
            skip_ids = frozenset(self._played) if skip is None else skip

            # 2. Метаданные YouTube.
            try:
                track = await self.resolver.resolve(query, skip=skip_ids)
            except ResolveError as exc:
                raise PlaybackError(str(exc)) from exc
            if track.duration > self.config.audio.max_duration_seconds:
                raise PlaybackError(
                    f"Трек длится {track.duration / 60:.0f} мин — больше лимита "
                    f"{self.config.audio.max_duration_seconds / 60:.0f} мин"
                )

            # Старый трек останавливаем только после успешного поиска нового.
            await self._cancel_watch()
            if previous is not None and not previous.finished.is_set():
                _LOGGER.info("Останавливаю предыдущий поток")
                with contextlib.suppress(Exception):
                    await self.glagol.stop()
                await self._terminate_session(previous)
                self.hub.clear(previous)

            # 3. Расчёт конечного Content-Length и запуск конвейера.
            content_length = int(
                (track.duration + self.config.audio.padding_seconds)
                * self.config.audio.bytes_per_second
            )
            if content_length < 16_384:
                raise PlaybackError("Слишком короткий трек для потоковой раздачи")
            token = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
            url = self.config.stream_url(token)
            buffer = StreamBuffer(
                max_buffer_bytes=max(
                    self.config.server.prebuffer_bytes + 131_072, 512 * 1024
                )
            )
            session = StreamSession(
                token=token,
                url=url,
                query=query,
                title=track.title,
                artist=track.artist,
                duration=track.duration,
                content_length=content_length,
                buffer=buffer,
            )
            self.hub.install(session)
            _LOGGER.info("play: новая сессия %s (%s)", token[:8], track.display_title)
            session.producer_task = asyncio.create_task(
                produce_mp3(
                    self.config,
                    self.resolver,
                    track,
                    buffer,
                    content_length,
                ),
                name=f"audio-{token[:8]}",
            )
            self._watch_task = asyncio.create_task(
                self._watch_producer(session), name=f"watch-{token[:8]}"
            )

            # 4. Ждём prebuffer, чтобы Станция не получила пустой HTTP-ответ.
            target = min(self.config.server.prebuffer_bytes, content_length)
            try:
                level_ok = await buffer.wait_for_level(
                    target, self.config.server.prebuffer_timeout
                )
                if buffer.error is not None:
                    raise PlaybackError(f"Ошибка аудиопотока: {buffer.error}")
                if session.producer_task.done():
                    error = session.producer_task.exception()
                    if error is not None:
                        raise PlaybackError(f"Не удалось получить аудио: {error}")
                if buffer.buffered_bytes <= 0:
                    raise PlaybackError(
                        "Аудиопоток не выдал данных за "
                        f"{self.config.server.prebuffer_timeout:.0f} с "
                        "(проверьте yt-dlp/ffmpeg и доступ к YouTube)"
                    )
                if not level_ok:
                    _LOGGER.warning(
                        "Prebuffer не достиг %d байт (есть %d), продолжаю: трек короткий",
                        target,
                        buffer.buffered_bytes,
                    )
            except PlaybackError:
                await self._abort(session)
                raise
            except asyncio.CancelledError:
                await self._abort(session)
                raise

            # 5. Команда Станции.
            if self.config.playback.stop_before_play and previous is not None:
                await asyncio.sleep(0.15)
            try:
                ack = await self.glagol.play(
                    url,
                    track.title,
                    track.artist,
                    directive=directive,
                )
            except Exception as exc:  # noqa: BLE001 - Glagol может упасть по-разному
                await self._abort(session, stop_glagol=True)
                if isinstance(exc, PlaybackError):
                    raise
                raise PlaybackError(f"Glagol: {exc}") from exc

            # 6. Ждём, пока Станция откроет HTTP-поток. Это лучший индикатор,
            # что команда принята и воспроизведение реально началось.
            try:
                await asyncio.wait_for(
                    session.connected.wait(),
                    self.config.server.station_connect_timeout,
                )
            except asyncio.TimeoutError as exc:
                await self._abort(session, stop_glagol=True)
                raise PlaybackError(
                    "Станция не подключилась к локальному HTTP-потоку за "
                    f"{self.config.server.station_connect_timeout:.0f} с. "
                    "Проверьте firewall, server.advertise_host и доступность "
                    f"{url}"
                ) from exc

            elapsed = time.monotonic() - started
            self._played.append(track.video_id)
            self._paused = False
            _LOGGER.info(
                "Играю %r (%s) — %.1f c, stream=%s",
                track.display_title,
                "audio_play" if directive == "audio_play" else "radio_play",
                track.duration,
                url,
            )
            return {
                "title": track.title,
                "artist": track.artist,
                "duration": round(track.duration, 2),
                "query": query,
                "stream_url": url,
                "content_length": content_length,
                "glagol": ack,
                "startup_seconds": round(elapsed, 2),
                "played_count": len(self._played),
                "session": session.status(),
            }

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            session = self.hub.current
            if session is None:
                return {"stopped": False, "reason": "nothing is playing"}
            with contextlib.suppress(Exception):
                await self.glagol.stop()
            await self._terminate_session(session)
            self.hub.clear(session)
            await self._cancel_watch()
            self._query = ""
            self._played.clear()
            self._paused = False
            return {"stopped": True, "title": session.title}

    # ── пауза / продолжение / следующий / громкость ─────────────────────
    #
    # Пауза и продолжение по умолчанию выполняются директивой Станции
    # (player_pause / player_continue), которую отправляет quasard вместе с
    # VinsResponse: только так поток остаётся открытым, а «продолжай»
    # возвращает трек с той же секунды. Сервис ведёт состояние и переподключает
    # поток, когда прошивка сама этого не делает.

    async def pause(self, *, send_command: bool | None = None) -> dict[str, Any]:
        session = self.hub.current
        if session is None:
            return {"paused": False, "reason": "nothing is playing"}
        if send_command is None:
            send_command = self.config.playback.pause_command
        # Позицию паузы фиксируем РОВНО ОДИН раз: повторная «пауза»/«поставь на
        # паузу» её не двигает (иначе offset_ms уезжает вперёд на время простоя).
        session.freeze_at_pause()
        if send_command:
            with contextlib.suppress(Exception):
                await self.glagol.pause()
        self._paused = True
        _LOGGER.info(
            "Пауза%s на %d байт (позиция %d мс, отдано в буфер %d мс): %s",
            " (Glagol stop)" if send_command else "",
            session.resume_offset,
            session.played_ms,
            session.offset_ms,
            session.title,
        )
        return {
            "paused": True,
            "sent_command": send_command,
            "offset_ms": session.played_ms,
            "offset_bytes": session.resume_offset,
            "title": session.title,
        }

    async def resume(
        self, *, send_command: bool | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Продолжить трек с того же места.

        Разбор состояний (раньше здесь была одна общая ветка, и любая ошибка
        состояния превращала «продолжай» в пустое действие):

        * поток завершён/остановлен — включаем трек заново с начала;
        * поток жив и Станция его читает — продолжать нечего;
        * поток висит (пауза, Станция отвалилась, отстала от реального
          времени больше чем на 3 с) — переподключение: ``stop``, освобождение
          старого сокета, ``audio_play`` с ``offset_ms`` и новым url, ожидание
          нового запроса потока; не вышло — вторая попытка, затем перезапуск
          трека целиком (лучше музыка с начала, чем тишина).

        ``send_command`` оставлен для совместимости API: транспортная команда
        Glagol ``{"command":"play"}`` с audio_client-сессией ничего не делает
        (лог 20:33:49 и 20:33:56 — два ``play`` подряд, тишина), поэтому вместо
        неё всегда выполняется реальное переподключение потока.
        """
        session = self.hub.current
        if session is None:
            return {"resumed": False, "reason": "nothing is playing"}
        if session.cancelled:
            return {"resumed": False, "reason": "session cancelled"}

        offset_bytes, offset_ms = session.resume_position()

        if session.finished.is_set():
            # Жёсткая пауза или поток закрыт: включаем трек заново.
            _LOGGER.info("Поток закрыт, перезапускаю трек (%s)", session.title)
            result = await self._replay(session)
            if result is None:
                return {"resumed": False, "reason": "nothing to replay"}
            self._paused = False
            return {"resumed": True, "replayed": True, "reconnected": False, **result}

        reconnect, why = self._needs_reconnect(session, force=force)
        if not reconnect:
            self._paused = False
            _LOGGER.info("Продолжение не нужно: поток жив (%s)", why)
            return {
                "resumed": True,
                "replayed": False,
                "reconnected": False,
                "offset_ms": offset_ms,
                "offset_bytes": offset_bytes,
                "sent_command": False,
                "title": session.title,
            }

        return await self._resume_reconnect(session, offset_bytes, offset_ms, why)

    @staticmethod
    def _needs_reconnect(session: StreamSession, *, force: bool = False) -> tuple[bool, str]:
        """Нужно ли переподключать поток, чтобы «продолжай» реально сработало."""
        if force:
            return True, "force"
        if session.finished.is_set():
            return True, "stream finished"
        if not session.connected.is_set():
            # Станция не держит поток: пауза кольцом, обрыв, неудачный resume.
            return True, "station detached"
        if session.paused:
            return True, "paused"
        # ВАЖНО: не stalled_for(), а behind_s(). Станция забирает поток рывками
        # на минуты вперёд и потом молчит в сокете, пока не доиграет буфер — это
        # нормальное воспроизведение, а не пауза (проверено: 21:22 во время
        # теста байты не шли 13 с, музыка при этом играла).
        behind = session.behind_s()
        if behind > _STALL_S:
            return True, f"station behind {behind:.1f}s"
        return False, "stream alive"

    def _rotate_stream_url(self, session: StreamSession) -> str:
        """Новый url для того же потока: Станция не должна узнать старую ссылку.

        ``audio_play`` с уже загруженным url прошивка иногда игнорирует — поток
        при этом не переоткрывается (лог 20:13:13 и 20:33:43: команда ушла,
        запроса ``Range`` от Станции не последовало). Новый токен делает ссылку
        незнакомой, а отдаёт её та же сессия из того же буфера и history.
        """
        token = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        session.aliases.append(token)
        del session.aliases[:-8]  # старые ссылки Станции больше не нужны
        session.url = self.config.stream_url(token)
        return token

    async def _resume_reconnect(
        self, session: StreamSession, offset_bytes: int, offset_ms: int, why: str
    ) -> dict[str, Any]:
        timeout_connect = self.config.playback.resume_connect_timeout
        timeout_command = self.config.playback.resume_command_timeout
        attempts = max(1, int(self.config.playback.resume_attempts))
        # До доказательства обратного считаем, что потока у Станции нет.
        session.paused = False
        session.consumer_claimed = False
        session.resume_offset = offset_bytes
        session.resume_epoch += 1  # старый handler больше не трогает состояние
        session.connected.clear()
        token = ""
        for attempt in range(1, attempts + 1):
            baseline = session.last_sent_at
            token = self._rotate_stream_url(session)
            _LOGGER.info(
                "Продолжение %d/%d: переподключение с %d мс (байт %d/%d, причина: %s): %s",
                attempt,
                attempts,
                offset_ms,
                offset_bytes,
                session.content_length,
                why,
                session.title,
            )
            try:
                await asyncio.wait_for(
                    self._glagol_reconnect(session, offset_ms), max(1.0, timeout_command)
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Продолжение: Glagol не ответил за %.1f с (команда могла уйти)",
                    timeout_command,
                )
            except Exception as exc:  # noqa: BLE001 - не теряем состояние сессии
                session.paused = True
                _LOGGER.warning("Продолжение не отправлено: %r", exc)
                return {"resumed": False, "reason": str(exc), "offset_ms": offset_ms}

            if await self._wait_stream_alive(session, timeout_connect, baseline):
                # Позицию зафиксировали в offset_ms — дальше её считает новое
                # соединение станции (старт Range + время чтения).
                session.played_ms = 0
                self._paused = False
                _LOGGER.info(
                    "Продолжение принято: Станция снова читает поток (url=%s, %.1f c)",
                    token[:8],
                    time.time() - baseline if baseline else 0.0,
                )
                return {
                    "resumed": True,
                    "replayed": False,
                    "reconnected": True,
                    "offset_ms": offset_ms,
                    "offset_bytes": offset_bytes,
                    "sent_command": True,
                    "attempts": attempt,
                    "title": session.title,
                }
            _LOGGER.warning(
                "Продолжение %d/%d: Станция не открыла поток за %.1f с после audio_play(offset=%d мс)",
                attempt,
                attempts,
                timeout_connect,
                offset_ms,
            )

        if not self.config.playback.resume_escalate_play:
            session.paused = True
            return {
                "resumed": False,
                "reason": "station did not reconnect",
                "offset_ms": offset_ms,
            }
        result = await self._replay(session)
        if result is None:
            session.paused = True
            return {
                "resumed": False,
                "reason": "station did not reconnect",
                "offset_ms": offset_ms,
            }
        self._paused = False
        return {
            "resumed": True,
            "replayed": True,
            "reconnected": False,
            "offset_ms": offset_ms,
            **result,
        }

    async def _glagol_reconnect(self, session: StreamSession, offset_ms: int) -> dict[str, Any]:
        """``stop`` -> освободить сокет -> ``audio_play`` с ``offset_ms``.

        Между stop и audio_play обязательно ждём, пока закроется старый
        HTTP-поток: пока Станция его держит, новый audio_play она игнорирует и
        трек не продолжается (лог 21:24:17: stop и audio_play в одну секунду,
        поток закрылся в 21:24:22, новую ссылку Станция не открыла). Сокет
        освобождаем сами — снимаем свой handler, не надеясь на прошивку.
        """
        await self.glagol.stop()
        session.release_stream()
        await self._wait_stream_released(session, _RELEASE_S)
        await asyncio.sleep(0.2)
        return await self.glagol.play(
            session.url, session.title, session.artist, offset_ms=offset_ms
        )

    async def _wait_stream_released(self, session: StreamSession, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.2, timeout)
        while time.monotonic() < deadline:
            if not session.stream_held():
                return True
            await asyncio.sleep(0.05)
        return not session.stream_held()

    async def _replay(self, session: StreamSession) -> dict[str, Any] | None:
        """Включить трек заново с начала (последняя ступень продолжения)."""
        query = (self._query or session.query).strip()
        if not query:
            return None
        _LOGGER.info("Перезапускаю трек целиком: %r", query)
        return await self.play(query, skip=frozenset())

    async def _wait_stream_alive(
        self, session: StreamSession, timeout: float, baseline: float
    ) -> bool:
        """Дождаться доказательства, что Станция снова читает поток.

        Признаки: новый HTTP-запрос к потоку (``connected``) либо байты снова
        пошли в сокет по старому соединению (Станция могла просто продолжить
        читать уже открытый поток, без переподключения).
        """
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            # Сначала «подключилась»: короткий остаток трека может доиграть
            # внутри этого окна и выставить finished — это всё равно успех.
            if session.connected.is_set():
                return True
            if session.cancelled or session.finished.is_set():
                return False
            if session.last_sent_at > baseline:
                return True
            await asyncio.sleep(0.1)
        return session.connected.is_set()

    async def next_track(self) -> dict[str, Any]:
        """Следующий результат по текущему запросу (простая очередь из поиска)."""
        session = self.hub.current
        if session is None:
            return {"played": False, "reason": "nothing is playing"}
        query = (self._query or session.query).strip()
        if looks_like_url(query):
            # Сессия включена по прямой ссылке: «следующая» по той же ссылке
            # вернула бы тот же ролик, поэтому ищем по исполнителю и названию.
            query = " ".join(x for x in (session.artist, session.title) if x).strip() or query
        if not query:
            return {"played": False, "reason": "unknown query"}
        try:
            result = await self.play(query)
        except PlaybackError as exc:
            _LOGGER.warning("Следующий трек не включён: %s", exc)
            return {"played": False, "reason": str(exc)}
        return {"played": True, **result}

    async def volume(
        self, *, delta: float | None = None, level: float | None = None
    ) -> dict[str, Any]:
        if level is not None:
            await self.glagol.set_volume(float(level))
            return {"volume": round(float(level), 2), "step": 0}
        step = float(1 if delta is None else delta)
        result = await self.glagol.volume_delta(step * self.config.playback.volume_step)
        return {**result, "step": step}

    def status(self) -> dict[str, Any]:
        session = self.hub.current
        return {
            "service": "quasard-youtube",
            "ok": True,
            "paused": self._paused,
            "queue": {"query": self._query, "played": len(self._played)},
            "station": {
                "host": self.config.station.host,
                "port": self.config.station.port,
                "device_id": self.config.station.device_id,
                "connected": self.glagol.state.connected,
                "audio_client": self.glagol.state.audio_client,
                "last_error": self.glagol.state.last_error,
            },
            "stream": session.status() if session else None,
            "advertise_host": self._safe_advertise_host(),
        }

    def _safe_advertise_host(self) -> str:
        try:
            return self.config.resolve_advertise_host()
        except Exception:  # noqa: BLE001
            return ""

    async def handle_text(self, text: str) -> dict[str, Any]:
        parsed = parse_command(text)
        if not parsed.matched:
            return {
                "matched": False,
                "text": text,
                "spoken_text": text,
                "query": "",
            }
        result = await self.play(parsed.query)
        spoken = parsed.spoken_text or self.config.quasard.empty_reply
        return {
            "matched": True,
            "text": spoken,
            "spoken_text": spoken,
            "query": parsed.query,
            "playback": result,
        }

    async def _abort(self, session: StreamSession, *, stop_glagol: bool = False) -> None:
        if stop_glagol:
            with contextlib.suppress(Exception):
                await self.glagol.stop()
        await self._terminate_session(session)
        self.hub.clear(session)
        await self._cancel_watch()

    async def _terminate_session(self, session: StreamSession) -> None:
        """Не дать зависшему ffmpeg/yt-dlp навсегда оставить сессию в waiting_station."""
        try:
            await asyncio.wait_for(session.terminate(), 12.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("Не удалось корректно остановить аудиопроцессы за 12 с")
            session.cancelled = True
            await session.buffer.close()
            session.finished.set()

    async def _cancel_watch(self) -> None:
        """Снять наблюдатель за потоком, но ждать ограниченно.

        Если Станция перестала читать HTTP-поток, производитель аудио может
        зависнуть в ``buffer.write()`` и не отреагировать на отмену. Ждать его
        без таймаута нельзя: play() держит self._lock, и весь API встанет
        навсегда (проверено: /api/next висел 4 минуты).
        """
        task = self._watch_task
        self._watch_task = None
        if task is None or task.done():
            return
        _LOGGER.info("watch: отменяю наблюдение за потоком")
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 6.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "watch: задача не завершилась за 6 с (аудиопроцесс завис) — продолжаю без неё"
            )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _watch_producer(self, session: StreamSession) -> None:
        task = session.producer_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            session.error = str(exc)
            _LOGGER.error("Аудиопоток упал: %s", exc)
            if session.connected.is_set() and not session.finished.is_set():
                with contextlib.suppress(Exception):
                    await self.glagol.stop()


__all__ = ["PlaybackService", "PlaybackError"]
