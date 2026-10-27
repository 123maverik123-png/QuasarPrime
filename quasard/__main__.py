"""Запуск: python -m quasard [--config config.toml]"""
from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from .app import create_app
from .config import load_config

# Значения по умолчанию для WS-keep-alive на транспортном уровне (RFC 6455).
# Раньше здесь стояло ws_ping_interval=None, то есть сервер не слал станции ни
# одного PING-фрейма за всё время жизни соединения. Это единственный слой,
# который станция не может «не поддерживать»: System.Ping из AAPI она объявляет
# неподдерживаемым (SupportedServerEvents без "Ping"), а PING-фрейм — часть
# WebSocket-протокола, а не протокола Алисы.
WS_PING_INTERVAL_DEFAULT = 15.0
WS_PING_TIMEOUT_DEFAULT: float | None = None


def _opt_seconds(value, default: float | None) -> float | None:
    """Секунды из config.toml или из env (там всегда строка).

    None / "none" / "off" / 0 и отрицательные значения -> None (выключено).
    """
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "off", "false", "no", "null"):
            return None
        try:
            value = float(text)
        except ValueError:
            logging.warning("Не понимаю значение %r — беру %s", value, default)
            return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return None if value <= 0 else value


async def _serve(cfg) -> None:
    app = create_app(cfg)
    s = cfg.server
    log = logging.getLogger("uvicorn.error")

    # ws_ping_interval: раз в N секунд сервер шлёт PING-фрейм, станция обязана
    # ответить PONG (её WS-стек делает это сам, без участия кода Алисы).
    # ws_ping_timeout=None — пинговать «вечно», не закрывая соединение, если
    # PONG не пришёл: это безопасный режим (не добавляет новый повод для
    # обрыва). Ставим 20, когда нужен детектор мёртвого линка.
    ping_interval = _opt_seconds(getattr(s, "ws_ping_interval", WS_PING_INTERVAL_DEFAULT), WS_PING_INTERVAL_DEFAULT)
    ping_timeout = _opt_seconds(getattr(s, "ws_ping_timeout", WS_PING_TIMEOUT_DEFAULT), WS_PING_TIMEOUT_DEFAULT)

    mode = str(getattr(s, "ws_keepalive_mode", "push") or "none").strip().lower()
    log.info("WS keep-alive: транспорт ping_interval=%s ping_timeout=%s; протокол Алисы mode=%s interval=%ss",
             "off" if ping_interval is None else f"{ping_interval:g}s",
             "off" if ping_timeout is None else f"{ping_timeout:g}s",
             mode, getattr(s, "ws_keepalive_interval", "-"))
    if ping_interval is None:
        log.warning("WS-пинги выключены (server.ws_ping_interval) — станция останется без транспорта keep-alive")

    servers = []
    cert, key = cfg.resolve(s.certfile), cfg.resolve(s.keyfile)
    common = dict(app=app, host=s.host, log_level=s.log_level.lower(),
                  ws_ping_interval=ping_interval, ws_ping_timeout=ping_timeout,
                  ws_max_size=16 * 1024 * 1024,
                  proxy_headers=False, access_log=s.access_log)
    if s.https_port:
        if cert.exists() and key.exists():
            servers.append(uvicorn.Server(uvicorn.Config(port=s.https_port, ssl_certfile=str(cert),
                                                         ssl_keyfile=str(key), **common)))
        else:
            logging.warning("HTTPS отключён: нет %s / %s (см. tools/gen_certs.py)", cert, key)
    if s.http_port:
        servers.append(uvicorn.Server(uvicorn.Config(port=s.http_port, **common)))
    if not servers:
        raise SystemExit("Не настроен ни один порт")
    await asyncio.gather(*(srv.serve() for srv in servers))


def main() -> None:
    ap = argparse.ArgumentParser(description="Локальный бэкенд голосового ассистента (GigaAM + LLM + TTS)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--ws-ping-interval", default=None,
                    help="переопределить server.ws_ping_interval (сек, none — выключить)")
    ap.add_argument("--ws-ping-timeout", default=None,
                    help="переопределить server.ws_ping_timeout (сек, none — не закрывать при отсутствии PONG)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    # CLI перекрывает и config.toml, и env (удобно для A/B-прогона без правки файлов)
    if args.ws_ping_interval is not None:
        cfg.server.ws_ping_interval = args.ws_ping_interval
    if args.ws_ping_timeout is not None:
        cfg.server.ws_ping_timeout = args.ws_ping_timeout

    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    logging.basicConfig(level=cfg.server.log_level, format=fmt)
    # дублируем лог в файл: удобно смотреть удалённо и после закрытия окна
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(cfg.data_path / "server.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(fh)
    asyncio.run(_serve(cfg))


if __name__ == "__main__":
    main()
