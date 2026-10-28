"""Поиск на YouTube и получение метаданных через yt-dlp."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .cookies import CookieFileInfo, inspect_cookies, js_solver_status, prepare_working_jar

_LOGGER = logging.getLogger(__name__)
_URL_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class ResolveError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrackInfo:
    video_id: str
    title: str
    artist: str
    duration: float
    webpage_url: str
    thumbnail: str = ""
    query: str = ""

    @property
    def display_title(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


def looks_like_url(value: str) -> bool:
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        return False
    host = (urlparse(value).hostname or "").lower()
    return host in _URL_HOSTS


class YoutubeResolver:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._command = self._find_ytdlp()
        self._cookies_source: Path | None = None
        self._cookie_note = ""
        self._cookies = self._prepare_cookies()

    def _prepare_cookies(self, *, announce: bool = True) -> Path | None:
        """Рабочая копия файла куки для ``--cookies`` (``None`` — не передавать).

        Отсутствие файла — норма: yt-dlp работает анонимно, как до появления
        этой настройки. Битый файл тоже не должен ронять воспроизведение,
        поэтому он просто игнорируется с предупреждением в логе.

        yt-dlp перезаписывает ``--cookies`` при каждом запуске, поэтому ему
        достаётся копия: исходный экспорт пользователя не меняется.

        ``announce=False`` — повторная проверка перед запуском yt-dlp: файл
        могли положить уже после старта сервиса, и тогда он подхватится без
        перезапуска (сообщения в лог не повторяются).
        """
        configured = self.config.youtube.cookies_file.strip()
        if not configured:
            return None
        if _extra_args_use_cookies(self.config.youtube.extra_args):
            # Пользователь уже передал --cookies/--cookies-from-browser вручную.
            if announce:
                _LOGGER.info(
                    "Куки: youtube.extra_args уже содержит флаг куки — "
                    "youtube.cookies_file не применяется"
                )
            return None
        path = self.config.resolve_cookies_file()
        if path is None:
            if announce:
                _LOGGER.info(
                    "Куки: файл %r не найден — yt-dlp работает без авторизации",
                    configured,
                )
            return None
        info = inspect_cookies(path)
        if not info.valid:
            if announce or info.detail != self._cookie_note:
                _LOGGER.warning("Куки: %s игнорируется — %s", path, info.detail)
            self._cookie_note = info.detail
            return None
        _LOGGER.info(
            "Куки: %s (записей %d, youtube.com %d, %s)",
            path,
            info.entries,
            info.youtube_entries,
            "авторизация есть" if info.authorized else "авторизации нет",
        )
        if not info.authorized:
            _LOGGER.warning(
                "Куки: в файле нет SID/LOGIN_INFO — похоже, YouTube был не "
                "залогинен при экспорте; ограничения YouTube это не снимет"
            )
        solver_ready, solver_detail = js_solver_status(self.config.youtube.extra_args)
        if solver_ready:
            _LOGGER.info("Куки: JS-солвер готов (%s)", solver_detail)
        else:
            # С куки YouTube отдаёт клиенту web, а он требует подписи форматов.
            # Без солвера видео приходит без аудио — хуже, чем без авторизации.
            _LOGGER.warning(
                "Куки: %s. С авторизацией yt-dlp должен решать JS-задачу "
                "YouTube, иначе форматов не будет («Аудиопоток пуст»). "
                "См. docs/COOKIES.md, раздел про yt-dlp-ejs",
                solver_detail,
            )
        self._cookies_source = path
        return prepare_working_jar(path)

    @property
    def cookies_file(self) -> Path | None:
        """Файл, который реально попадёт в ``--cookies`` (рабочая копия)."""
        return self._cookies

    @property
    def cookies_source(self) -> Path | None:
        """Исходный экспорт пользователя (для ``doctor`` и логов)."""
        return self._cookies_source

    def cookies_info(self) -> CookieFileInfo | None:
        """Отчёт о файле куки для диагностики (``doctor``)."""
        path = self._cookies_source or self.config.resolve_cookies_file()
        if path is None:
            return None
        return inspect_cookies(path)

    def _find_ytdlp(self) -> list[str]:
        configured = self.config.youtube.yt_dlp_path.strip()
        if configured:
            return [configured]
        found = shutil.which("yt-dlp")
        if found:
            return [found]
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        raise ResolveError(
            "yt-dlp не найден. Установите: pip install yt-dlp "
            "(или укажите youtube.yt_dlp_path в config.toml)"
        )

    @property
    def command(self) -> list[str]:
        return list(self._command)

    def _base_args(self) -> list[str]:
        if self._cookies is None and self.config.youtube.cookies_file.strip():
            # Файл могли положить уже после старта сервиса: подхватываем без
            # перезапуска (повторный лог не пишем).
            self._cookies = self._prepare_cookies(announce=False)
        args = [
            *self._command,
            "--no-warnings",
            "--no-progress",
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            *self.config.youtube.extra_args,
        ]
        if self._cookies is not None:
            # Один и тот же файл нужен и поиску/метаданным, и стриму:
            # длинные треки без авторизации YouTube часто отдаёт неохотно.
            args.extend(["--cookies", str(self._cookies)])
        return args

    async def resolve(self, query: str, *, skip: frozenset[str] = frozenset()) -> TrackInfo:
        """Найти трек. ``skip`` — video_id, которые уже играли (для «следующей»)."""
        query = query.strip()
        if not query:
            raise ResolveError("Пустой поисковый запрос")
        is_url = looks_like_url(query)
        candidates = 1 if is_url else max(1, min(self.config.playback.search_candidates, len(skip) + 1))
        url = query if is_url else f"ytsearch{candidates}:{query}"
        args = [
            *self._base_args(),
            "--dump-single-json",
            "--skip-download",
        ]
        if is_url:
            args.append("--no-playlist")
        else:
            # Для поиска не нужно извлекать 40+ форматов видео: flat-playlist
            # отдаёт id/title/duration в разы быстрее и компактнее.
            args.extend(["--flat-playlist", "--playlist-items", f"1-{candidates}"])
        args.append(url)

        _LOGGER.info("YouTube: ищу %r", query)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_subprocess_flags(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self.config.youtube.metadata_timeout
            )
        except asyncio.TimeoutError as exc:
            await _terminate(process)
            raise ResolveError("yt-dlp не успел получить метаданные") from exc

        if process.returncode != 0:
            detail = _tail(stderr)
            raise ResolveError(f"yt-dlp завершился с кодом {process.returncode}: {detail}")
        try:
            raw = json.loads(stdout.decode("utf-8-sig", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ResolveError(f"Не удалось разобрать JSON yt-dlp: {_tail(stdout)}") from exc

        if skip:
            info = pick_fresh(raw, query, skip)
        else:
            entry = _first_entry(raw)
            if entry is None:
                raise ResolveError("YouTube не вернул ни одного результата")
            info = track_from_entry(entry, query)
        _LOGGER.info(
            "YouTube: %s (%s, %.1f c) за %.2f c",
            info.display_title,
            info.video_id,
            info.duration,
            time.monotonic() - started,
        )
        return info

    def stream_args(self, info: TrackInfo) -> list[str]:
        """Аргументы yt-dlp, который пишет выбранный audio-поток в stdout."""
        return [
            *self._base_args(),
            "--quiet",
            "--no-playlist",
            "-f",
            "bestaudio/best",
            "-o",
            "-",
            info.webpage_url,
        ]


def track_from_entry(entry: dict[str, Any], query: str = "") -> TrackInfo:
    """Преобразовать entry из yt-dlp в TrackInfo, проверив пригодность."""
    live_status = entry.get("live_status")
    if entry.get("is_live") or live_status in {"is_live", "is_upcoming"}:
        raise ResolveError("Прямые трансляции не поддерживаются: нужен конечный MP3")

    duration_raw = entry.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise ResolveError(
            "YouTube не сообщил длительность; нельзя рассчитать Content-Length"
        ) from exc
    if duration <= 0:
        raise ResolveError("Некорректная длительность видео")

    video_id = str(entry.get("id") or "")
    webpage_url = str(
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")
        or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    )
    if not video_id or not webpage_url.startswith("http"):
        raise ResolveError("yt-dlp вернул неполные данные видео")

    artist = str(
        entry.get("artist")
        or entry.get("creator")
        or entry.get("uploader")
        or entry.get("channel")
        or ""
    ).strip()
    return TrackInfo(
        video_id=video_id,
        title=str(entry.get("title") or "YouTube").strip(),
        artist=artist,
        duration=duration,
        webpage_url=webpage_url,
        thumbnail=str(entry.get("thumbnail") or ""),
        query=query,
    )


def _first_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("_type") in {"playlist", "multi_video"}:
        entries = raw.get("entries") or []
        for entry in entries:
            if isinstance(entry, dict):
                return entry
        return None
    if raw.get("_type") == "url":
        return raw
    return raw


def _tail(data: bytes, limit: int = 1200) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return text[-limit:] if text else "(пусто)"


def _entries(raw: Any) -> list[dict[str, Any]]:
    """Все результаты поиска (playlist → entries), без ломания на одиночном видео."""
    if not isinstance(raw, dict):
        return []
    if raw.get("_type") in {"playlist", "multi_video"}:
        return [entry for entry in (raw.get("entries") or []) if isinstance(entry, dict)]
    return [raw]


def pick_fresh(raw: Any, query: str, skip: frozenset[str]) -> TrackInfo:
    """Первый результат поиска, которого ещё не было в текущей сессии.

    Нужен для команды «следующая»: yt-dlp отдаёт один и тот же первый
    результат на один и тот же запрос, поэтому просим несколько и берём
    следующий неизвестный video_id.
    """
    entries = _entries(raw)
    rejected = 0
    for entry in entries:
        video_id = str(entry.get("id") or "")
        if not video_id or video_id in skip:
            continue
        try:
            return track_from_entry(entry, query)
        except ResolveError as exc:  # live/без длительности — просто дальше
            rejected += 1
            _LOGGER.warning("YouTube: пропускаю %s — %s", video_id, exc)
    raise ResolveError(
        "Больше нет подходящих результатов по запросу "
        f"(получено {len(entries)}, отклонено {rejected})"
    )


def _extra_args_use_cookies(extra_args: list[str]) -> bool:
    """Уже передан ли флаг куки вручную через ``youtube.extra_args``."""
    for arg in extra_args:
        if arg in {"--cookies", "--cookies-from-browser"}:
            return True
        if arg.startswith(("--cookies=", "--cookies-from-browser=")):
            return True
    return False


def _subprocess_flags() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
