"""CLI quasard-youtube."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys

from . import __version__
from .audio import ensure_ffmpeg
from .command import parse_command
from .config import ConfigError, load_config
from .cookies import inspect_cookies, js_solver_status
from .glagol import GlagolClient, GlagolError
from .service import PlaybackError, PlaybackService
from .youtube import ResolveError, YoutubeResolver

_LOGGER = logging.getLogger("quasard_youtube")


def _setup_logging(verbose: int, log_file: str | None) -> None:
    # На Windows перенаправленный stdout иначе уходит в cp1251, и русский
    # текст в JSON/логах превращается в мусор.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    level = logging.WARNING if verbose <= 0 else logging.INFO if verbose == 1 else logging.DEBUG
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _load(args: argparse.Namespace) -> Config:
    return load_config(args.config)


async def _cmd_serve(args: argparse.Namespace) -> int:
    config = _load(args)
    try:
        service = PlaybackService(config)
        await service.start()
    except (ResolveError, RuntimeError, ConfigError, OSError) as exc:
        print(f"Не удалось запустить сервис: {exc}", file=sys.stderr)
        return 2
    print(f"quasard-youtube {__version__} слушает {config.server.host}:{config.server.port}")
    print(f"API:  http://127.0.0.1:{config.server.port}/api/status")
    print(f"Для Станции: http://{config.resolve_advertise_host()}:{config.server.port}/")
    print("Ctrl+C — остановить.")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: request_stop())
            except (ValueError, OSError):
                pass
    try:
        await stop_event.wait()
    finally:
        print("Останавливаюсь...")
        await service.close()
    return 0


async def _cmd_play(args: argparse.Namespace) -> int:
    config = _load(args)
    service: PlaybackService | None = None
    try:
        service = PlaybackService(config)
        await service.start()
        result = await service.play(args.query)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        session = service.hub.current
        if session is not None:
            timeout = float(result.get("duration", 60)) + 120
            try:
                await asyncio.wait_for(session.finished.wait(), timeout)
            except asyncio.TimeoutError:
                print("Поток не завершился вовремя.", file=sys.stderr)
    except (PlaybackError, GlagolError, ResolveError, RuntimeError, ConfigError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    finally:
        if service is not None:
            await service.close()
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    problems = 0

    def ok(label: str, detail: str = "") -> None:
        print(f"[OK]   {label}" + (f": {detail}" if detail else ""))

    def bad(label: str, detail: str) -> None:
        nonlocal problems
        problems += 1
        print(f"[FAIL] {label}: {detail}")

    def warn(label: str, detail: str) -> None:
        # Не увеличивает счётчик: без этого файла сервис штатно работает.
        print(f"[WARN] {label}: {detail}")

    print(f"quasard-youtube {__version__}")
    print(f"config: {config.config_path or '(defaults)'}")
    try:
        ensure_ffmpeg(config)
        version = await _run_version([config.youtube.ffmpeg_path, "-version"])
        ok("ffmpeg", version)
    except Exception as exc:  # noqa: BLE001
        bad("ffmpeg", str(exc))

    try:
        resolver = YoutubeResolver(config)
        version = await _run_version([*resolver.command, "--version"])
        ok("yt-dlp", version)
    except Exception as exc:  # noqa: BLE001
        resolver = None
        bad("yt-dlp", str(exc))

    # Куки: без файла всё работает, поэтому отсутствие — WARN, а вот
    # явно указанный, но непригодный файл — настоящая ошибка конфига.
    configured_cookies = config.youtube.cookies_file.strip()
    cookies_path = config.resolve_cookies_file()
    if not configured_cookies:
        print("[--]   cookies: youtube.cookies_file пуст — работаем анонимно")
    elif cookies_path is None:
        warn(
            "cookies",
            f"{configured_cookies} не найден — работаем анонимно, как раньше",
        )
    else:
        report = inspect_cookies(cookies_path)
        if not report.valid:
            bad("cookies", f"{cookies_path}: {report.detail}")
        else:
            auth = (
                "авторизация есть"
                if report.authorized
                else "НЕТ SID/LOGIN_INFO — YouTube не залогинен"
            )
            ok(
                "cookies",
                f"{cookies_path} (записей {report.entries}, "
                f"youtube.com {report.youtube_entries}, {auth})",
            )
            # С авторизацией yt-dlp идёт в клиент, требующий JS-подписи.
            solver_ready, solver_detail = js_solver_status(config.youtube.extra_args)
            if solver_ready:
                ok("JS-солвер", solver_detail)
            else:
                bad(
                    "JS-солвер",
                    f"{solver_detail}: с куки YouTube вернёт видео без аудио "
                    "(см. docs/COOKIES.md)",
                )

    try:
        advertise = config.resolve_advertise_host()
        ok("advertise host", advertise)
        if advertise.startswith("127."):
            bad("advertise host", "локальный адрес не будет доступен Станции")
    except Exception as exc:  # noqa: BLE001
        bad("advertise host", str(exc))

    if not config.station.host:
        bad("station.host", "не задан")
    else:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.station.host, config.station.port), 4
            )
            writer.close()
            await writer.wait_closed()
            del reader
            ok("Glagol TCP", f"{config.station.host}:{config.station.port}")
        except Exception as exc:  # noqa: BLE001
            bad("Glagol TCP", f"{config.station.host}:{config.station.port}: {exc}")

    if not config.session_id:
        bad("Session_id", "не задан (YANDEX_SESSION_ID)")
    else:
        client = GlagolClient(config)
        try:
            if not config.station.device_id:
                await client.discover_device()
                ok("device", config.station.device_id)
            token = await client.fetch_token()
            ok("Glagol token", f"получен ({len(token)} символов)")
            await client.connect()
            ok("Glagol WSS", client.endpoint())
            await asyncio.sleep(0.5)
            if client.state.audio_client:
                ok("audio_client", "поддерживается (audio_play)")
            else:
                print(
                    "[INFO] audio_client не обнаружен до первой команды; "
                    "для прошивки 0.369 всё равно используйте directive=audio_play"
                )
        except GlagolError as exc:
            bad("Glagol", str(exc))
        finally:
            await client.close()

    if args.query and resolver is not None:
        try:
            info = await resolver.resolve(args.query)
            ok("YouTube", f"{info.display_title} ({info.duration:.0f} c)")
        except Exception as exc:  # noqa: BLE001
            bad("YouTube", str(exc))

    if problems:
        print(f"\nПроблем: {problems}")
        return 1
    print("\nВсё готово.")
    return 0


async def _run_version(command: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("таймаут") from None
    if process.returncode != 0:
        raise RuntimeError(stdout.decode("utf-8", errors="replace")[:300])
    return stdout.decode("utf-8", errors="replace").splitlines()[0].strip()


def _cmd_parse(args: argparse.Namespace) -> int:
    parsed = parse_command(args.text)
    print(
        json.dumps(
            {
                "matched": parsed.matched,
                "query": parsed.query,
                "spoken_text": parsed.spoken_text,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if parsed.matched else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quasard-youtube",
        description="YouTube -> локальный HTTP MP3 -> Glagol radio_play",
    )
    parser.add_argument("--config", "-c", help="путь к config.toml")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--log-file", help="писать лог в файл")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="запустить HTTP/API-сервер")
    serve.set_defaults(func=_cmd_serve)

    play = sub.add_parser("play", help="найти трек и включить на Станции")
    play.add_argument("query", help="название, исполнитель или URL YouTube")
    play.set_defaults(func=_cmd_play)

    doctor = sub.add_parser("doctor", help="проверить окружение, станцию и токен")
    doctor.add_argument("--query", help="дополнительно проверить поиск YouTube")
    doctor.set_defaults(func=_cmd_doctor)

    parse = sub.add_parser("parse", help="проверить разбор ответа LLM")
    parse.add_argument("text")
    parse.set_defaults(func=_cmd_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.log_file)
    if args.command == "parse":
        return _cmd_parse(args)
    try:
        _load(args)
        return int(asyncio.run(args.func(args)))
    except ConfigError as exc:
        print(f"Конфигурация: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
