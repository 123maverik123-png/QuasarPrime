"""Минимальный protobuf-кодек для Glagol ``externalCommandBypass``.

Сообщение — два length-delimited поля:
  1: имя команды (``radio_play`` / ``audio_play``)
  2: JSON-строка с параметрами

Формат проверен по alice-spotify-bridge (bridge/src/glagol.rs) и
ma-provider-yandex-station (provider/protobuf.py).
"""

from __future__ import annotations

import base64
import json
from typing import Any

__all__ = [
    "encode_field",
    "encode_message",
    "build_external_command",
    "build_radio_play",
    "build_audio_play",
    "decode_message",
]


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint не может быть отрицательным")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_field(tag: int, value: str | bytes) -> bytes:
    """Length-delimited поле (wire type 2)."""
    if tag <= 0:
        raise ValueError("tag должен быть > 0")
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return _varint((tag << 3) | 2) + _varint(len(raw)) + raw


def encode_message(fields: dict[int, str | bytes] | list[tuple[int, str | bytes]]) -> bytes:
    entries = fields.items() if isinstance(fields, dict) else fields
    return b"".join(encode_field(tag, value) for tag, value in entries)


def build_external_command(name: str, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """Собрать payload для Glagol-команды ``externalCommandBypass``."""
    data: dict[int, str] = {1: name}
    if payload is not None:
        data[2] = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
    wire = encode_message(data)
    return {
        "command": "externalCommandBypass",
        "data": base64.b64encode(wire).decode("ascii"),
    }


def build_radio_play(url: str, title: str = "", subtitle: str = "") -> dict[str, Any]:
    """Классическая команда ``radio_play`` (как в alice-spotify-bridge)."""
    payload: dict[str, Any] = {
        "streamUrl": url,
        "force_restart_player": True,
    }
    if title:
        payload["title"] = title
    if subtitle:
        payload["subtitle"] = subtitle
    return build_external_command("radio_play", payload)


def build_audio_play(
    url: str,
    title: str = "",
    subtitle: str = "",
    *,
    fmt: str = "MP3",
    media_type: str = "Track",
    offset_ms: int = 0,
) -> dict[str, Any]:
    """Современная команда ``audio_play`` для прошивок с audio_client.

    Прошивка проверяет формат ДО загрузки URL: допустимы "MP3" и "HLS".
    WAV/FLAC/AAC она затем определяет по содержимому потока.
    ``offset_ms`` — с какой миллисекунды начинать (0 для обычного старта).
    """
    payload: dict[str, Any] = {
        "stream": {
            "url": url,
            "format": fmt,
            "type": media_type,
            "offset_ms": max(0, int(offset_ms)),
        },
        "set_pause": False,
    }
    metadata: dict[str, str] = {}
    if title:
        metadata["title"] = title
    if subtitle:
        metadata["subtitle"] = subtitle
    if metadata:
        payload["metadata"] = metadata
    return build_external_command("audio_play", payload)


def decode_message(wire: bytes) -> dict[int, Any]:
    """Простой декодер для тестов и диагностики (только wire type 2)."""

    def read_varint(pos: int) -> tuple[int, int]:
        result = 0
        shift = 0
        while True:
            if pos >= len(wire):
                raise ValueError("обрыв varint")
            byte = wire[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result, pos
            shift += 7
            if shift > 63:
                raise ValueError("слишком длинный varint")

    result: dict[int, Any] = {}
    pos = 0
    while pos < len(wire):
        key, pos = read_varint(pos)
        tag, wire_type = key >> 3, key & 7
        if wire_type != 2:
            raise ValueError(f"поддерживается только wire type 2, получен {wire_type}")
        length, pos = read_varint(pos)
        end = pos + length
        if end > len(wire):
            raise ValueError("длина поля выходит за границу сообщения")
        result[tag] = wire[pos:end]
        pos = end
    return result
