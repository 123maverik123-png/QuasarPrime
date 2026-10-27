"""Управление внешним YouTube-плеером (сервис quasard-youtube, 127.0.0.1:8099).

* «Следующая» — целиком HTTP: сервис останавливает текущий поток и запускает
  следующий результат поиска по тому же запросу.
* «Пауза» / «продолжай» — директивой Станции (``player_pause`` /
  ``player_continue``), потому что директива едет внутри VinsResponse, а его
  формирует только quasard. Сервису отправляем запрос «только для состояния»,
  чтобы он знал про паузу (и мог перезапустить трек, если Станция закрыла
  поток).

Модуль намеренно не бросает исключений наружу: голосовой сценарий не должен
падать из-за недоступного моста.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import YoutubeCfg

log = logging.getLogger("youtube")


class YoutubeControl:
    def __init__(self, cfg: YoutubeCfg) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s, connect=2.0))
        self._active = False
        self._active_until = 0.0
        self._paused = False

    # ── HTTP ────────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return self.cfg.bridge_url.rstrip("/") + path

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Token": self.cfg.api_token} if self.cfg.api_token else {}

    async def _get(self, path: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(self._url(path), headers=self._headers())
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("quasard-youtube GET %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    async def _post(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                self._url(path),
                json=payload or {},
                headers=self._headers(),
                timeout=timeout if timeout is not None else self.cfg.timeout_s,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("quasard-youtube POST %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    # ── состояние ───────────────────────────────────────────────────────

    async def active(self) -> bool:
        """Играет ли сейчас поток YouTube (кэш на ``status_ttl_s`` секунд)."""
        if not self.cfg.enabled:
            return False
        now = time.monotonic()
        if now < self._active_until:
            return self._active
        data = await self._get("/api/status") or {}
        stream = data.get("stream") if isinstance(data.get("stream"), dict) else {}
        state = str(stream.get("state") or "")
        # «На паузе» станция обычно держит поток открытым (state=streaming), но
        # может и закрыть его — тогда сессия живёт в paused/resume_offset, и
        # «продолжай» всё равно надо отдать мосту.
        alive = state in {"streaming", "waiting_station", "paused"} or bool(stream.get("paused"))
        self._active = bool(alive or data.get("paused"))
        self._active_until = now + max(0.0, self.cfg.status_ttl_s)
        return self._active

    def _invalidate(self) -> None:
        """Сбросить кэш ``active()``: состояние только что изменили мы сами."""
        self._active_until = 0.0

    # ── команды ─────────────────────────────────────────────────────────

    @property
    def paused(self) -> bool:
        """Последнее известное состояние паузы (по нашим же командам)."""
        return self._paused

    async def status(self) -> dict[str, Any]:
        """Сырой статус моста: ``stream.state``, ``paused``, громкость и т.п."""
        return await self._get("/api/status") or {}

    async def is_paused(self) -> bool:
        """Пауза по данным моста; если мост не ответил — по последней команде."""
        data = await self.status()
        if "paused" in data:
            return bool(data.get("paused"))
        return self._paused

    async def playback(self) -> dict[str, Any]:
        """Состояние плеера одним запросом (для кнопки play/pause и кольца).

        ``playing`` — Станция сейчас тянет поток; ``resumable`` — есть трек,
        который можно продолжить, даже если поток уже закрыт (пауза кольцом,
        локальная пауза Станции, неудачное переподключение: state становится
        ``finished``/``waiting_station``, а трек при этом продолжается с
        ``offset_ms``).
        """
        data = await self.status()
        stream = data.get("stream") or {}
        state = str(stream.get("state") or "")
        playing = state in {"streaming", "waiting_station"}
        resumable = bool(
            stream.get("resumable")  # мост сам знает, есть ли что продолжать
            or data.get("paused")
            or playing
            or (state == "finished" and (stream.get("title") or (data.get("queue") or {}).get("query")))
        )
        return {
            "playing": playing,
            "resumable": resumable,
            "paused": bool(data.get("paused")),
            "state": state,
            "title": stream.get("title") or "",
        }

    async def pause(self) -> dict[str, Any] | None:
        data = await self._post("/api/pause")
        self._paused = True
        self._invalidate()
        return data

    async def resume(self, *, force: bool = False) -> dict[str, Any] | None:
        """Снять с паузы.

        ``force=True`` — не верить флагам состояния моста и заставить его
        переподключить поток (``stop`` + ``audio_play`` с ``offset_ms``).
        Так «продолжай» перестаёт быть пустым действием: раньше при любом
        расхождении флагов мост уходил в ветку с Glagol ``{"command":"play"}``,
        которая с audio_client-сессией ничего не делает (лог 20:33:49).

        Мост отвечает не сразу (переподключение + ожидание запроса потока),
        поэтому таймаут здесь больше обычного.
        """
        data = await self._post(
            "/api/resume",
            {"send_command": True, "force": force},
            timeout=self.cfg.resume_timeout_s,
        )
        if data is not None and data.get("resumed"):
            self._paused = False
        self._invalidate()
        return data

    async def resumable(self) -> bool:
        """Есть ли трек, который можно продолжить (даже если поток отвалился)."""
        if not self.cfg.enabled:
            return False
        state = await self.playback()
        return bool(state.get("resumable"))

    async def active_or_resumable(self) -> bool:
        """Гейт для «пауза»/«продолжай»/«дальше»: играет ИЛИ есть что продолжить."""
        return await self.active() or await self.resumable()

    async def next_track(self) -> dict[str, Any] | None:
        """Поиск + prebuffer + audio_play: может занять десятки секунд."""
        data = await self._post("/api/next", timeout=self.cfg.next_timeout_s)
        if data and data.get("played"):
            self._paused = False
        self._invalidate()
        return data

    async def play(self, query: str) -> dict[str, Any] | None:
        data = await self._post("/api/play", {"query": query}, timeout=self.cfg.next_timeout_s)
        if data and data.get("ok"):
            self._paused = False
        self._invalidate()
        return data

    async def volume(self, delta: int) -> dict[str, Any] | None:
        return await self._post("/api/volume", {"delta": int(delta)})

    async def close(self) -> None:
        await self._client.aclose()
