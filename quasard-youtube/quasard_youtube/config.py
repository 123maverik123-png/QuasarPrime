"""Конфигурация quasard-youtube.

TOML читается стандартным tomllib (Python 3.11+). Строки вида ``${ENV}``
подставляются из окружения; это удобно для Session_id, чтобы не хранить
секрет в config.toml.
"""

from __future__ import annotations

import os
import re
import socket
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Некорректная конфигурация."""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _section(raw: dict[str, Any], name: str, cls: type) -> Any:
    data = raw.get(name, {})
    if not isinstance(data, dict):
        raise ConfigError(f"Секция [{name}] должна быть таблицей")
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"Неизвестные ключи в [{name}]: {', '.join(unknown)}. "
            f"Допустимые: {', '.join(sorted(known))}"
        )
    return cls(**data)


@dataclass(slots=True)
class StationConfig:
    host: str = ""
    port: int = 1961
    device_id: str = ""
    platform: str = "yandexstation"
    session_id: str = ""
    device_name: str = ""
    # Локальный quasard умеет и /glagol/token, и /glagol/check_token.
    # Важно брать токен у него же: станция проверяет подпись локальным ключом.
    token_url: str = "http://127.0.0.1:80/glagol/token"
    command_timeout: float = 5.0
    connect_timeout: float = 8.0


@dataclass(slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8099
    advertise_host: str = ""
    prebuffer_bytes: int = 98_304
    prebuffer_timeout: float = 20.0
    station_connect_timeout: float = 15.0
    api_token: str = ""


@dataclass(slots=True)
class AudioConfig:
    bitrate_kbps: int = 192
    sample_rate: int = 44_100
    channels: int = 2
    padding_seconds: float = 2.0
    max_duration_seconds: int = 1800

    @property
    def bytes_per_second(self) -> int:
        return self.bitrate_kbps * 1000 // 8


@dataclass(slots=True)
class YoutubeConfig:
    yt_dlp_path: str = ""
    ffmpeg_path: str = "ffmpeg"
    extra_args: list[str] = field(default_factory=list)
    metadata_timeout: float = 45.0


@dataclass(slots=True)
class PlaybackConfig:
    directive: str = "radio_play"
    stop_before_play: bool = True
    wait_for_ack: bool = False
    # Пауза: false — сервис только запоминает состояние, а саму паузу делает
    # quasard директивой player_pause (поток остаётся открытым, «продолжай»
    # возвращает трек с той же секунды). true — жёсткий stop через Glagol,
    # тогда «продолжай» перезапускает трек с начала.
    pause_command: bool = False
    # Продолжение = полное переподключение потока: stop -> audio_play(offset_ms)
    # с новым url. Сколько секунд ждать, пока Станция откроет HTTP-поток заново,
    # прежде чем считать попытку неудачной и перезапустить трек целиком.
    resume_connect_timeout: float = 4.0
    # Сколько секунд отводим на сами Glagol-команды продолжения (stop+audio_play).
    resume_command_timeout: float = 6.0
    # Сколько раз пробуем переподключить поток, прежде чем включить трек заново.
    resume_attempts: int = 2
    # Если Станция так и не переподключилась — включаем трек заново с начала
    # (лучше слышимый трек с нуля, чем тишина).
    resume_escalate_play: bool = True
    # Шаг громкости для POST /api/volume {"delta": 1} (0.1 = 10%).
    volume_step: float = 0.1
    # Сколько результатов просить у yt-dlp для команды «следующая».
    search_candidates: int = 10


@dataclass(slots=True)
class QuasardConfig:
    bridge_url: str = "http://127.0.0.1:8099"
    empty_reply: str = "Включаю."


@dataclass(slots=True)
class Config:
    station: StationConfig = field(default_factory=StationConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    youtube: YoutubeConfig = field(default_factory=YoutubeConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    quasard: QuasardConfig = field(default_factory=QuasardConfig)
    config_path: Path | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._apply_env()
        self.validate()

    def _apply_env(self) -> None:
        if not self.station.session_id:
            self.station.session_id = os.environ.get("YANDEX_SESSION_ID", "")
        if not self.station.session_id:
            self.station.session_id = os.environ.get("QUASARD_YANDEX_SESSION_ID", "")
        if os.environ.get("QUASARD_YOUTUBE_STATION_HOST"):
            self.station.host = os.environ["QUASARD_YOUTUBE_STATION_HOST"]
        if os.environ.get("QUASARD_YOUTUBE_ADVERTISE_HOST"):
            self.server.advertise_host = os.environ["QUASARD_YOUTUBE_ADVERTISE_HOST"]
        if os.environ.get("QUASARD_YOUTUBE_API_TOKEN"):
            self.server.api_token = os.environ["QUASARD_YOUTUBE_API_TOKEN"]

    def validate(self) -> None:
        if not 1 <= self.station.port <= 65535:
            raise ConfigError("station.port вне диапазона 1..65535")
        if not 1 <= self.server.port <= 65535:
            raise ConfigError("server.port вне диапазона 1..65535")
        if self.audio.bitrate_kbps not in {128, 160, 192, 224, 256, 320}:
            raise ConfigError("audio.bitrate_kbps: поддерживаются 128/160/192/224/256/320")
        if self.audio.sample_rate not in {44_100, 48_000}:
            raise ConfigError("audio.sample_rate: поддерживаются 44100 или 48000")
        if self.audio.channels != 2:
            raise ConfigError("audio.channels: Станция ожидает стерео (2)")
        if self.audio.padding_seconds < 0:
            raise ConfigError("audio.padding_seconds не может быть отрицательным")
        if self.server.prebuffer_bytes < 32_768:
            raise ConfigError("server.prebuffer_bytes: минимум 32768 (1 секунда MP3)")
        if self.playback.directive not in {"radio_play", "audio_play", "auto"}:
            raise ConfigError("playback.directive: radio_play, audio_play или auto")
        if not 0.0 < self.playback.volume_step <= 1.0:
            raise ConfigError("playback.volume_step: шаг громкости в диапазоне 0..1")
        if not 1 <= self.playback.search_candidates <= 25:
            raise ConfigError("playback.search_candidates: 1..25")
        if self.station.port == self.server.port and self.station.host in {
            "127.0.0.1",
            "localhost",
        }:
            raise ConfigError("station.port и server.port не должны совпадать")

    @property
    def session_id(self) -> str:
        value = self.station.session_id.strip()
        if value.lower().startswith("session_id="):
            value = value.split("=", 1)[1]
        return value.strip().strip('"').strip("'")

    def resolve_advertise_host(self) -> str:
        """Определить IP, который видит Станция.

        UDP connect не отправляет пакеты, но заставляет ОС выбрать интерфейс
        маршрута до колонки. Это надёжнее, чем первый попавшийся адрес хоста.
        """
        if self.server.advertise_host:
            return self.server.advertise_host
        host = self.station.host or "192.168.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.0)
                sock.connect((host, self.station.port))
                address = sock.getsockname()[0]
                if address and not address.startswith("0."):
                    return address
        except OSError:
            pass
        try:
            address = socket.gethostbyname(socket.gethostname())
            if address and not address.startswith("127."):
                return address
        except OSError:
            pass
        raise ConfigError(
            "Не удалось определить IP для streamUrl. Укажите server.advertise_host."
        )

    def stream_url(self, token: str) -> str:
        return f"http://{self.resolve_advertise_host()}:{self.server.port}/stream/{token}.mp3"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("config_path", None)
        data["station"]["session_id"] = "***" if self.session_id else ""
        data["server"]["api_token"] = "***" if self.server.api_token else ""
        return data


def load_config(path: str | Path | None = None) -> Config:
    """Загрузить TOML или вернуть defaults.

    Поиск: аргумент -> $QUASARD_YOUTUBE_CONFIG -> ./config.toml.
    """
    candidate: Path | None
    if path:
        candidate = Path(path)
        if not candidate.exists():
            raise ConfigError(f"Файл конфигурации не найден: {candidate}")
    else:
        env_path = os.environ.get("QUASARD_YOUTUBE_CONFIG")
        default = Path("config.toml")
        candidate = Path(env_path) if env_path else (default if default.exists() else None)

    raw: dict[str, Any] = {}
    if candidate:
        try:
            with candidate.open("rb") as handle:
                raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Ошибка TOML в {candidate}: {exc}") from exc
    raw = _expand(raw)

    config = Config(
        station=_section(raw, "station", StationConfig),
        server=_section(raw, "server", ServerConfig),
        audio=_section(raw, "audio", AudioConfig),
        youtube=_section(raw, "youtube", YoutubeConfig),
        playback=_section(raw, "playback", PlaybackConfig),
        quasard=_section(raw, "quasard", QuasardConfig),
        config_path=candidate,
    )
    return config
