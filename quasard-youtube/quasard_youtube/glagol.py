"""Локальный клиент Glagol (WebSocket, порт 1961) для Яндекс Станции.

Станция отдаёт самоподписанный TLS-сертификат, поэтому проверка сертификата
отключена (как во всех известных реализациях). Токен берётся у
``quasar.yandex.ru`` по cookie ``Session_id``; для этого нужны device_id и
platform колонки.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .config import Config
from .protobuf import build_audio_play, build_radio_play

_LOGGER = logging.getLogger(__name__)

DEVICES_URL = "https://iot.quasar.yandex.ru/m/v3/user/devices"
_USER_AGENT = "quasard-youtube/0.1 (+https://github.com/)"

class GlagolError(RuntimeError):
    """Ошибка подключения/команды Glagol."""


def _iter_device_nodes(value: Any):
    """Рекурсивно обойти ответ IoT API: devices или households→rooms→items."""
    if isinstance(value, dict):
        quasar = value.get("quasar_info")
        if isinstance(quasar, dict) and quasar.get("device_id"):
            yield value
        for child in value.values():
            yield from _iter_device_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_device_nodes(child)


def _device_id(device: dict[str, Any]) -> str:
    quasar = device.get("quasar_info") or {}
    return str(quasar.get("device_id") or "") if isinstance(quasar, dict) else ""


def _platform(device: dict[str, Any]) -> str:
    quasar = device.get("quasar_info") or {}
    return str(quasar.get("platform") or "") if isinstance(quasar, dict) else ""


def _token_ttl(token: str) -> float:
    """Срок жизни JWT (локальный quasard-токен живёт сутки, не 30 секунд)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = float(claims["exp"])
        return max(5.0, min(exp - time.time() - 60.0, 86400.0))
    except (IndexError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 25.0


@dataclass(slots=True)
class GlagolState:
    connected: bool = False
    supported_features: frozenset[str] = field(default_factory=frozenset)
    audio_client: bool = False
    player_type: str = ""
    playing: bool = False
    # Когда Станция последний раз прислала «играет/не играет» и что именно.
    # Это единственный достоверный признак того, что она реально звучит:
    # пауза кольцом не закрывает HTTP-поток, байты просто перестают читаться.
    playing_at: float = 0.0
    player_state: dict[str, Any] = field(default_factory=dict)
    # Позиция внутри отданного Станции потока (секунды) и его длительность:
    # playerState.progress / playerState.duration. Для трека, собранного с
    # середины (перемотка), progress начинается с нуля, а не с start_ms.
    progress_s: float = -1.0
    progress_at: float = 0.0
    duration_s: float = 0.0
    player_title: str = ""
    volume: float = 0.0
    last_message_at: float = 0.0
    last_error: str = ""


class GlagolClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = GlagolState()
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._token: str | None = None
        self._token_deadline = 0.0
        self._device_checked = False
        self._ping_task: asyncio.Task[None] | None = None

    # ── HTTP/token ──────────────────────────────────────────────────────

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            self._http = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT},
            )
        return self._http

    def _cookie_header(self) -> str:
        session_id = self.config.session_id
        if not session_id:
            raise GlagolError(
                "Не задан Yandex Session_id. Укажите station.session_id или "
                "переменную окружения YANDEX_SESSION_ID."
            )
        return f"Session_id={session_id}"

    async def fetch_token(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._token and now < self._token_deadline:
            return self._token
        if not self.config.station.device_id or not self.config.station.platform:
            await self.discover_device()
        elif not self._device_checked and self.config.station.platform == "yandexstation":
            # Старый дефолт: у Lite 2 платформа plum. Пробуем уточнить по API,
            # но не блокируем работу, если список устройств недоступен.
            try:
                await self.discover_device()
            except GlagolError as exc:
                _LOGGER.warning("Не удалось уточнить platform по API: %s", exc)
            self._device_checked = True
        if not self.config.station.device_id:
            raise GlagolError("Не удалось определить station.device_id")

        session = await self._session()
        params = {
            "device_id": self.config.station.device_id,
            "platform": self.config.station.platform,
        }
        try:
            async with session.get(
                self.config.station.token_url,
                params=params,
                headers={"Cookie": self._cookie_header()},
            ) as response:
                if response.status == 401:
                    raise GlagolError(
                        "Yandex Session_id истёк или недействителен (HTTP 401). "
                        "Обновите cookie Session_id."
                    )
                if response.status != 200:
                    text = (await response.text())[:300]
                    raise GlagolError(
                        f"Не удалось получить Glagol-токен: HTTP {response.status} {text}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                    text = (await response.text())[:300]
                    raise GlagolError(
                        "Token endpoint вернул не JSON (вероятно, страницу логина). "
                        f"Проверьте Session_id. Ответ: {text}"
                    ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GlagolError(
                "Ошибка запроса Glagol-токена. Проверьте, что quasard запущен и "
                f"{self.config.station.token_url} отвечает: {exc}"
            ) from exc

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise GlagolError(f"В ответе token endpoint нет token: {payload!r}")
        self._token = str(token)
        self._token_deadline = now + _token_ttl(self._token)
        return self._token

    async def discover_device(self) -> None:
        """Заполнить device_id/platform/host через Yandex IoT API."""
        if not self.config.session_id:
            raise GlagolError("Для автоопределения устройства нужен Session_id")
        session = await self._session()
        try:
            async with session.get(
                DEVICES_URL,
                headers={"Cookie": self._cookie_header()},
            ) as response:
                if response.status == 401:
                    raise GlagolError("Session_id недействителен (HTTP 401)")
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise GlagolError(f"Не удалось получить список устройств: {exc}") from exc

        devices = [item for item in _iter_device_nodes(payload) if _device_id(item)]
        if not devices:
            raise GlagolError("Yandex IoT API не вернул ни одной Станции")

        configured_host = self.config.station.host.strip()
        configured_name = self.config.station.device_name.strip().casefold()
        configured_id = self.config.station.device_id.strip()
        candidates: list[dict[str, Any]] = []
        for device in devices:
            host = str(device.get("host") or device.get("ip") or "").strip()
            name = str(device.get("name") or "").strip()
            if configured_id and _device_id(device) == configured_id:
                candidates.insert(0, device)
            elif configured_host and host == configured_host:
                candidates.insert(0, device)
            elif configured_name and name.casefold() == configured_name:
                candidates.append(device)

        if not candidates and len(devices) == 1:
            candidates = [devices[0]]
        if not candidates and not configured_id and not configured_host and not configured_name:
            raise GlagolError(
                "Устройство не выбрано: укажите station.device_id или station.device_name"
            )
        if not candidates:
            names = ", ".join(
                f"{device.get('name')} @ {device.get('host') or device.get('ip') or '?'} "
                f"({_device_id(device)}, {_platform(device)})"
                for device in devices
            )
            raise GlagolError(
                "Станция не найдена в списке устройств. Укажите device_id вручную. "
                f"Доступные: {names}"
            )

        device = candidates[0]
        quasar = device.get("quasar_info") or {}
        self.config.station.device_id = str(quasar.get("device_id") or "")
        self.config.station.platform = str(
            quasar.get("platform") or self.config.station.platform
        )
        host = str(device.get("host") or device.get("ip") or "").strip()
        if not self.config.station.host and host:
            self.config.station.host = host
        _LOGGER.info(
            "Glagol: выбрано устройство %r (%s, %s)",
            device.get("name"),
            self.config.station.device_id,
            self.config.station.platform,
        )

    # ── WebSocket ───────────────────────────────────────────────────────

    async def _check_private_host(self) -> None:
        host = self.config.station.host
        if not host:
            raise GlagolError("Не задан station.host")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, self.config.station.port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise GlagolError(f"Не удалось разрешить адрес Станции {host}: {exc}") from exc
        addresses = {item[4][0] for item in infos}
        if not addresses:
            raise GlagolError(f"Адрес Станции {host} не разрешился")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not (ip.is_private or ip.is_link_local or ip.is_loopback):
                raise GlagolError(
                    f"Отказ подключаться к публичному адресу {address}: "
                    "токен Glagol нельзя отправлять за пределы локальной сети"
                )

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._ws is not None and not self._ws.closed:
                return
            if not self.config.station.host:
                await self.discover_device()
            await self._check_private_host()
            token = await self.fetch_token()
            url = f"wss://{self.config.station.host}:{self.config.station.port}"
            session = await self._session()
            _LOGGER.info("Glagol: подключаюсь к %s", url)
            try:
                self._ws = await session.ws_connect(
                    url,
                    ssl=False,
                    heartbeat=None,
                    max_msg_size=4 * 1024 * 1024,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.state.connected = False
                self.state.last_error = str(exc)
                raise GlagolError(f"WebSocket к Станции не открылся: {exc}") from exc
            # Первый токен уже получен; connect() не отправляет команду.
            _ = token
            self.state.connected = True
            self.state.last_error = ""
            self._reader_task = asyncio.create_task(
                self._reader(self._ws), name="glagol-reader"
            )
            self._ping_task = asyncio.create_task(
                self._ping_loop(), name="glagol-ping"
            )

    async def _reader(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    self._handle_message(data)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.warning(
                        "Glagol: Станция закрыла WebSocket (code=%s, reason=%r)",
                        ws.close_code,
                        message.extra,
                    )
                    break
                elif message.type == aiohttp.WSMsgType.ERROR:
                    self.state.last_error = str(ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - сеть может упасть как угодно
            self.state.last_error = repr(exc)
            _LOGGER.warning("Glagol reader завершился с ошибкой: %r", exc)
        finally:
            self.state.connected = False
            if self._ws is ws:
                self._ws = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(GlagolError("Соединение Glagol закрыто"))
            self._pending.clear()
            if self._ping_task is not None:
                self._ping_task.cancel()
                self._ping_task = None

    async def _ping_loop(self) -> None:
        """Станция закрывает WSS через ~10 с без app-level ping.

        WebSocket PING/PONG прошивка не считает достаточным: нужен JSON
        {"payload":{"command":"ping"}} примерно раз в 5 секунд.
        """
        try:
            await asyncio.sleep(3)
            while self._ws is not None and not self._ws.closed:
                token = await self.fetch_token()
                await self._ws.send_json(
                    {
                        "conversationToken": token,
                        "id": uuid.uuid4().hex,
                        "sentTime": round(time.time() * 1000),
                        "payload": {"command": "ping"},
                    }
                )
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = repr(exc)
            _LOGGER.warning("Glagol: ping остановлен: %r", exc)

    def _apply_state(self, state: dict[str, Any]) -> None:
        """Разобрать состояние проигрывателя, которое присылает Станция.

        ``playing`` — главный признак: Станция присылает состояние при каждом
        изменении воспроизведения, в том числе когда паузу нажали кольцом или
        кнопкой, а не мы. Через HTTP-поток такую паузу не видно: соединение
        остаётся открытым, байты просто перестают забираться.

        ``playerState.progress`` — позиция внутри отданного потока (секунды),
        точнее любых наших оценок по часам. После паузы Станция продолжает
        присылать ``progress`` на замершем значении, так что по нему же
        считается и позиция продолжения.
        """
        player = state.get("playerState")
        if isinstance(player, dict):
            self.state.player_state = dict(player)
            self.state.player_type = str(player.get("playerType") or "")
            self.state.player_title = str(player.get("title") or "")
            with contextlib.suppress(TypeError, ValueError):
                self.state.duration_s = float(player.get("duration") or 0.0)
            progress = player.get("progress")
            if progress is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self.state.progress_s = float(progress)
                    self.state.progress_at = time.time()
        if "volume" in state:
            with contextlib.suppress(TypeError, ValueError):
                self.state.volume = float(state["volume"])
        playing = state.get("playing")
        if playing is None and isinstance(player, dict):
            playing = player.get("playing")
        if playing is None:
            return
        playing = bool(playing)
        if playing != self.state.playing:
            self.state.playing = playing
            self.state.playing_at = time.time()
            _LOGGER.info(
                "Glagol: Станция %s (позиция %.1f c)",
                "играет" if playing else "не играет",
                self.state.progress_s,
            )
        elif not self.state.playing_at:
            self.state.playing_at = time.time()

    def _handle_message(self, data: dict[str, Any]) -> None:
        self.state.last_message_at = time.time()
        features = data.get("supported_features")
        if isinstance(features, list):
            self.state.supported_features = frozenset(str(item) for item in features)
            self.state.audio_client = "audio_client" in self.state.supported_features

        state = data.get("state")
        if isinstance(state, dict):
            self._apply_state(state)

        request_id = data.get("requestId")
        if request_id and request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                result = {
                    "status": data.get("status", "unknown"),
                }
                if "error" in data:
                    result["error"] = data["error"]
                if "message" in data:
                    result["message"] = data["message"]
                future.set_result(result)

    # ── Команды ─────────────────────────────────────────────────────────

    async def _send_json(self, payload: dict[str, Any], request_id: str, token: str) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise GlagolError("WebSocket не подключён")
        await ws.send_json(
            {
                "conversationToken": token,
                "id": request_id,
                "sentTime": round(time.time() * 1000),
                "payload": payload,
            }
        )

    async def send(
        self,
        payload: dict[str, Any],
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = timeout or self.config.station.command_timeout
        async with self._send_lock:
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    await self.connect()
                    token = await self.fetch_token()
                    request_id = uuid.uuid4().hex
                    future: asyncio.Future[dict[str, Any]] | None = None
                    if wait:
                        future = asyncio.get_running_loop().create_future()
                        self._pending[request_id] = future
                    try:
                        await self._send_json(payload, request_id, token)
                        if not wait or future is None:
                            return {"status": "SENT"}
                        try:
                            result = await asyncio.wait_for(future, timeout)
                        except asyncio.TimeoutError:
                            return {"status": "NO_RESPONSE"}
                        if result.get("error"):
                            raise GlagolError(f"Станция вернула ошибку: {result['error']}")
                        if result.get("status") not in {None, "SUCCESS", "unknown"}:
                            if result.get("status") != "NO_RESPONSE":
                                _LOGGER.warning("Glagol: ответ %s", result)
                        return result
                    finally:
                        if future is not None:
                            self._pending.pop(request_id, None)
                except (GlagolError, aiohttp.ClientError, OSError) as exc:
                    last_error = exc
                    self.state.connected = False
                    if self._ws is not None:
                        with contextlib.suppress(Exception):
                            await self._ws.close()
                        self._ws = None
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                        continue
                    raise GlagolError(f"Не удалось отправить команду Glagol: {exc}") from exc
            raise GlagolError(f"Не удалось отправить команду Glagol: {last_error}")

    async def play(
        self,
        url: str,
        title: str = "",
        subtitle: str = "",
        *,
        directive: str | None = None,
        offset_ms: int = 0,
    ) -> dict[str, Any]:
        selected = directive or self.config.playback.directive
        if selected == "auto":
            selected = "audio_play" if self.state.audio_client else "radio_play"
        if selected == "audio_play":
            payload = build_audio_play(url, title, subtitle, offset_ms=offset_ms)
        elif selected == "radio_play":
            payload = build_radio_play(url, title, subtitle)
        else:
            raise GlagolError(f"Неизвестная директива: {selected}")
        _LOGGER.info("Glagol: %s -> %s", selected, url)
        return await self.send(
            payload,
            wait=self.config.playback.wait_for_ack,
            timeout=self.config.station.command_timeout,
        )

    async def stop(self) -> dict[str, Any]:
        _LOGGER.info("Glagol: stop")
        return await self.send({"command": "stop"}, wait=False)

    # ── транспорт (пауза/продолжение/переключение) ──────────────────────
    #
    # В Glagol нет отдельной команды player_pause: у Станции есть play,
    # stop, next, prev, rewind, setVolume (так же их использует
    # ma-provider-yandex-station: для audio_client-сессии пауза = stop).
    # «Мягкую» паузу без потери позиции умеет только директива Станции
    # player_pause, а её может отправить лишь quasard — она едет в
    # VinsResponse. Поэтому по умолчанию /api/pause команду не шлёт.

    async def pause(self) -> dict[str, Any]:
        """Жёсткая пауза: Станция останавливает поток и закрывает HTTP-соединение."""
        _LOGGER.info("Glagol: stop (жёсткая пауза)")
        return await self.send({"command": "stop"}, wait=False)

    async def resume(self) -> dict[str, Any]:
        _LOGGER.info("Glagol: play (продолжение)")
        return await self.send({"command": "play"}, wait=False)

    async def next_track(self) -> dict[str, Any]:
        """Переключить встроенную очередь Станции (для нашего потока не нужен)."""
        _LOGGER.info("Glagol: next")
        return await self.send({"command": "next"}, wait=False)

    async def set_volume(self, volume: float) -> dict[str, Any]:
        if not 0.0 <= volume <= 1.0:
            raise ValueError("volume должен быть в диапазоне 0..1")
        return await self.send(
            {"command": "setVolume", "volume": round(volume, 2)}, wait=False
        )

    async def volume_delta(self, delta: float) -> dict[str, Any]:
        """Относительная громкость: нужен известный текущий уровень (state.volume)."""
        if not self.state.last_message_at:
            raise GlagolError(
                "Станция ещё не присылала состояние — текущая громкость неизвестна. "
                "Подождите пару секунд после подключения."
            )
        base = self.state.volume
        target = min(1.0, max(0.0, base + delta))
        _LOGGER.info("Glagol: volume %.2f -> %.2f", base, target)
        await self.set_volume(target)
        return {"previous": round(base, 2), "volume": round(target, 2)}

    async def close(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ping_task
            self._ping_task = None
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None
        self.state.connected = False

    def endpoint(self) -> str:
        return f"wss://{self.config.station.host}:{self.config.station.port}"


def sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
