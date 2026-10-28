"""Аудиоконвейер yt-dlp -> ffmpeg -> MP3 (CBR) -> StreamBuffer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from .config import Config
from .streaming import BufferClosed, StreamBuffer
from .youtube import TrackInfo, YoutubeResolver

_LOGGER = logging.getLogger(__name__)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_CHUNK = 32 * 1024


def ffmpeg_args(config: Config, start_seconds: float = 0.0) -> list[str]:
    audio = config.audio
    args = [
        config.youtube.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
    ]
    if start_seconds and start_seconds > 0:
        # Перемотка: -ss перед -i. Вход — pipe, поэтому ffmpeg не ищет в файле,
        # а пропускает пакеты до нужного времени, не декодируя их: это дешевле
        # и точнее, чем -ss после -i (там пришлось бы декодировать всё начало).
        args += ["-ss", f"{float(start_seconds):.3f}"]
    args += [
        "-i",
        "pipe:0",
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0",
        "-ac",
        str(audio.channels),
        "-ar",
        str(audio.sample_rate),
    ]
    # Громкость музыки: -af до кодера, чтобы уровень не зависел от YouTube.
    gain_filter = audio.filter.strip()
    if gain_filter:
        args += ["-af", gain_filter]
    args += [
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{audio.bitrate_kbps}k",
        "-minrate",
        f"{audio.bitrate_kbps}k",
        "-maxrate",
        f"{audio.bitrate_kbps}k",
        "-bufsize",
        f"{audio.bitrate_kbps * 2}k",
        "-write_xing",
        "0",
        "-map_metadata",
        "-1",
        "-f",
        "mp3",
        "pipe:1",
    ]
    return args


def _proc_kwargs() -> dict[str, int]:
    if _CREATE_NO_WINDOW:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
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


async def _stderr_tail(stream: asyncio.StreamReader | None, limit: int = 4000) -> str:
    if stream is None:
        return ""
    data = bytearray()
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                del data[: len(data) - limit]
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return data.decode("utf-8", errors="replace").strip()


async def generate_silence(config: Config, seconds: float = 5.0) -> bytes:
    """Сгенерировать валидную MP3-тишину для добивки Content-Length."""
    args = [
        config.youtube.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={config.audio.sample_rate}:cl=stereo",
        "-t",
        str(seconds),
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{config.audio.bitrate_kbps}k",
        "-write_xing",
        "0",
        "-f",
        "mp3",
        "pipe:1",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **_proc_kwargs(),
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        except asyncio.TimeoutError:
            await _terminate(process)
            return b"\x00" * 65_536
        if process.returncode == 0 and stdout:
            return stdout
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Не удалось сгенерировать MP3-тишину: %r", exc)
    return b"\x00" * 65_536


async def produce_mp3(
    config: Config,
    resolver: YoutubeResolver,
    track: TrackInfo,
    buffer: StreamBuffer,
    content_length: int,
    *,
    start_seconds: float = 0.0,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Запустить yt-dlp+ffmpeg и писать MP3 в буфер, пока не наберётся length.

    ``start_seconds`` — с какой секунды трека начинать (перемотка): ffmpeg
    пропускает начало потока, поэтому буфер сразу содержит нужное место.

    Возвращает число записанных байт. Ошибка поднимается только если не удалось
    получить ни одного байта; частичный поток добивается тишиной на HTTP-слое.
    """
    stop_event = stop_event or asyncio.Event()
    ytdlp: asyncio.subprocess.Process | None = None
    ffmpeg: asyncio.subprocess.Process | None = None
    yt_err_task: asyncio.Task[str] | None = None
    ff_err_task: asyncio.Task[str] | None = None
    pump_task: asyncio.Task[None] | None = None
    written = 0
    error: BaseException | None = None

    try:
        ytdlp = await asyncio.create_subprocess_exec(
            *resolver.stream_args(track),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_proc_kwargs(),
        )
        ffmpeg = await asyncio.create_subprocess_exec(
            *ffmpeg_args(config, start_seconds),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_proc_kwargs(),
        )
        yt_err_task = asyncio.create_task(_stderr_tail(ytdlp.stderr), name="yt-dlp-stderr")
        ff_err_task = asyncio.create_task(_stderr_tail(ffmpeg.stderr), name="ffmpeg-stderr")
        pump_task = asyncio.create_task(
            _pump(ytdlp.stdout, ffmpeg.stdin), name="yt-dlp-to-ffmpeg"
        )
        _LOGGER.info("Аудио: yt-dlp PID %s -> ffmpeg PID %s", ytdlp.pid, ffmpeg.pid)

        assert ffmpeg.stdout is not None
        while written < content_length and not stop_event.is_set():
            chunk = await ffmpeg.stdout.read(_CHUNK)
            if not chunk:
                break
            if written + len(chunk) > content_length:
                chunk = chunk[: content_length - written]
            await buffer.write(chunk)
            written += len(chunk)

        if written >= content_length:
            _LOGGER.info("Аудио: достигнут Content-Length (%d байт), останавливаю кодировщик", written)
            await _terminate(ytdlp)
            await _terminate(ffmpeg)
        else:
            await _terminate(ytdlp)
            with contextlib.suppress(Exception):
                await ffmpeg.stdin.wait_closed()
            await asyncio.wait_for(ffmpeg.wait(), 10)

        if pump_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pump_task, 5)

        yt_rc = ytdlp.returncode
        ff_rc = ffmpeg.returncode
        if written == 0:
            detail_parts = []
            if yt_rc not in (0, None):
                detail_parts.append(f"yt-dlp rc={yt_rc}")
            if ff_rc not in (0, None):
                detail_parts.append(f"ffmpeg rc={ff_rc}")
            detail = ", ".join(detail_parts) or "неизвестная ошибка"
            raise RuntimeError(f"Аудиопоток пуст ({detail})")
        if yt_rc not in (0, None) and written < content_length:
            _LOGGER.warning("Аудио: yt-dlp завершился с кодом %s, получено %d байт", yt_rc, written)
    except asyncio.CancelledError:
        error = BufferClosed("воспроизведение отменено")
        raise
    except BaseException as exc:
        error = exc
        raise
    finally:
        for task in (pump_task,):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await _terminate(ytdlp)
        await _terminate(ffmpeg)
        stderr_parts: list[str] = []
        for task in (yt_err_task, ff_err_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    text = await asyncio.wait_for(task, 2)
                    if text:
                        stderr_parts.append(text)
        if stderr_parts:
            _LOGGER.debug("Аудио stderr: %s", " | ".join(stderr_parts))
        if error is None:
            await buffer.finish()
        else:
            await buffer.fail(error if isinstance(error, BaseException) else RuntimeError(str(error)))
        _LOGGER.info("Аудио: записано %d байт в буфер", written)


async def _pump(
    source: asyncio.StreamReader | None, target: asyncio.StreamWriter | None
) -> None:
    if source is None or target is None:
        return
    try:
        while True:
            chunk = await source.read(64 * 1024)
            if not chunk:
                break
            target.write(chunk)
            await target.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(Exception):
            target.close()
        with contextlib.suppress(Exception):
            await target.wait_closed()


def ensure_ffmpeg(config: Config) -> None:
    path = config.youtube.ffmpeg_path
    import shutil
    from pathlib import Path

    found = Path(path).is_file() or bool(shutil.which(path))
    if not found:
        raise RuntimeError(
            f"ffmpeg не найден: {path}. Установите ffmpeg и добавьте его в PATH "
            "или укажите youtube.ffmpeg_path."
        )


__all__ = [
    "ffmpeg_args",
    "generate_silence",
    "produce_mp3",
    "ensure_ffmpeg",
]
