from __future__ import annotations

import base64
import json

from quasard_youtube.protobuf import (
    build_audio_play,
    build_radio_play,
    decode_message,
    encode_field,
)


def _payload(command: dict) -> tuple[bytes, str, dict]:
    assert command["command"] == "externalCommandBypass"
    wire = base64.b64decode(command["data"])
    fields = decode_message(wire)
    name = fields[1].decode()
    body = json.loads(fields[2].decode())
    return wire, name, body


def test_radio_play_wire_format() -> None:
    command = build_radio_play(
        "http://192.168.50.95:8099/stream/abc.mp3", "Название", "Исполнитель"
    )
    _, name, body = _payload(command)
    assert name == "radio_play"
    assert body == {
        "streamUrl": "http://192.168.50.95:8099/stream/abc.mp3",
        "force_restart_player": True,
        "title": "Название",
        "subtitle": "Исполнитель",
    }


def test_audio_play_wire_format() -> None:
    command = build_audio_play("http://127.0.0.1:8099/x.mp3", "T", "A")
    _, name, body = _payload(command)
    assert name == "audio_play"
    assert body["stream"]["format"] == "MP3"
    assert body["stream"]["type"] == "Track"
    assert body["stream"]["offset_ms"] == 0
    assert body["set_pause"] is False
    assert body["metadata"] == {"title": "T", "subtitle": "A"}


def test_long_field_uses_multibyte_varint() -> None:
    value = "я" * 100
    wire = encode_field(2, value)
    assert wire[0] == (2 << 3) | 2
    assert decode_message(wire)[2].decode() == value


def test_no_optional_metadata_when_empty() -> None:
    _, _, body = _payload(build_radio_play("http://x/y.mp3"))
    assert "title" not in body
    assert "subtitle" not in body
