from __future__ import annotations

from quasard_youtube.command import parse_command


def test_exact_line() -> None:
    parsed = parse_command("PLAY_YOUTUBE: Кино — Группа крови")
    assert parsed.matched
    assert parsed.query == "Кино — Группа крови"
    assert parsed.spoken_text == ""


def test_command_with_spoken_text() -> None:
    parsed = parse_command("Сейчас включу.\nPLAY_YOUTUBE: Nirvana Smells Like Teen Spirit\nХорошего вечера!")
    assert parsed.matched
    assert parsed.query == "Nirvana Smells Like Teen Spirit"
    assert parsed.spoken_text == "Сейчас включу.\n\nХорошего вечера!"


def test_fenced_response() -> None:
    parsed = parse_command("```text\nPLAY_YOUTUBE: Молчат Дома Судно\n```")
    assert parsed.matched
    assert parsed.query == "Молчат Дома Судно"
    assert parsed.spoken_text == ""


def test_quotes_and_youtube_noise() -> None:
    parsed = parse_command('PLAY_YOUTUBE: "включи на YouTube Кино — Звезда по имени Солнце"')
    assert parsed.matched
    assert parsed.query == "Кино — Звезда по имени Солнце"


def test_inline_command() -> None:
    parsed = parse_command("Включаю: PLAY_YOUTUBE: Radiohead Creep")
    assert parsed.matched
    assert parsed.query == "Radiohead Creep"


def test_no_command() -> None:
    text = "Сегодня облачно, около пятнадцати градусов."
    parsed = parse_command(text)
    assert not parsed.matched
    assert parsed.query == ""
    assert parsed.spoken_text == ""


def test_empty_query_is_not_a_command() -> None:
    parsed = parse_command("PLAY_YOUTUBE:   ")
    assert not parsed.matched


def test_noise_at_end() -> None:
    parsed = parse_command("PLAY_YOUTUBE: Linkin Park Numb на YouTube")
    assert parsed.query == "Linkin Park Numb"
