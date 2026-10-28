"""Проверка файла куки, который yt-dlp принимает через ``--cookies``.

yt-dlp читает куки через ``http.cookiejar.MozillaCookieJar``, а тот падает с
``LoadError`` на любом файле без заголовка Netscape. Поэтому файл сначала
проверяется здесь: битый экспорт (JSON вместо Netscape, пустой файл, куки
чужого браузера не в том формате) не должен ломать воспроизведение — лучше
тихо работать без куки и сказать об этом в лог.

Второй нюанс: при выходе yt-dlp **перезаписывает** файл из ``--cookies``
содержимым своей куки-банки. Если сессия недействительна, сервер присылает
``Set-Cookie`` с удалением — и ``SID``/``LOGIN_INFO`` из пользовательского
экспорта исчезают навсегда. Поэтому в yt-dlp отдаётся рабочая копия
(``prepare_working_jar``), а исходный ``cookies.txt`` остаётся неприкосновенным.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# MozillaCookieJar считает файл валидным только с одной из этих строк.
_HEADERS = ("# netscape http cookie file", "# http cookie file")
# Строки с этим префиксом — тоже данные: так помечаются HttpOnly-куки.
_HTTP_ONLY = "#httponly_"
# Куки, которые появляются только в авторизованной сессии YouTube.
_AUTH_COOKIES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "LOGIN_INFO",
    }
)
_FIELDS = 7  # domain, flag, path, secure, expiry, name, value
# yt-dlp включает без флага только deno; остальные — через --js-runtimes.
_DEFAULT_JS_RUNTIMES = ("deno",)


def _explicit_js_runtimes(extra_args: list[str]) -> list[str]:
    """Значения ``--js-runtimes`` из extra_args (``node``, ``node:C:\\...``)."""
    specs: list[str] = []
    for index, arg in enumerate(extra_args):
        value = ""
        if arg == "--js-runtimes" and index + 1 < len(extra_args):
            value = extra_args[index + 1]
        elif arg.startswith("--js-runtimes="):
            value = arg.split("=", 1)[1]
        for part in value.split(","):
            part = part.strip()
            if part:
                specs.append(part)
    return specs


def js_solver_status(extra_args: list[str]) -> tuple[bool, str]:
    """Сможет ли yt-dlp решить JS-задачу YouTube (подпись ``nsig``/``n``).

    Когда yt-dlp получает куки, он идёт в клиент ``web``, а тот требует подписи
    форматов. Без пакета ``yt-dlp-ejs`` и включённого JS-рантайма подпись не
    решается (``Signature solving failed``), и видео приходит вообще без
    аудио-форматов — то есть с куки работает хуже, чем без них. Поэтому такая
    конфигурация проверяется и попадает в лог/``doctor``.
    """
    has_ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
    explicit = _explicit_js_runtimes(extra_args)
    candidates = explicit or list(_DEFAULT_JS_RUNTIMES)
    found: list[str] = []
    for spec in candidates:
        name, _, path = spec.partition(":")
        name = name.strip().lower()
        if path:
            if Path(path.strip().strip('"')).is_file():
                found.append(f"{name}: {path.strip()}")
        else:
            where = shutil.which(name)
            if where:
                found.append(f"{name}: {where}")
    if not has_ejs:
        return False, "нет пакета yt-dlp-ejs (нужен: pip install yt-dlp-ejs)"
    if not found:
        if explicit:
            return False, f"JS-рантайм из extra_args не найден: {', '.join(explicit)}"
        node = shutil.which("node")
        hint = (
            f'; node найден ({node}) — включите его: '
            'extra_args = ["--js-runtimes", "node"]'
            if node
            else ""
        )
        return False, "нет JS-рантайма (deno в PATH)" + hint
    return True, ", ".join(found)


@dataclass(frozen=True, slots=True)
class CookieFileInfo:
    """Результат разбора cookies.txt."""

    path: Path
    exists: bool = False
    valid: bool = False
    entries: int = 0
    youtube_entries: int = 0
    authorized: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.exists and self.valid


def inspect_cookies(path: Path) -> CookieFileInfo:
    """Разобрать Netscape-файл куки и оценить его пригодность для yt-dlp."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return CookieFileInfo(path=path, detail="файл не найден")
    except OSError as exc:
        return CookieFileInfo(path=path, detail=f"не читается: {exc}")

    header = False
    entries = 0
    youtube_entries = 0
    authorized = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(_HEADERS):
            header = True
            continue
        if lowered.startswith(_HTTP_ONLY):
            line = line[len(_HTTP_ONLY) :]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) != _FIELDS:
            continue
        entries += 1
        domain, name = parts[0].lower(), parts[5]
        if "youtube.com" in domain:
            youtube_entries += 1
        if name in _AUTH_COOKIES:
            authorized = True

    if entries == 0:
        return CookieFileInfo(
            path=path,
            exists=True,
            entries=0,
            detail="нет ни одной куки — файл пустой или это не Netscape-формат",
        )
    if not header:
        return CookieFileInfo(
            path=path,
            exists=True,
            entries=entries,
            youtube_entries=youtube_entries,
            authorized=authorized,
            detail=(
                "нет строки '# Netscape HTTP Cookie File' — yt-dlp такой файл "
                "не загрузит; переэкспортируйте куки расширением"
            ),
        )
    if youtube_entries == 0:
        return CookieFileInfo(
            path=path,
            exists=True,
            entries=entries,
            authorized=authorized,
            detail="в файле нет куки домена youtube.com — экспорт сделан не с YouTube",
        )
    return CookieFileInfo(
        path=path,
        exists=True,
        valid=True,
        entries=entries,
        youtube_entries=youtube_entries,
        authorized=authorized,
        detail="ок",
    )


def working_jar_path(source: Path) -> Path:
    """Имя рабочей копии рядом с экспортом: ``cookies.txt`` -> ``cookies.work.txt``."""
    return source.with_name(f"{source.stem}.work{source.suffix}")


def prepare_working_jar(source: Path) -> Path:
    """Актуальная рабочая копия куки для yt-dlp.

    Копия обновляется, когда исходный файл изменился (переэкспорт подхватывается
    без перезапуска сервиса). При ошибке копирования возвращается исходный
    путь: пусть yt-dlp попробует сам, это лучше, чем молча работать без куки.
    """
    target = working_jar_path(source)
    try:
        src_stat = source.stat()
    except OSError as exc:
        _LOGGER.warning("Куки: исходный файл %s недоступен: %s", source, exc)
        return source
    try:
        dst_stat = target.stat()
    except OSError:
        dst_stat = None
    if (
        dst_stat is not None
        and dst_stat.st_size == src_stat.st_size
        and dst_stat.st_mtime_ns >= src_stat.st_mtime_ns
    ):
        return target
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        _LOGGER.warning(
            "Куки: не удалось создать рабочую копию %s (%s); "
            "передаю yt-dlp исходный файл",
            target,
            exc,
        )
        return source
    _LOGGER.info("Куки: рабочая копия обновлена — %s", target)
    return target


__all__ = [
    "CookieFileInfo",
    "inspect_cookies",
    "js_solver_status",
    "prepare_working_jar",
    "working_jar_path",
]
