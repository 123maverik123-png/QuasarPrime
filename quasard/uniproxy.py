"""Эмуляция uniproxy: WebSocket /uni.ws (JSON-протокол SpeechKit, как в прошивке 1.56).

Сценарий голосового запроса (по HAR):
  -> System.SynchronizeState            <- System.SynchronizeStateResponse {SessionId}, System.Push(notify)
  -> System.Ack
  -> Vins.VoiceInput (streamId=1)       <- System.InputStartAck
  -> бинарные фреймы [streamId][Ogg/Opus 16k]
  -> streamcontrol action=2 (конец фразы-активации), не закрывает поток
                                        <- ASR.Result (партиалы), ASR.Result endOfUtt=true
                                        <- streamcontrol close stream 1
  -> streamcontrol close stream 1       <- Spotter.Validation, Vins.VinsResponse,
                                           TTS.Speak (streamId=чётный), [streamId][Ogg/Opus 48k], streamcontrol close
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from . import aliceapi, intents, music, protocol as P
from .asr import EndpointDetector, Recognizer
from .audio import OggOpusStreamEncoder, OpusStreamDecoder, frame_stream, parse_stream_frame, split_ogg_pages
from .config import Config, opt_seconds
from .llm import Dialog
from .music import Music
from .tts import Synthesizer
from .youtube_control import YoutubeControl

log = logging.getLogger("uniproxy")

# Фоновые задачи, не привязанные к WS-сессии (например, запуск песни-пасхалки).
# Задачи сессии (``_spawn``) отменяются при отключении станции — а песня должна
# догрузиться, даже если станция в этот момент переподключит WS.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(_log_task_error)
    return task


def _ping_timestamp(payload) -> int | None:
    """``TPing.Timestamp`` из входящего Ping — в любом из двух видов протокола.

    * прошивка 1.56 (JSON): ``{"payload": {"timestamp": 1793111854, ...}}``;
    * прошивка 3.x (AAPI): ``{"payload": {"proto": {"Timestamp": 1793111854}}}``
      — ``aliceapi.client_to_json`` оставляет proto-тело как есть, с
      CamelCase-именами полей.

    ``Timestamp`` в схеме — uint32 (секунды эпохи), поэтому маска 0xFFFFFFFF.
    """
    if not isinstance(payload, dict):
        return None
    for source in (payload, payload.get("proto")):
        if not isinstance(source, dict):
            continue
        for key in ("timestamp", "Timestamp"):
            value = source.get(key)
            if value is None:
                continue
            try:
                return int(value) & 0xFFFFFFFF
            except (TypeError, ValueError):
                continue
    return None

# streamcontrol.action (по прошивке: Close=0, SpotterEnd=2, LazyTTS=3 с полем size)
STREAM_CLOSE = 0
STREAM_SPOTTER_END = 2
# streamcontrol.reason для Close: OK=0, INTERNAL_ERROR=2, STOP_RECORDING=5, AUDIO_SOURCE_STOPPED=6,
# CANCEL=7, NO_SPEECH=8
REASON_CANCEL = 7
REASON_NO_SPEECH = 8

# ── сенсорная кнопка (кольцо) Станции ───────────────────────────────────
# Станция присылает server_action с этим именем; разметку intents ещё
# уточняем по реальным логам, поэтому наборы ниже — расширяемые.
SF_NAME = "@@mm_semantic_frame"
SF_PAUSE = {"music_pause", "player_pause", "pause", "pause_music", "music.pause"}
SF_RESUME = {"music_play", "music_resume", "music_continue", "player_continue",
             "play", "resume", "continue"}
SF_TOGGLE = {"toggle", "play_pause", "pause_play", "music_toggle", "player_toggle"}
# ключи, в которых станция может прислать название интента/действия
SF_ACTION_KEYS = ("intent", "intent_name", "action", "player_action", "command", "type")

# Сколько секунд после голосовой команды плееру игнорировать кадр кольца.
# Станция шлёт один и тот же PlayerContinueSemanticFrame и от кнопки, и как
# эхо распознанного «продолжай»/«пауза» — приходит примерно через 0.1–1 c
# после ASR (в логе: resume 20:22:56.255 -> кадр 20:22:56.345 -> pause).
RING_MUTE_S = 4.0


@dataclass
class VoiceRequest:
    ref: str  # messageId события VoiceInput
    stream_id: int
    request_id: str
    sequence_number: int
    spotter_validation: bool
    decoder: OpusStreamDecoder
    detector: EndpointDetector
    started: float = field(default_factory=time.monotonic)
    finalized: bool = False
    partial_no: int = 0


class Services:
    """Общие для всех соединений тяжёлые объекты."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.recognizer = Recognizer(cfg.asr)
        self.dialog = Dialog(cfg.llm, cfg.device.timezone)
        self.tts = Synthesizer(cfg.tts, cfg.data_path)
        self.music = Music(cfg.music, cfg.data_path)
        self.youtube = YoutubeControl(cfg.youtube)
        self.connections: dict[str, set["UniproxySession"]] = {}
        # Окно «не верить кадру кольца» живёт здесь, а не в сессии: Станция
        # присылает эхо-кадр @@mm_semantic_frame уже в НОВОМ WebSocket (в логе
        # каждый кадр идёт сразу после «WS подключение»), поэтому флаг на
        # объекте сессии до него не доживал. Лог 20:22:56: «Продолжай» ->
        # /api/resume -> кадр через 0.09 c -> «Кольцо: переключатель -> pause»,
        # и музыка замолкала сразу после продолжения.
        self.ring_mute_until: dict[str, float] = {}
        self.ring_mute_why: dict[str, str] = {}


class UniproxySession:
    def __init__(self, ws: WebSocket, svc: Services) -> None:
        self.ws = ws
        self.svc = svc
        self.cfg = svc.cfg
        h = ws.headers
        self.device_id = h.get("x-uprx-device-id", "unknown")
        self.session_id = h.get("x-uprx-ssid") or str(uuid.uuid4())
        self.uuid = h.get("x-uprx-uuid", "")
        self.state: dict = {}
        self.proto = False  # станция прислала AAPI-кадр -> отвечаем protobuf-ом (прошивки 3.x)
        self._send_lock = asyncio.Lock()
        self._next_out_stream = 2  # серверные потоки — чётные
        self._voice: dict[int, VoiceRequest] = {}
        self._tasks: set[asyncio.Task] = set()
        self._current: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        # Окно «не верить кадру кольца» — в Services (общее для всех соединений,
        # см. Services.__init__): эхо-кадр приходит уже в новом WebSocket.
        self._pings_sent = 0       # keep-alive-кадров отправлено
        self._pings_answered = 0   # на них ответили (System.Ack или эхо-Ping)
        self._pongs = 0            # именно эхо-Ping = «станция жива»
        self._last_rx = time.monotonic()  # последний ЛЮБОЙ кадр от станции (в т.ч. аудио)
        self._last_tx = time.monotonic()
        self._pending_ping_ts: int | None = None  # Timestamp нашего Ping, ждём эхо
        self._ping_echo_done: deque[int] = deque(maxlen=8)  # защита от петли Ping↔Ping
        self._silence_warned_at = 0.0  # чтобы warning про тишину не спамил каждые N секунд
        self._connected_at = time.monotonic()
        self._traffic = None
        if self.cfg.server.log_traffic:
            d = self.cfg.data_path / "traffic"
            d.mkdir(parents=True, exist_ok=True)
            self._traffic = open(d / f"{time.strftime('%Y%m%d')}_{self.device_id}.jsonl", "a", encoding="utf-8")

    # ------------------------------------------------------------ io

    def _log_traffic(self, direction: str, msg) -> None:
        if self._traffic is None:
            return
        rec = {"t": time.time(), "dir": direction, "ssid": self.session_id}
        rec["msg" if isinstance(msg, dict) else "bin"] = msg if isinstance(msg, dict) else len(msg)
        self._traffic.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._traffic.flush()

    async def send_json(self, msg: dict) -> None:
        """Отправка сообщения сервера. Внутри всё в JSON-формате 1.56; для прошивок 3.x — в proto."""
        self._log_traffic("out", msg)
        if self.proto:
            pm = aliceapi.server_to_proto(msg)
            if pm is None:
                return
            data = aliceapi.encode_server(pm)
            async with self._send_lock:
                await self.ws.send_bytes(data)
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(data)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        t.add_done_callback(_log_task_error)
        return t

    # ------------------------------------------------------------ main loop

    async def run(self) -> None:
        log.info("WS подключение: device=%s ssid=%s", self.device_id, self.session_id)
        self.svc.connections.setdefault(self.device_id, set()).add(self)
        self._start_keepalive()
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    self._log_close(msg.get("code"), msg.get("reason"))
                    break
                # любой кадр от станции (в том числе аудио) = линк живой
                self._last_rx = time.monotonic()
                if msg.get("text") is not None:
                    await self._on_text(msg["text"])
                elif msg.get("bytes") is not None:
                    await self._on_binary(msg["bytes"])
        except WebSocketDisconnect as exc:
            # Starlette бросает это, когда disconnect пришёл исключением, а не сообщением
            self._log_close(getattr(exc, "code", None), getattr(exc, "reason", None))
        finally:
            self._stop_keepalive()
            for t in list(self._tasks):
                t.cancel()
            self.svc.connections.get(self.device_id, set()).discard(self)
            if self._traffic:
                self._traffic.close()
            log.info("WS отключение: device=%s ssid=%s", self.device_id, self.session_id)

    # ------------------------------------------------------------ keep-alive

    def _log_close(self, code, reason) -> None:
        """Кто и как закрыл WS + сколько молчал линк.

        1005 — TCP-обрыв без close-кадра (станция или сеть), 1000/1001 — станция
        уходит штатно, 1011 — сервер сам закрыл по таймауту keep-alive.
        """
        hints = {1000: "штатное закрытие", 1001: "станция уходит/перезапуск", 1002: "ошибка протокола",
                 1005: "TCP-обрыв без close-кадра (станция или сеть)", 1006: "обрыв без close-кадра",
                 1011: "сервер закрыл: нет PONG на WS-ping", 1012: "перезапуск сервиса", 1013: "перегружен"}
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        log.info("станция закрыла WS: code=%s reason=%r (%s) — прожили %.0f c, тишина на входе %.0f c, "
                 "keep-alive отправлено %d, подтверждений %d, pong %d",
                 code, reason, hints.get(code_int, "?"),
                 time.monotonic() - self._connected_at, time.monotonic() - self._last_rx,
                 self._pings_sent, self._pings_answered, self._pongs)

    def _start_keepalive(self) -> None:
        """Фоновая задача прикладного keep-alive (протокол Алисы).

        Транспортные PING-фреймы шлёт uvicorn — это отдельный слой, его настройки
        ``server.ws_ping_interval`` / ``ws_ping_timeout`` (см. __main__.py).
        """
        mode = str(getattr(self.cfg.server, "ws_keepalive_mode", "push") or "none").strip().lower()
        interval = opt_seconds(getattr(self.cfg.server, "ws_keepalive_interval", 5.0), 5.0)
        if mode in ("", "none", "off") or not interval:
            return
        if mode not in ("push", "ping"):
            log.warning("keep-alive: неизвестный режим %r (ожидал push/ping/none) — выключаю", mode)
            return
        self._keepalive = asyncio.create_task(self._keepalive_loop(interval, mode))

    def _stop_keepalive(self) -> None:
        if self._keepalive is None:
            return
        self._keepalive.cancel()
        self._keepalive = None
        if self._pings_sent:
            log.info("keep-alive итог: отправлено %d, ответов станции %d (pong %d) за %.0f c (ssid=%s)",
                     self._pings_sent, self._pings_answered, self._pongs,
                     time.monotonic() - self._connected_at, self.session_id)

    async def _keepalive_loop(self, interval: float, mode: str) -> None:
        """Прикладной keep-alive раз в ``interval`` секунд.

        ``push`` — пустой ``System.Push``: он есть в ``SupportedServerEvents``
        станции, станция отвечает ``System.Ack``, и по этому Ack видно, что она
        нас слышит. ``ping`` — ``System.Ping`` (``TPing``): станция этот кадр в
        списке поддерживаемых НЕ объявляет, поэтому режим не по умолчанию.

        Пока идут пинги и станция отвечает, соединение «живое» на уровне
        протокола; тишина дольше трёх интервалов — повод для warning в логе.
        """
        timeout_ms = int(getattr(self.cfg.server, "ws_ping_timeout_ms", 0) or 0)
        try:
            while True:
                await asyncio.sleep(interval)
                msg = None
                if mode == "ping":
                    ts = int(time.time()) & 0xFFFFFFFF
                    msg = P.ping(timestamp_s=ts, ping_timeout_ms=timeout_ms, pong_timeout_ms=timeout_ms)
                    self._pending_ping_ts = ts  # ждём эхо с этим же Timestamp
                else:
                    msg = P.push_empty()
                try:
                    await self.send_json(msg)
                except Exception:  # noqa: BLE001 - соединение могло закрыться
                    log.debug("keep-alive: не отправлено", exc_info=True)
                    return
                self._pings_sent += 1
                self._last_tx = time.monotonic()
                silence = time.monotonic() - self._last_rx
                if self._pings_sent == 1 or self._pings_sent % 20 == 0:
                    log.info("keep-alive %s #%d: ответов %d, тишина на входе %.0f c (ssid=%s)",
                             mode, self._pings_sent, self._pings_answered, silence, self.session_id)
                if self._pings_sent >= 3 and silence > interval * 3 and \
                        time.monotonic() - self._silence_warned_at > max(60.0, interval * 4):
                    self._silence_warned_at = time.monotonic()
                    log.warning("keep-alive: станция молчит %.0f c при интервале %.0f c — "
                                "соединение, скорее всего, уже мертво", silence, interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("keep-alive: цикл упал")


    async def _on_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Не JSON: %.200s", text)
            return
        await self._on_message(msg)

    async def _on_message(self, msg: dict) -> None:
        self._log_traffic("in", msg)
        if "streamcontrol" in msg:
            await self._on_stream_control(msg["streamcontrol"])
            return
        event = msg.get("event")
        if not event:
            log.info("Неизвестное сообщение: %.300s", json.dumps(msg, ensure_ascii=False))
            return
        header = event.get("header", {})
        name = f"{header.get('namespace')}.{header.get('name')}"
        payload = event.get("payload", {})
        req_event = (payload.get("request") or {}).get("event") or {}
        if header.get("namespace") == "Vins" and req_event.get("type") == "server_action":
            # колбэки плеера и @@mm_stack_engine_get_next — станция возвращает нам server_action-директивы
            await self._on_server_action(header, payload, req_event)
            return
        handler = {
            "System.SynchronizeState": self._on_sync_state,
            "System.Ack": self._on_ack,
            "System.Ping": self._on_client_ping,
            "System.Pong": self._on_client_pong,
            "System.EchoRequest": self._on_echo,
            "Vins.CancelRequest": self._on_cancel,
            "Log.Spotter": self._on_log_spotter,
            "Vins.VoiceInput": self._on_voice_input,
            "ASR.Recognize": self._on_voice_input,
            "Vins.TextInput": self._on_text_input,
            "Vins.VinsRequest": self._on_text_input,
            "Vins.CustomInput": self._on_text_input,
            "TTS.Generate": self._on_tts_generate,
        }.get(name, "unknown")
        if handler is None:
            return
        if handler == "unknown":
            if header.get("name") == "RequestStat":
                # без подтверждения станция повторяет RequestStat
                await self.send_json(P.directive("Log", "RequestStatAck", {}, ref=header.get("messageId")))
                return
            if name.startswith("Log.") or name.startswith("Spotter."):
                return
            if header.get("namespace") == "Proto":
                # Прошивки 3.x шлют события, которых нет в нашем конвейере
                # (MatchedUser/VoiceprintMatch и т.п.). Ждут ответ на свой
                # RequestId — без него колонка рвёт WS и переподключается.
                if not getattr(self.cfg.server, "proto_ack", True):
                    log.info("Событие %s — игнорирую (server.proto_ack=false)", name)
                    return
                await self._on_proto_event(header, name, payload)
                return
            log.info("Необработанное событие %s: %.500s", name, json.dumps(payload, ensure_ascii=False))
            if header.get("namespace") == "Vins":
                await self._respond_empty(header.get("messageId", ""), payload)
            return
        await handler(header, payload)

    async def _on_binary(self, data: bytes) -> None:
        if len(data) < 4:
            return
        if data.startswith(aliceapi.MAGIC):
            # прошивка 3.x: события идут protobuf-ом — переключаем сессию и переводим в JSON-вид
            if not self.proto:
                log.info("Станция говорит protobuf-протоколом (AAPI)")
                self.proto = True
            try:
                msg = aliceapi.client_to_json(aliceapi.decode_client(data))
            except Exception:
                log.exception("Не удалось разобрать AAPI-сообщение")
                return
            if msg is not None:
                await self._on_message(msg)
            return
        stream_id, chunk = parse_stream_frame(data)
        req = self._voice.get(stream_id)
        if req is None or req.finalized:
            return
        try:
            req.detector.feed(req.decoder.feed(chunk))
        except Exception:
            log.exception("Ошибка обработки аудио")
            return
        if req.detector.is_end():
            await self._finalize_voice(req, reason="vad")

    # ------------------------------------------------------------ proto-события

    async def _on_proto_event(self, header: dict, name: str, payload: dict) -> None:
        """Событие прошивки 3.x, которого нет в нашем конвейере -> пустой ответ.

        Колонка отправляет такое событие на собственный RequestId и ждёт
        AliceResponse. Пока ответа нет, запрос для неё «висящий»: она считает
        сессию битой, закрывает WS и подключается заново — в логе это и видно
        как «WS подключение / WS отключение» каждые несколько секунд.

        Отвечаем сразу пустым ответом (без речи и без директив) — это по сути
        ACK. Диалог он не сбивает: настоящий запрос (VoiceInput/TextInput)
        приходит отдельным событием со своим RequestId.

        ``Proto.MatchedUser`` — отчёт сопоставления голоса
        (EventSource.Type = VoiceprintMatch, GuestUserOptions.Status = NoMatch).
        Ничего делать по нему не нужно, только подтвердить.
        """
        proto = payload.get("proto") or {}
        req_id = (proto.get("Header") or {}).get("RequestId") or header.get("messageId", "")
        if name == "Proto.MatchedUser":
            source = proto.get("EventSource") or {}
            options = (proto.get("Request") or {}).get("AdditionalOptions") or {}
            guest = options.get("GuestUserOptions") or {}
            log.info("MatchedUser: %s/%s — пустой ACK (request_id=%s)",
                     source.get("Type"), guest.get("Status"), req_id)
        else:
            log.info("Событие %s — пустой ACK (request_id=%s)", name, req_id)
        await self._respond_empty(header.get("messageId", ""), payload)

    # ------------------------------------------------------------ System

    async def _on_sync_state(self, header: dict, payload: dict) -> None:
        self.state = payload
        ref = header.get("messageId", "")
        # ConnectionData (Geo/Ctype) реальный uniproxy присылает всегда; станция
        # может использовать его для политики переподключений.
        await self.send_json(P.sync_state_response(
            ref, self.session_id,
            {"Geo": "sas", "Ctype": "production"} if getattr(self.cfg.server, "sync_connection_data", True) else None,
            keep_message_id=getattr(self.cfg.server, "sync_keep_message_id", True),
        ))
        # System.Push(notify) станция понимает как «состояние изменилось,
        # перечитай»: отвечает System.Ack и уходит на новую синхронизацию, то
        # есть на каждое подключение получается новый круг переподключений.
        # По умолчанию выключено (server.push_notify); состояние станция и так
        # забирает по своим эндпоинтам.
        if getattr(self.cfg.server, "push_notify", False):
            await self.send_json(P.push_notify(ref))

    async def _on_log_spotter(self, header: dict, payload: dict) -> None:
        # станция присылает запись фразы активации (отдельный поток PCM) и ждёт подтверждения
        await self.send_json(P.directive("Log", "Ack", {}, ref=header.get("messageId")))

    async def _on_cancel(self, header: dict, payload: dict) -> None:
        if self._current and not self._current.done():
            self._current.cancel()
        for req in self._voice.values():
            req.finalized = True

    async def _on_echo(self, header: dict, payload: dict) -> None:
        await self.send_json(P.directive("System", "EchoResponse", payload, ref=header.get("messageId")))

    async def _on_ack(self, header: dict, payload: dict) -> None:
        """Станция подтвердила наше событие — значит соединение живое.

        Считаем именно Ack-и на keep-alive Push: по ним видно, что станция нас
        слышит, а не просто молча держит сокет. Первый Ack за сессию пишем в
        INFO — это самая быстрая проверка «станция на связи».
        """
        self._pings_answered += 1
        if self._pings_answered == 1:
            log.info("ACK: станция подтвердила keep-alive — соединение живое (ssid=%s)", self.session_id)
        else:
            log.debug("Ack от станции (всего %d)", self._pings_answered)

    async def _on_client_ping(self, header: dict, payload: dict) -> None:
        """Ping от станции (``TPing``) — её keep-alive либо ответ на наш Ping.

        Отдельного ``TPong`` в протоколе НЕТ: в ``aliceapi.desc`` (прошивка
        3.367.1.28) есть только ``TPing`` — и в ``TServerMessage``, и в
        ``TClientMessage``. Поэтому «понгом» служит Ping с тем же ``Timestamp``,
        и на Ping станции мы отвечаем эхом Ping, а не выдуманным System.Pong.

        Разбор кадра идёт двумя путями: JSON-протокол 1.56 кладёт поля прямо в
        payload (``timestamp``), а AAPI-кадр 3.x приходит обёрнутым в
        ``{"proto": {"Timestamp": ...}}`` (см. ``aliceapi.client_to_json``).
        """
        ts = _ping_timestamp(payload)
        if ts is not None and self._pending_ping_ts is not None and ts == self._pending_ping_ts:
            # станция вернула наш Timestamp — это и есть PONG
            self._pending_ping_ts = None
            self._pings_answered += 1
            self._pongs += 1
            log.info("PONG: станция жива (Timestamp=%d, pong всего %d, ssid=%s)",
                     ts, self._pongs, self.session_id)
            return
        log.info("PING от станции: Timestamp=%s — отвечаю эхом System.Ping с тем же Timestamp", ts)
        if ts is not None and ts in self._ping_echo_done:
            log.debug("keep-alive: эхо на Timestamp=%s уже отправлено, не зацикливаемся", ts)
            return
        if ts is not None:
            self._ping_echo_done.append(ts)
        timeout_ms = int(getattr(self.cfg.server, "ws_ping_timeout_ms", 0) or 0)
        await self.send_json(P.ping(timestamp_s=ts, ping_timeout_ms=timeout_ms, pong_timeout_ms=timeout_ms,
                                    ref=header.get("messageId", "")))

    async def _on_client_pong(self, header: dict, payload: dict) -> None:
        """``System.Pong``.

        В AAPI-схеме прошивок 3.x такого события нет (см. ``_on_client_ping``),
        но обработчик зарегистрирован: он нужен, если станция/прошивка 1.56 всё
        же пришлёт явный Pong, и служит тем самым «PONG: станция жива» в логе.
        """
        self._pending_ping_ts = None
        self._pings_answered += 1
        self._pongs += 1
        log.info("PONG: станция жива (System.Pong, pong всего %d, ssid=%s)", self._pongs, self.session_id)

    # ------------------------------------------------------------ голос

    async def _on_voice_input(self, header: dict, payload: dict) -> None:
        ref = header["messageId"]
        stream_id = int(header.get("streamId", 1))
        hdr = payload.get("header") or {}
        req = VoiceRequest(
            ref=ref,
            stream_id=stream_id,
            request_id=hdr.get("request_id") or str(uuid.uuid4()),
            sequence_number=int(hdr.get("sequence_number") or 0),
            spotter_validation=bool(payload.get("enable_spotter_validation")),
            decoder=OpusStreamDecoder(),
            detector=EndpointDetector(self.cfg.asr),
        )
        # новый запрос отменяет текущий ответ
        if self._current and not self._current.done():
            self._current.cancel()
        for old in self._voice.values():
            old.finalized = True
        self._voice = {stream_id: req}
        await self.send_json(P.input_start_ack(ref))
        self._spawn(self._partials_loop(req))

    async def _partials_loop(self, req: VoiceRequest) -> None:
        """Пустые промежуточные ASR.Result + страховка по времени (как делает uniproxy)."""
        interval = self.cfg.asr.partial_interval_ms / 1000
        hard_limit = self.cfg.asr.max_utterance_ms / 1000 + 3
        while not req.finalized:
            await asyncio.sleep(interval)
            if req.finalized:
                return
            if time.monotonic() - req.started > hard_limit:
                await self._finalize_voice(req, reason="timeout")
                return
            await self.send_json(P.asr_result(req.ref, "", req.partial_no, False, req.detector.duration_ms))
            req.partial_no += 1

    async def _on_stream_control(self, sc: dict) -> None:
        stream_id = sc.get("streamId")
        req = self._voice.get(stream_id)
        if req is None:
            return
        action = sc.get("action")
        if action == STREAM_SPOTTER_END and not req.finalized:
            req.detector.mark_spotter_end()
        elif action == STREAM_CLOSE:
            reason = sc.get("reason")
            if not req.finalized:
                if reason == REASON_CANCEL:
                    # станция отменила запрос (нажали кнопку, перебили) — не отвечаем
                    req.finalized = True
                    log.info("Запрос отменён станцией")
                elif reason == REASON_NO_SPEECH:
                    req.detector.speech_started = False
                    await self._finalize_voice(req, reason="client_no_speech")
                else:
                    # станция сама закончила запись (STOP_RECORDING и т.п.) — распознаём то, что есть
                    await self._finalize_voice(req, reason=f"client_close:{reason}")
            self._voice.pop(stream_id, None)

    async def _finalize_voice(self, req: VoiceRequest, reason: str) -> None:
        if req.finalized:
            return
        req.finalized = True
        log.info("Конец фразы (%s), %.1f c аудио", reason, req.decoder.pcm.size / 16000)
        self._current = self._spawn(self._process_voice(req))

    async def _process_voice(self, req: VoiceRequest) -> None:
        pcm = req.detector.speech_only(req.decoder.pcm)  # VAD: выкидываем тишину
        try:
            text = await self.svc.recognizer.transcribe(pcm) if req.detector.speech_started else ""
        except Exception:
            log.exception("Ошибка ASR")
            text = ""
        text = _strip_wake_word(text)
        log.info("ASR: %r", text)
        await self.send_json(P.asr_result(req.ref, text, req.partial_no, True, req.detector.duration_ms))
        await self.send_json(P.stream_control(req.stream_id))
        if req.spotter_validation:
            await self.send_json(P.spotter_validation(req.ref, self.device_id, True))
        await self._answer(req.ref, req.request_id, req.sequence_number, text)

    # ------------------------------------------------------------ текст

    async def _on_text_input(self, header: dict, payload: dict) -> None:
        ref = header.get("messageId", "")
        request = payload.get("request") or {}
        event = request.get("event") or {}
        hdr = payload.get("header") or {}
        text = event.get("text") or payload.get("text") or ""
        if event.get("type") not in (None, "text_input") or not text:
            log.info("Vins-событие без текста (%s %s) — пустой ответ", event.get("type"), event.get("name"))
            await self._respond_empty(ref, payload)
            return
        if self._current and not self._current.done():
            self._current.cancel()
        self._current = self._spawn(self._answer(ref, hdr.get("request_id") or str(uuid.uuid4()),
                                                 int(hdr.get("sequence_number") or 0), text))

    # ------------------------------------------------------------ музыка

    async def _music_command(self, request_id: str, action: str, query: str) -> tuple[str, list[dict]]:
        """Голосовая музыкальная команда -> (что сказать, директивы)."""
        m = self.svc.music
        if action == "pause":
            return "", [P.client_action("player_pause", {"smooth": True})]
        if action == "resume":
            return "", [P.client_action("player_continue")]
        if not m.enabled:
            return "Музыка не подключена. Запусти на сервере tools/music_login.py.", []
        try:
            if action == "what":
                s = m.sessions.get(self.device_id)
                return (music.say_track(s.current) if s and s.current else "Сейчас ничего не играет."), []
            if action == "like":
                return ("Поставила лайк." if await m.rate(self.device_id, True) else "Сейчас ничего не играет."), []
            if action == "dislike":
                # как у Яндекса: дизлайк и сразу следующий трек
                if await m.rate(self.device_id, False) is None:
                    return "Сейчас ничего не играет.", []
                action = "next"
            if action in ("next", "prev"):
                if self.device_id not in m.sessions:
                    await m.start_wave(self.device_id)
                track = await m.step(self.device_id, 1 if action == "next" else -1, skipped=action == "next")
                speech = ""
            else:
                s = await (m.start_wave(self.device_id) if action == "wave" else m.start_search(self.device_id, query))
                if s is None:
                    return f"Не нашла «{query}» в Яндекс Музыке.", []
                track = await m.step(self.device_id, 1)
                speech = "Включаю «Мою волну»." if action == "wave" else f"Включаю: {music.say_track(track)}"
            if track is None:
                return "Треки закончились.", []
            log.info("Музыка: %s — %s (%s)", track.artists, track.title, track.id)
            return speech, await m.play_directives(self.device_id, request_id, track)
        except Exception:
            log.exception("Ошибка Яндекс Музыки")
            return "Не получилось включить музыку.", []

    async def _youtube_command(self, ref: str, request_id: str, seq: int, action: str) -> None:
        """Пауза/продолжение — директивой Станции, «следующая» — силами сервиса."""
        yt = self.svc.youtube
        # Станция пришлёт эхо-кадр кольца через ~0.1 c — его нельзя считать
        # нажатием, иначе он переключит только что выполненную команду обратно.
        self._mute_ring(f"голос: {action}")
        log.info("YouTube: команда %s", action)
        directives: list[dict] = []
        sentences: list[str] = []
        if action == "next":
            result = await yt.next_track()
            if not result or not result.get("played"):
                # YouTube не смог (или очередь кончилась) — пусть решает музыка.
                speech, directives = await self._music_command(request_id, "next", "")
                await self._speak_sentences(ref, request_id, seq, _aiter([speech] if speech else []),
                                            directives, listen_on_question=False)
                return
            title = " — ".join(x for x in (result.get("artist"), result.get("title")) if x)
            sentences = [f"Включаю: {title}." if title else "Включаю следующий."]
        elif action == "pause":
            # Саму паузу делает директива player_pause, мост только запоминает
            # позицию. Команду в мост отправляем в фоне, чтобы VinsResponse с
            # директивой уходил сразу.
            #
            # Именно _spawn_background, а не self._spawn: Станция закрывает WS
            # через ~1 c после ASR, а задачи сессии отменяются вместе с ним —
            # команда мосту оборвалась бы на полпути.
            _spawn_background(self._youtube_bridge_command("pause"))
            directives = [P.client_action("player_pause", {"smooth": True})]
        elif action == "resume":
            # Продолжение — это переподключение потока (stop + audio_play с
            # offset_ms и новым url): несколько секунд. Ждать его синхронно
            # нельзя — Станция успевает закрыть WS и бросить запрос (лог
            # 20:34:06: POST /api/resume висел 10 с и вернул таймаут).
            _spawn_background(self._youtube_bridge_command("resume", force=True))
            directives = [P.client_action("player_continue")]
        else:
            return
        await self._speak_sentences(ref, request_id, seq, _aiter(sentences), directives,
                                    listen_on_question=False)

    async def _youtube_bridge_command(self, action: str, *, force: bool = False) -> None:
        """Команда мосту в фоне: ответ Станции не ждёт переподключения потока."""
        try:
            if action == "pause":
                await self.svc.youtube.pause()
                log.info("YouTube: мост — пауза (%s)", action)
            else:
                data = await self.svc.youtube.resume(force=force)
                if data is None:
                    log.warning("YouTube: мост не подтвердил продолжение")
                elif not data.get("resumed"):
                    log.warning("YouTube: продолжение не выполнено: %s", data.get("reason"))
                elif data.get("replayed"):
                    log.info("YouTube: мост включил трек заново (поток не переподключился)")
                else:
                    log.info("YouTube: мост переподключил поток с %s мс", data.get("offset_ms"))
        except Exception:  # noqa: BLE001 - голосовой сценарий не должен падать
            log.exception("YouTube: ошибка команды %s", action)

    async def _on_semantic_frame(self, event: dict) -> list[dict]:
        """Кольцо Станции (``@@mm_semantic_frame``) -> пауза/продолжение YouTube.

        Станция на нажатие переключает свой плеер локально и присылает кадр
        ``PlayerContinueSemanticFrame`` (analytics ``player_continue``) — он
        одинаков для паузы и продолжения, поэтому нажатие считаем
        переключателем, а направление берём из нашего же флага паузы в мосте
        (его меняем только мы).

        Команду в мост отправляем в фоне: «продолжение» после паузы — это
        перезапуск потока (yt-dlp + prebuffer, несколько секунд), и если ждать
        его синхронно, станция успевает бросить запрос и переподключить WS.

        Возвращает директивы для VinsResponse (сам ответ отправляет
        ``_on_server_action``).
        """
        yt = self.svc.youtube
        left = self.svc.ring_mute_until.get(self.device_id, 0.0) - time.monotonic()
        if left > 0:
            log.info("Кольцо: кадр через %.1f c после голосовой команды (%s) — это эхо той же "
                     "команды, игнорирую (ещё %.1f c)", RING_MUTE_S - left,
                     self.svc.ring_mute_why.get(self.device_id, ""), left)
            return []
        frame = aliceapi.decode_semantic_frame(event.get("payload_raw") or "")
        action, leaves = _sf_action(event)
        log.info("Кольцо: кадр=%s purpose=%r intent=%r action=%r paused=%r -> действие %r",
                 frame.get("typed"), frame.get("purpose"), leaves.get("intent"),
                 leaves.get("action"), leaves.get("paused"), action)
        if action is None:
            log.info("Кольцо: действие не распознано, полный кадр: %s",
                     json.dumps(event, ensure_ascii=False))
            return []
        st = await yt.playback()
        log.info("Кольцо: мост state=%s playing=%s resumable=%s paused=%s title=%r",
                 st["state"], st["playing"], st["resumable"], st["paused"], st["title"])
        if not st["playing"] and not st["resumable"]:
            log.info("Кольцо: ничего не играет — %s в мост не отправляю", action)
            return []
        if action == "toggle":
            # После локальной паузы кольцом станция держит HTTP-поток открытым
            # (state=streaming, просто не читает), поэтому «играет/не играет» по
            # потоку не отличить. Направление берём из флага паузы моста: его
            # меняем только мы, и он переживает локальные паузы станции.
            action = "resume" if st["paused"] else "pause"
            log.info("Кольцо: переключатель -> %s (paused=%s, state=%s)",
                     action, st["paused"], st["state"])
        if action == "pause":
            _spawn_background(self._youtube_ring_command("pause"))
            # Директиву player_pause НЕ шлём: станция на нажатие кольца уже
            # поставила паузу локально, повторная директива ломает ей состояние
            # (плеер перестаёт реагировать на следующие нажатия).
            return []
        if action == "resume":
            # _spawn_background: продолжение — это переподключение потока, и
            # Станция может закрыть это WS раньше, чем оно закончится.
            _spawn_background(self._youtube_ring_command("resume"))
            # Продолжение делает сам мост (переподключение потока + audio_play).
            return []
        log.info("Кольцо: действие %r пока не поддержано", action)
        return []

    def _mute_ring(self, reason: str) -> None:
        """Заглушить эхо-кадр кольца после голосовой команды плееру.

        Станция присылает ``@@mm_semantic_frame`` не только на нажатие кнопки,
        но и после распознавания «продолжай»/«пауза» (тот же кадр
        ``player_continue``). Команду мы уже выполнили по тексту, поэтому кадр
        в этом окне — дубль, и обрабатывать его как переключатель нельзя.

        Ключ — в общем ``Services``: эхо приходит уже в НОВОМ WebSocket-соединении
        (флаг на объекте сессии до него не доживал), поэтому окно хранится на
        устройство, а не на соединение.
        """
        self.svc.ring_mute_until[self.device_id] = time.monotonic() + RING_MUTE_S
        self.svc.ring_mute_why[self.device_id] = reason

    async def _youtube_ring_command(self, action: str) -> None:
        """Команда мосту в фоне: ответ станции не ждёт перезапуска потока."""
        try:
            if action == "pause":
                await self.svc.youtube.pause()
                log.info("Кольцо: YouTube — пауза")
            else:
                # force=True: кольцо уже сняло локальную паузу Станции, поэтому
                # мост не должен верить своим флагам — он переподключает поток.
                data = await self.svc.youtube.resume(force=True)
                if data is not None and data.get("replayed"):
                    log.info("Кольцо: YouTube — трек включён заново (поток не переподключился)")
                else:
                    log.info("Кольцо: YouTube — продолжение")
        except Exception:  # noqa: BLE001 - кольцо не должно ронять сессию
            log.exception("Кольцо: ошибка команды %s", action)

    async def _on_server_action(self, header: dict, payload: dict, event: dict) -> None:
        ref = header.get("messageId", "")
        hdr = payload.get("header") or {}
        request_id = hdr.get("request_id") or str(uuid.uuid4())
        seq = int(hdr.get("sequence_number") or 0)
        name = event.get("name") or ""
        # ── диагностика: кольцо пока не размечено, смотрим ВСЁ, что приходит ──
        log.info("server_action payload: %s", json.dumps(event.get("payload") or {}, ensure_ascii=False))
        log.info("server_action header: %s", json.dumps(header, ensure_ascii=False))
        log.info("server_action req_event: %s", json.dumps(event, ensure_ascii=False))
        log.info("server_action msg_payload: %s", json.dumps(payload, ensure_ascii=False))
        log.info("server_action от станции: %s", name)
        directives: list[dict] = []
        m = self.svc.music
        try:
            if name == music.CB_PREFIX + "on_failed":
                s = m.sessions.get(self.device_id)
                if s is not None:
                    s.fail_streak += 1
                    log.warning("Станция не смогла проиграть трек %s (подряд: %d)",
                                (event.get("payload") or {}).get("track_id"), s.fail_streak)
                    if s.fail_streak >= 3:  # не «листаем пулемётом» — останавливаемся и говорим
                        m.sessions.pop(self.device_id, None)
                        await self._speak_sentences(ref, request_id, seq,
                                                    _aiter(["Музыка не грузится, станция не может скачать трек."]),
                                                    [], listen_on_question=False)
                        return
            if name == music.CB_PREFIX + "on_started":
                await m.track_started(self.device_id)
            elif name in (music.GET_NEXT, "music_thin_client_recovery", music.CB_PREFIX + "on_failed") and m.enabled:
                if self.device_id in m.sessions:
                    track = await m.step(self.device_id, 1)
                    if track is not None:
                        log.info("Музыка (дальше): %s — %s", track.artists, track.title)
                        directives = await m.play_directives(self.device_id, request_id, track)
            elif name == SF_NAME:
                # верхняя сенсорная кнопка (кольцо) на Станции Лайт 2
                directives = await self._on_semantic_frame(event)
        except Exception:
            log.exception("Ошибка обработки server_action %s", name)
        await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, "", speech="",
                                             directives=directives))

    async def _respond_empty(self, ref: str, payload: dict) -> None:
        hdr = payload.get("header") or {}
        if not hdr:
            # proto-событие (прошивка 3.x): идентификаторы запроса лежат
            # в Header внутри proto, а не в payload.header
            ph = (payload.get("proto") or {}).get("Header") or {}
            hdr = {"request_id": ph.get("RequestId"), "sequence_number": ph.get("SequenceNumber", 0)}
        await self.send_json(P.vins_response(ref, hdr.get("request_id") or str(uuid.uuid4()), self.session_id,
                                             int(hdr.get("sequence_number") or 0), "", speech=""))

    # ------------------------------------------------------------ ответ

    async def _answer(self, ref: str, request_id: str, seq: int, text: str) -> None:
        if not text.strip():
            await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, "", speech=""))
            return

        # ── пасхалка: «десептиконы» -> Автоботы + Linkin Park, без LLM ───
        # Проверяем раньше остальных интентов: фраза не должна уйти в DeepSeek
        # (иначе он начнёт рассуждать про Мегатрона вместо включения песни).
        if intents.match_autobots(text):
            log.info("АВТОБОТЫ: обнаружено слово 'Десептиконы'")
            self._mute_ring("пасхалка: десептиконы")

            async def _autobots_music() -> None:
                # Новый Glagol audio_play обрывает то, что станция играет сейчас,
                # поэтому песню стартуем после фразы: TTS ~1.5 c + ~2.5 c речи.
                # Сам play() идёт ещё несколько секунд (yt-dlp + пребуфер), так
                # что команда станции всё равно приходит после ответа.
                await asyncio.sleep(3.0)
                await self.svc.youtube.play(intents.AUTOBOTS_QUERY)

            _spawn_background(_autobots_music())
            await self._speak_sentences(ref, request_id, seq,
                                        _aiter([intents.AUTOBOTS_SPEECH]), [],
                                        listen_on_question=False)
            return

        intent = intents.match(text)
        if intent is not None:
            if intent.reset_dialog:
                self.svc.dialog.reset(self.device_id)
            await self._speak_sentences(ref, request_id, seq, _aiter([intent.text] if intent.text else []),
                                        intent.directives, listen_on_question=False)
            return
            
        # ── YouTube: короткие команды внешнему плееру ────────────────────
        # Только если поток реально играет ИЛИ есть что продолжить: после
        # паузы Станция может закрыть HTTP-поток, и тогда активной сессии уже
        # нет, хотя «продолжай» обязана обработать именно она.
        youtube_action = intents.match_youtube(text)
        if youtube_action is not None and await self.svc.youtube.active_or_resumable():
            await self._youtube_command(ref, request_id, seq, youtube_action)
            return

        # YouTube-запросы не отдаём встроенной Яндекс.Музыке:
        # их должен обработать LLM и вернуть PLAY_YOUTUBE.
        is_youtube_request = bool(
            re.search(r"(?i)\b(?:youtube|youtu\.be|ютуб\w*|ютьюб\w*)\b", text)
        )
        # Встроенная Яндекс.Музыка не подключена (нет токена/сессии) -> любые
        # «включи/поставь/запусти …» не перехватываем, а отдаём в LLM, чтобы
        # они превратились в PLAY_YOUTUBE и ушли в YouTube-мост.
        music_enabled = self.svc.music.enabled
        music_cmd = None if is_youtube_request else intents.match_music(text, music_enabled=music_enabled)
        if music_cmd is None and not music_enabled and intents.is_play_request(text):
            log.info("Музыка не подключена — запрос уходит в LLM/YouTube: %s", text)
        if music_cmd and music_cmd[0] in ("like", "dislike") and self.device_id not in self.svc.music.sessions:
            music_cmd = None  # «мне нравится» без играющей музыки — обычный разговор
        if music_cmd is not None:
            speech, directives = await self._music_command(request_id, *music_cmd)
            await self._speak_sentences(ref, request_id, seq, _aiter([speech] if speech else []), directives,
                                        listen_on_question=False)
            return

        async def llm_sentences():
            youtube_started = False
            try:
                async for s in self.svc.dialog.stream(self.device_id, text):
                    match = re.search(r"PLAY\s*[-_]?\s*YOUTUBE\s*:\s*(.+)", s, flags=re.IGNORECASE)
                    if match:
                        query = match.group(1).strip().strip("`\"'«» ")
                        if query and not youtube_started:
                            youtube_started = True
                            log.info("YouTube-команда: %s", query)
                            self._mute_ring(f"голос: включи {query}")

                            async def trigger(q: str = query):
                                import httpx

                                try:
                                    async with httpx.AsyncClient(timeout=90.0) as client:
                                        response = await client.post(
                                            "http://127.0.0.1:8099/api/play",
                                            json={"query": q},
                                        )
                                        response.raise_for_status()
                                except Exception:
                                    log.exception("Ошибка запуска YouTube")

                            self._spawn(trigger())

                        # Убираем команду из текста, чтобы TTS её не прочитал.
                        s = (s[: match.start()] + s[match.end() :]).strip()
                        if not s:
                            s = "Включаю."

                    if s:
                        yield s
            except Exception:
                log.exception("Ошибка LLM")
                yield "Извините, мой мозг сейчас недоступен."

        await self._speak_sentences(ref, request_id, seq, llm_sentences(), [],
                                    listen_on_question=self.cfg.llm.listen_after_question)

    async def _speak_sentences(self, ref: str, request_id: str, seq: int, sentences, directives: list[dict],
                               listen_on_question: bool) -> None:
        """Конвейер: фразы (из LLM) -> TTS -> один непрерывный Ogg/Opus поток к станции.

        Vins.VinsResponse уходит, когда готов звук первой фразы; если LLM к этому моменту
        ещё пишет, ждём её не дольше hold_response_ms, чтобы в ответе был полный текст
        и правильный should_listen."""
        queue: asyncio.Queue = asyncio.Queue()
        collected: list[str] = []
        llm_done = asyncio.Event()

        async def producer():
            try:
                async for s in sentences:
                    collected.append(s)
                    await queue.put(s)
            finally:
                llm_done.set()
                await queue.put(None)

        prod = self._spawn(producer())
        enc = None
        sid = 0
        try:
            while (s := await queue.get()) is not None:
                try:
                    pcm, rate = await self.svc.tts.synth_pcm(s)
                except Exception:
                    log.exception("Ошибка TTS")
                    continue
                if not pcm.size:
                    continue
                if enc is None:
                    try:
                        await asyncio.wait_for(llm_done.wait(), self.cfg.llm.hold_response_ms / 1000)
                    except asyncio.TimeoutError:
                        pass
                    text = " ".join(collected)
                    listen = listen_on_question and llm_done.is_set() and text.rstrip().endswith("?")
                    await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, text, speech=text,
                                                         directives=directives, should_listen=listen))
                    sid = self._next_out_stream
                    self._next_out_stream += 2
                    await self.send_json(P.tts_speak(ref, sid))
                    enc = OggOpusStreamEncoder()
                else:
                    await self._send_audio(sid, enc.silence(0.12))
                await self._send_audio(sid, enc.add(pcm, rate))
        finally:
            prod.cancel()
        if enc is None:  # нечего озвучивать (например, команда громкости)
            await self.send_json(P.vins_response(ref, request_id, self.session_id, seq, " ".join(collected),
                                                 speech="", directives=directives))
            return
        await self._send_audio(sid, enc.close())
        await self.send_json(P.stream_control(sid))

    async def _send_audio(self, sid: int, data: bytes) -> None:
        for chunk in split_ogg_pages(data):
            await self.send_bytes(frame_stream(sid, chunk))

    async def _on_tts_generate(self, header: dict, payload: dict) -> None:
        ogg = await self.svc.tts.synth(payload.get("text") or "")
        await self.stream_tts(header.get("messageId", ""), ogg)

    async def stream_tts(self, ref: str, ogg: bytes) -> None:
        sid = self._next_out_stream
        self._next_out_stream += 2
        await self.send_json(P.tts_speak(ref, sid))
        await self._send_audio(sid, ogg)
        await self.send_json(P.stream_control(sid))


async def _aiter(items):
    for x in items:
        yield x


def _collect_leaves(obj, out: dict | None = None) -> dict:
    """Плоские пары «ключ -> значение» из вложенного кадра (последний победил)."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                _collect_leaves(value, out)
            else:
                out[str(key).lower()] = value
    elif isinstance(obj, list):
        for value in obj:
            _collect_leaves(value, out)
    return out


def _sf_action(event: dict) -> tuple[str | None, dict]:
    """Определить действие кольца по semantic frame.

    Основной источник — ``payload_raw`` (см. ``aliceapi.decode_semantic_frame``):
    прошивка кладёт туда ``TypedSemanticFrame`` и ``Analytics.Purpose``.
    Дальше — запасные варианты: intent/action в открытом payload и флаг
    ``paused``.
    """
    leaves = _collect_leaves(event)
    frame = aliceapi.decode_semantic_frame(event.get("payload_raw") or "")
    text = " ".join(
        [str(x) for x in frame.get("typed") or []] + [str(frame.get("purpose") or "")]
    ).lower()
    if text.strip():
        if "pause" in text:
            return "pause", leaves
        if "continue" in text or "replay" in text:
            # кольцо — одна кнопка play/pause, поэтому «continue» = переключатель
            return "toggle", leaves
        if any(k in text for k in ("next", "prev", "like", "dislike", "shuffle", "repeat", "musicplay")):
            # другое действие плеера: toggle тут был бы ошибкой
            return None, leaves
    for key in SF_ACTION_KEYS:
        value = leaves.get(key)
        if not isinstance(value, str):
            continue
        v = value.strip().lower()
        if v in SF_PAUSE:
            return "pause", leaves
        if v in SF_RESUME:
            return "resume", leaves
        if v in SF_TOGGLE:
            return "toggle", leaves
    if "paused" in leaves:
        return "toggle", leaves
    return None, leaves


_WAKE_RE = re.compile(r"^\s*(слушай,?\s+)?(алиса|алис|олиса|малиса|яндекс)[\s,.!?]*", re.I)


def _strip_wake_word(text: str) -> str:
    """Яндекс не включает фразу активации в результат ASR — убираем, если ASR её услышал."""
    rest = _WAKE_RE.sub("", text, count=1)
    return rest[:1].upper() + rest[1:] if rest else ""


def _log_task_error(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("Ошибка в задаче", exc_info=t.exception())