from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, WebSocket

from .config import Config
from .http_api import HostPrefixMiddleware, build_router, fallback
from .store import Store
from .uniproxy import Services, UniproxySession

log = logging.getLogger("app")


def create_app(cfg: Config) -> HostPrefixMiddleware:
    store = Store(cfg)
    svc = Services(cfg)
    app = FastAPI(title="Alice backend emulator", docs_url="/_docs", redoc_url=None, openapi_url="/_openapi.json")
    app.state.svc = svc
    app.state.store = store

    @app.on_event("startup")
    async def _warmup() -> None:
        # модели ASR и TTS грузятся в фоне, чтобы HTTP поднялся сразу.
        # startup срабатывает на каждый uvicorn-сервер (80 и 443) — грузим один раз
        if getattr(app.state, "warmed", False):
            return
        app.state.warmed = True
        loop = asyncio.get_running_loop()
        for name, fn in (("ASR", svc.recognizer.load), ("TTS", svc.tts.load)):
            fut = loop.run_in_executor(None, fn)
            fut.add_done_callback(lambda f, n=name: f.exception() and log.error(
                "Не удалось загрузить %s: %r", n, f.exception(), exc_info=f.exception()))

    @app.websocket("/uni.ws")
    async def uni_ws(ws: WebSocket) -> None:
        await ws.accept()
        await UniproxySession(ws, svc).run()

    # --- служебное API для отладки
    @app.get("/_status")
    async def status():
        return {
            "devices": store.known_devices(),
            "connections": {k: len(v) for k, v in svc.connections.items()},
        }

    @app.post("/_say/{device_id}")
    async def say(device_id: str, body: dict):
        """Озвучить текст на станции (через открытое uniproxy-соединение)."""
        conns = svc.connections.get(device_id) or set()
        if not conns:
            return {"status": "error", "error": "станция не подключена"}
        sess = next(iter(conns))
        ogg = await svc.tts.synth(body.get("text", ""))
        await sess.stream_tts("", ogg)
        return {"status": "ok"}

    app.include_router(build_router(cfg, store))
    app.add_api_route("/{path:path}", fallback, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    return HostPrefixMiddleware(app)
