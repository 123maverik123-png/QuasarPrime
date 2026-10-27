from __future__ import annotations

from quasard_youtube import quasard


def test_no_command_returns_original(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("bridge must not be called")

    monkeypatch.setattr(quasard, "_post_json", boom)
    assert quasard.process_reply("Привет!") == "Привет!"


def test_command_calls_bridge(monkeypatch) -> None:
    calls = []

    def fake(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        return {"matched": True, "spoken_text": "Включаю."}

    monkeypatch.setattr(quasard, "_post_json", fake)
    spoken = quasard.process_reply(
        "PLAY_YOUTUBE: Кино — Группа крови",
        "http://127.0.0.1:8099/",
    )
    assert spoken == "Включаю."
    assert calls[0][0] == "http://127.0.0.1:8099/api/command"
    assert calls[0][1]["text"].startswith("PLAY_YOUTUBE:")
