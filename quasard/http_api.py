"""HTTP-эндпоинты Яндекса, к которым ходит станция при старте.

Маршрутизация по пути; хост берётся из заголовка Host или из префикса пути
(/quasar.yandex.net/register — так проксирует nginx из v3rt3p/https-proxy).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .config import Config
from .store import Store

log = logging.getLogger("http")

KNOWN_HOSTS = (
    "quasar.yandex.net", "quasar.yandex.ru", "oauth.yandex.ru", "login.yandex.ru",
    "mobileproxy.passport.yandex.net", "startup.mobile.yandex.net", "report.appmetrica.yandex.net",
    "clck.yandex.net", "uniproxy.alice.yandex.net", "uniproxy-quic.alice.yandex.net", "push.yandex.ru",
    "scbh.yandex.net", "rpc.alice.yandex.net", "analytics.mobile.yandex.net",
    "updater.mobile.yandex.net",  # OTA прошивок 3.x — лучше вообще заблокировать на точке доступа
)


class HostPrefixMiddleware:
    """ASGI: /<host>/path -> /path, запоминает хост в scope['alice_host']."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            host = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0]
            parts = path.split("/", 2)
            if len(parts) > 1 and "." in parts[1] and parts[1].endswith((".net", ".ru", ".com")):
                host = parts[1]
                scope = dict(scope)
                scope["path"] = "/" + (parts[2] if len(parts) > 2 else "")
                scope["raw_path"] = scope["path"].encode()
            scope["alice_host"] = host
        await self.app(scope, receive, send)


def _jwt_hs256(claims: dict, key: bytes) -> str:
    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def build_router(cfg: Config, store: Store) -> APIRouter:
    r = APIRouter()
    key_file = cfg.data_path / "glagol_jwt.key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(secrets.token_bytes(32))
    jwt_key = key_file.read_bytes()

    async def form(request: Request) -> dict[str, str]:
        body = (await request.body()).decode(errors="replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    # ------------------------------------------------------------ проверка интернета (scbh.yandex.net и др.)
    @r.get("/generate_204")
    async def generate_204():
        return Response(status_code=204)

    # ------------------------------------------------------------ oauth.yandex.ru
    @r.post("/token")
    async def oauth_token(request: Request):
        f = await form(request)
        grant = f.get("grant_type")
        device_id = f.get("device_id", "")
        if f.get("device_name"):
            store.update_device(device_id, name=f["device_name"])
        if grant == "authorization_code":
            seed = f.get("code", "") + device_id
            return {
                "access_token": "y0_" + hashlib.sha256(("access" + seed).encode()).hexdigest(),
                "expires_in": 31536000,
                "refresh_token": "1:" + hashlib.sha256(("refresh" + seed).encode()).hexdigest(),
                "token_type": "bearer",
            }
        if grant == "refresh_token":
            token = "y0_" + hashlib.sha256(("access" + f.get("refresh_token", "")).encode()).hexdigest()
        else:  # x-token и любые другие — токен станции нам не важен, клиент его не проверяет
            token = f.get("access_token") or ("y0_" + secrets.token_hex(24))
        log.info("OAuth %s для %s", grant, device_id or "?")
        return {"access_token": token, "expires_in": 31536000, "token_type": "bearer", "uid": cfg.device.uid}

    # ------------------------------------------------------------ startup / метрики
    @r.get("/analytics/startup")
    async def startup(request: Request):
        q = request.query_params
        seed = q.get("deviceid", "") + q.get("api_key_128", "")
        uid = hashlib.md5(seed.encode()).hexdigest()
        hosts = {k: {"urls": [u]} for k, u in {
            "check_updates": "https://analytics.mobile.yandex.net",
            "location": "https://rosenberg.appmetrica.yandex.net",
            "startup": "https://startup.mobile.yandex.net",
            "report": "https://report.appmetrica.yandex.net",
            "diagnostic": "https://report.appmetrica.yandex.net",
        }.items()}
        return {"device_id": {"hash": str(int(uid[:15], 16))}, "query_hosts": {"list": hosts}, "uuid": {"value": uid}}

    @r.post("/report")
    async def appmetrica_report():
        return JSONResponse({"status": "accepted"}, media_type="application/json; encoding=utf-8")

    @r.post("/quasar_metrics/write/json/batch")
    @r.post("/quasar_metrics/write/batch")
    async def clck_batch():
        return [{"ResponseStatus": "OK"}]

    # ------------------------------------------------------------ quasar.yandex.net
    @r.post("/register")
    async def register(request: Request):
        q = request.query_params
        store.update_device(q.get("device_id", ""), platform=q.get("platform"), firmware=q.get("firmware_version"),
                            activation_code=q.get("activation_code"), last_register=int(time.time()))
        return {"hasCriticalUpdate": False, "status": "ok"}

    @r.get("/check_updates")
    async def check_updates():
        return {"hasUpdate": False}

    @r.post("/push_subscribe")
    async def push_subscribe():
        return {"sign": secrets.token_hex(16), "status": "ok", "ts": str(int(time.time()) + 28800)}

    @r.get("/glagol/device_list")
    @r.get("/glagol/family_device_list")
    async def device_list(request: Request):
        return store.device_list(request.query_params.get("device_id", ""))

    # ------------------------------------------------------------ updater.mobile.yandex.net (прошивки 3.x)
    # «Обновлений нет»: пустой набор вместо rom/ab/sys_recovery/apps, и запрет на установку
    @r.post("/tv/api/v4/updates")
    @r.post("/tv/api/v5/updates")
    async def tv_updates():
        return {"apps": [], "rom_update": None, "ab_update": None, "sys_recovery_update": None,
                "on_boot_update": None, "batches": []}

    @r.get("/allow_update")
    async def allow_update():
        return {"status": "ok", "is_allowed": False, "allowed": False}

    @r.get("/get_sync_info")
    async def get_sync_info(request: Request):
        q = request.query_params
        return store.sync_info(q.get("device_id", ""), q.get("platform", "yandexstation_2"))

    @r.get("/get_timezone")
    async def get_timezone():
        return {"timezone": cfg.device.timezone, "status": "ok", "offset_sec": cfg.device.timezone_offset_sec}

    @r.get("/get_user_details")
    async def get_user_details():
        captured = store.captured_json("user_details")
        if captured:
            return captured
        return {"status": "ok", "user_details": {"display_name": {"avatar": {"default": "0/0-0", "empty": True},
                                                                   "name": cfg.device.display_name}}}

    @r.post("/update_device_state")
    async def update_device_state(request: Request):
        q = request.query_params
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        store.update_device(q.get("device_id", ""), platform=q.get("platform"), networkInfo=body)
        return {"status": "ok"}

    @r.get("/glagol/token")
    async def glagol_token(request: Request):
        q = request.query_params
        now = int(time.time())
        token = _jwt_hs256({"plt": q.get("platform", ""), "sub": q.get("device_id", ""), "iss": "quasar-backend",
                            "aud": "glagol", "iat": now, "exp": now + 86400}, jwt_key)
        return {"status": "ok", "token": token}

    def _check_jwt(token: str) -> bool:
        try:
            head, body, sig = token.strip().split(".")
            good = base64.urlsafe_b64encode(hmac.new(jwt_key, f"{head}.{body}".encode(),
                                                     hashlib.sha256).digest()).rstrip(b"=").decode()
            return hmac.compare_digest(good, sig)
        except ValueError:
            return False

    @r.post("/glagol/check_token")
    @r.post("/check_token")
    async def glagol_check_token(request: Request):
        return {"status": "ok", "valid": _check_jwt((await request.body()).decode(errors="replace"))}

    @r.post("/glagol/v2.0/check_token")
    async def glagol_check_token_v2(request: Request):
        ok = _check_jwt((await request.body()).decode(errors="replace"))
        return {"guest": False, "owner": ok, "status": "ok"}

    return r


async def fallback(request: Request) -> Response:
    """Всё неизвестное логируем и отвечаем «ок», чтобы станция не зависала на ретраях."""
    body = await request.body()
    log.warning("Неизвестный запрос: %s %s%s ?%s body=%dB", request.method, request.scope.get("alice_host", ""),
                request.url.path, request.url.query, len(body))
    return JSONResponse({"status": "ok"})
