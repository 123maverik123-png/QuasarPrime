#!/usr/bin/env python3
"""QuasarPrime — запуск всех сервисов проекта одной командой.

Поднимает (в этом порядке):

  1. f5_server      — синтез речи с клоном голоса Оптимуса (127.0.0.1:8777)
  2. quasard-youtube — мост YouTube → Glagol-плеер станции (127.0.0.1:8099)
  3. quasard        — «мозг»: uniproxy для станции, ASR, LLM, TTS (80/443)

Каждый сервис пишет цветной лог с префиксом, всё дублируется в logs/<имя>.log.
Ctrl+C останавливает всё аккуратно: сначала просьба завершиться (CTRL_BREAK на
Windows, SIGTERM на Linux), через --stop-timeout — принудительно, вместе с
дочерними процессами (yt-dlp / ffmpeg / uvicorn workers).

Примеры:
    python launcher.py                 # всё
    python launcher.py --check         # только предварительные проверки
    python launcher.py --no-f5         # без клона голоса (TTS останется без F5)
    python launcher.py --only quasard  # только мозг
    python launcher.py --no-color      # для логов в файл

Настройки (все необязательные) можно положить в config.toml:

    [launcher]
    f5_python = "C:/QuasarPrime/.venv-f5/Scripts/python.exe"  # отдельное окружение для F5
    youtube_python = ""                                       # по умолчанию — текущий
    f5_port = 8777
    f5_nfe = 32
    stop_timeout = 8
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"

# ── цвета ──────────────────────────────────────────────────────────────────────
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
COLORS = {
    "quasard": "\033[96m",   # cyan
    "f5": "\033[95m",        # magenta
    "youtube": "\033[93m",   # yellow
    "launcher": "\033[92m",  # green
    "error": "\033[91m",     # red
}
_USE_COLOR = True


def _enable_ansi() -> None:
    """Включает ANSI-последовательности в conhost/cmd.exe на Windows 10+."""
    if not IS_WIN:
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass


def paint(text: str, who: str = "launcher", *, bold: bool = False) -> str:
    if not _USE_COLOR:
        return text
    return f"{BOLD if bold else ''}{COLORS.get(who, '')}{text}{RESET}"


def say(msg: str, who: str = "launcher", *, bold: bool = False) -> None:
    print(paint(msg, who, bold=bold), flush=True)


def fail(msg: str) -> None:
    print(paint(msg, "error"), file=sys.stderr, flush=True)


# ── конфиг ─────────────────────────────────────────────────────────────────────
@dataclass
class LaunchCfg:
    f5_python: str = ""
    youtube_python: str = ""
    f5_port: int = 8777
    f5_nfe: int = 32
    f5_ref: str = "voice/optimus_24k.wav"
    f5_ref_text: str = "voice/optimus.txt"
    bridge_dir: str = "quasard-youtube"
    wait_timeout: float = 180.0
    stop_timeout: float = 8.0
    log_dir: str = "logs"
    youtube_host: str = "127.0.0.1"
    youtube_port: int = 8099
    quasard_https_port: int = 443
    quasard_http_port: int = 80


@dataclass
class Service:
    name: str
    cmd: list[str]
    cwd: Path
    health: str = ""          # URL, который должен ответить 200, прежде чем идти дальше
    ready_timeout: float = 180.0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log_fh: object | None = field(default=None, repr=False)
    logs: list[str] = field(default_factory=list, repr=False)


def load_launch_cfg(path: Path) -> LaunchCfg:
    cfg = LaunchCfg()
    if not path.exists() or tomllib is None:
        return cfg
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # битый TOML не должен мешать запуску
        fail(f"config.toml не читается ({exc}) — беру настройки лаунчера по умолчанию")
        return cfg
    for key, value in (raw.get("launcher") or {}).items():
        if hasattr(cfg, key):
            setattr(cfg, key, type(getattr(cfg, key))(value))
    server = raw.get("server") or {}
    for key in ("https_port", "http_port"):
        if key in server:
            try:
                setattr(cfg, f"quasard_{key}", int(server[key]))
            except (TypeError, ValueError):
                pass
    return cfg


def _python_for(explicit: str) -> str:
    """Интерпретатор для сервиса: из config.toml [launcher], иначе текущий."""
    if not explicit:
        return sys.executable
    p = Path(explicit)
    if not p.is_absolute():
        p = ROOT / p
    return str(p)


# ── утилиты ────────────────────────────────────────────────────────────────────
def port_busy(port: int, host: str = "") -> bool:
    if port <= 0:
        return False
    fam = socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.bind((host or "0.0.0.0", port))
        except OSError:
            return True
    return False


def wait_url(url: str, timeout: float, stop_evt: threading.Event) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_evt.is_set():
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (локальный адрес)
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def wait_port(port: int, timeout: float, stop_evt: threading.Event, host: str = "127.0.0.1") -> bool:
    if port <= 0:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_evt.is_set():
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()


def _ask_stop(proc: subprocess.Popen) -> None:
    """Мягкая просьба завершиться: даёт uvicorn/fastapi закрыть сокеты."""
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ValueError):
        try:
            proc.terminate()
        except OSError:
            pass


# ── запуск ─────────────────────────────────────────────────────────────────────
def spawn(svc: Service, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    svc.log_fh = open(log_dir / f"{svc.name}.log", "a", encoding="utf-8", errors="replace")
    svc.log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start: "
                     f"{' '.join(shlex.quote(c) for c in svc.cmd)} (cwd={svc.cwd}) =====\n")
    svc.log_fh.flush()

    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    kwargs: dict = {}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    svc.proc = subprocess.Popen(
        svc.cmd, cwd=str(svc.cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
    )
    threading.Thread(target=_pump, args=(svc,), daemon=True).start()


def _pump(svc: Service) -> None:
    assert svc.proc and svc.proc.stdout
    for line in svc.proc.stdout:
        line = line.rstrip("\n")
        print(paint(f"{svc.name:>7} │ ", svc.name) + line, flush=True)
        if svc.log_fh:
            try:
                svc.log_fh.write(line + "\n")
                svc.log_fh.flush()
            except (OSError, ValueError):
                pass


def stop_all(services: list[Service], timeout: float) -> None:
    alive = [s for s in services if s.proc and s.proc.poll() is None]
    if not alive:
        return
    say("останавливаю сервисы…", bold=True)
    for svc in alive:
        _ask_stop(svc.proc)  # type: ignore[arg-type]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(s.proc.poll() is None for s in alive):  # type: ignore[union-attr]
        time.sleep(0.2)

    for svc in alive:
        if svc.proc.poll() is None:  # type: ignore[union-attr]
            say(f"{svc.name}: не завершился за {timeout:.0f} c — снимаю принудительно", "error")
            _kill_tree(svc.proc)  # type: ignore[arg-type]
    for svc in services:
        if svc.log_fh:
            try:
                svc.log_fh.write(f"===== {time.strftime('%H:%M:%S')} stopped =====\n")
                svc.log_fh.close()
            except (OSError, ValueError):
                pass
            svc.log_fh = None


# ── сборка сервисов ────────────────────────────────────────────────────────────
def build_services(args: argparse.Namespace, cfg: LaunchCfg, py: str) -> list[Service]:
    want = set(args.only.split(",")) if args.only else {"f5", "youtube", "quasard"}
    if args.no_f5:
        want.discard("f5")
    if args.no_youtube:
        want.discard("youtube")

    services: list[Service] = []

    if "f5" in want:
        f5_py = _python_for(cfg.f5_python)
        services.append(Service(
            name="f5",
            # cwd = корень проекта: --ref/--ref-text заданы относительно него
            cmd=[f5_py, "f5_server/f5_server.py",
                 "--ref", cfg.f5_ref, "--ref-text", cfg.f5_ref_text,
                 "--port", str(cfg.f5_port), "--nfe", str(cfg.f5_nfe)],
            cwd=ROOT,
            health=f"http://127.0.0.1:{cfg.f5_port}/health",
            ready_timeout=cfg.wait_timeout,
        ))

    if "youtube" in want:
        yt_py = _python_for(cfg.youtube_python)
        bridge = ROOT / cfg.bridge_dir
        services.append(Service(
            name="youtube",
            cmd=[yt_py, "-m", "quasard_youtube"],
            cwd=bridge,
            health=f"http://{cfg.youtube_host}:{cfg.youtube_port}/healthz",
            ready_timeout=45.0,
        ))

    if "quasard" in want:
        services.append(Service(
            name="quasard",
            cmd=[py, "-m", "quasard"],
            cwd=ROOT,
            ready_timeout=15.0,
        ))
    return services


def preflight(args: argparse.Namespace, cfg: LaunchCfg, services: list[Service]) -> list[str]:
    """Возвращает список фатальных проблем (пусто — можно запускать)."""
    problems: list[str] = []

    if sys.version_info < (3, 11):
        problems.append(f"нужен Python 3.11+ (у тебя {sys.version.split()[0]})")
    elif sys.version_info < (3, 12):
        say(f"Python {sys.version.split()[0]}: проект писался под 3.12 — скорее всего заработает, "
            f"но лучше поставить 3.12", "error")

    if not (ROOT / "config.toml").exists():
        problems.append("нет config.toml — скопируй config.example.toml и впиши ключ LLM")
    if tomllib is None:
        problems.append("нет модуля tomllib (нужен Python 3.11+)")

    names = {s.name for s in services}
    if "quasard" in names:
        if not (ROOT / "certs" / "server.crt").exists():
            problems.append("нет certs/server.crt — сгенерируй: python scripts/gen_certs.py --ip <IP_ПК>")
        if not IS_WIN and os.geteuid() != 0:
            problems.append("порты 80/443 требуют root: запусти через sudo")
        for port in (cfg.quasard_http_port, cfg.quasard_https_port):
            if port_busy(port):
                problems.append(f"порт {port} уже занят — останови прошлый quasard / прокси")
    if "f5" in names:
        if not (ROOT / cfg.f5_ref).exists():
            problems.append(f"нет файла голоса {cfg.f5_ref}")
        if not (ROOT / cfg.f5_ref_text).exists():
            problems.append(f"нет текста образца {cfg.f5_ref_text}")
        if port_busy(cfg.f5_port):
            problems.append(f"порт {cfg.f5_port} занят — вероятно, F5 уже запущен (это ок, запусти с --no-f5)")
    if "youtube" in names:
        bridge = ROOT / cfg.bridge_dir
        if not (bridge / "quasard_youtube").exists():
            problems.append(f"нет {bridge}/quasard_youtube")
        if not (bridge / "config.toml").exists():
            problems.append(f"нет {bridge}/config.toml — скопируй {cfg.bridge_dir}/config.example.toml")
        if port_busy(cfg.youtube_port):
            problems.append(f"порт {cfg.youtube_port} занят — мост уже запущен?")
    return problems


def show_plan(services: list[Service], cfg: LaunchCfg) -> None:
    say("", bold=True)
    say("QuasarPrime — Оптимус Прайм на Яндекс Станции", bold=True)
    say(f"каталог: {ROOT}")
    for svc in services:
        argv = " ".join(shlex.quote(c) for c in svc.cmd)
        say(f"  {svc.name:<8} {DIM if _USE_COLOR else ''}{argv}{RESET if _USE_COLOR else ''}")
        say(f"  {'':<8} cwd={svc.cwd}")
    say(f"  логи: {ROOT / cfg.log_dir}")
    say("")


def main() -> int:
    global _USE_COLOR

    ap = argparse.ArgumentParser(
        description="Запуск всех сервисов QuasarPrime с цветными логами.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", default=str(ROOT / "config.toml"), help="путь к config.toml")
    ap.add_argument("--only", default="", help="только эти сервисы: f5,youtube,quasard")
    ap.add_argument("--no-f5", action="store_true", help="не запускать F5-TTS")
    ap.add_argument("--no-youtube", action="store_true", help="не запускать мост YouTube")
    ap.add_argument("--check", action="store_true", help="только проверить окружение и выйти")
    ap.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
    ap.add_argument("--stop-timeout", type=float, default=None, help="сколько ждать остановки, c")
    args = ap.parse_args()

    _USE_COLOR = not args.no_color
    if IS_WIN:
        _enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    cfg = load_launch_cfg(Path(args.config))
    if args.stop_timeout is not None:
        cfg.stop_timeout = args.stop_timeout

    services = build_services(args, cfg, sys.executable)
    if not services:
        fail("нечего запускать: проверь --only/--no-f5/--no-youtube")
        return 2

    problems = preflight(args, cfg, services)
    if problems:
        say("предварительные проверки не прошли:", "error", bold=True)
        for p in problems:
            fail(f"  • {p}")
        if args.check:
            return 1
        say("исправь и запусти снова (или --check, чтобы увидеть только проверки)", "error")
        return 1

    if args.check:
        show_plan(services, cfg)
        say("проверки пройдены — можно запускать", bold=True)
        return 0

    show_plan(services, cfg)

    stop_evt = threading.Event()
    started: list[Service] = []

    def _sigint(_sig, _frm):
        if stop_evt.is_set():
            fail("повторный Ctrl+C — выхожу немедленно")
            for svc in started:
                if svc.proc:
                    _kill_tree(svc.proc)
            os._exit(130)
        stop_evt.set()

    signal.signal(signal.SIGINT, _sigint)
    if not IS_WIN:
        signal.signal(signal.SIGTERM, _sigint)

    log_dir = ROOT / cfg.log_dir
    exit_code = 0
    try:
        for svc in services:
            if stop_evt.is_set():
                break
            say(f"запускаю {svc.name}…", bold=True)
            try:
                spawn(svc, log_dir)
            except FileNotFoundError as exc:
                fail(f"{svc.name}: не найден интерпретатор или файл ({exc})")
                exit_code = 1
                break
            started.append(svc)

            if svc.health:
                say(f"{DIM if _USE_COLOR else ''}{svc.name}: жду готовности {svc.health}…{RESET if _USE_COLOR else ''}")
                if not wait_url(svc.health, svc.ready_timeout, stop_evt):
                    fail(f"{svc.name} не ответил за {svc.ready_timeout:.0f} c — смотри logs/{svc.name}.log")
                    exit_code = 1
                    break
            elif svc.name == "quasard":
                # ждём HTTPS-порт (станция ходит именно туда); 0 — HTTPS выключен
                port = cfg.quasard_https_port or cfg.quasard_http_port
                if not wait_port(port, svc.ready_timeout, stop_evt):
                    fail(f"quasard не открыл {port} — смотри logs/quasard.log")
                    exit_code = 1
                    break
            say(f"{svc.name}: готов", bold=True)

        if exit_code == 0:
            say("", bold=True)
            say("ВСЁ ЗАПУЩЕНО. Скажи станции «Алиса, привет». Ctrl+C — остановить.", bold=True)
            while not stop_evt.is_set():
                time.sleep(0.5)
                for svc in started:
                    if svc.proc and svc.proc.poll() is not None:
                        code = svc.proc.returncode
                        fail(f"{svc.name} завершился сам (код {code}) — останавливаю остальные")
                        exit_code = code or 1
                        stop_evt.set()
                        break
    except KeyboardInterrupt:
        stop_evt.set()
    finally:
        stop_all(started, cfg.stop_timeout)
        say("остановлено.", bold=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
