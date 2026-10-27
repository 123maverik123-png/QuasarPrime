"""Разбор служебной команды ``PLAY_YOUTUBE: <запрос>`` в ответе LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Строгая форма — отдельная строка, как просит системный промпт.
_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:```[a-z0-9_-]*[ \t]*)?`?PLAY_YOUTUBE`?[ \t]*:[ \t]*(?P<query>[^\r\n]+?)[ \t]*$"
)
# Мягкая форма — команда встретилась внутри строки. Нужна для старых/сбитых ответов.
_INLINE_RE = re.compile(r"(?i)\bPLAY_YOUTUBE\b[ \t]*:[ \t]*(?P<query>[^\r\n]+)")
_FENCE_RE = re.compile(r"(?m)^[ \t]*```[a-z0-9_-]*[ \t]*$")
_EDGE_QUOTES = " \t\r\n\"'`«»“”„"
_LEADING_NOISE_RE = re.compile(
    r"(?i)^(?:пожалуйста[,\s]+)?(?:включи|включить|поставь|поставить|найди|найти|"
    r"запусти|запустить)\s+"
)
_TRAILING_NOISE_RE = re.compile(
    r"(?i)\s+(?:на|в|с)\s+(?:ютьюб|ютуб|youtube)(?:е|а)?\.?$"
)
_LEADING_YOUTUBE_RE = re.compile(
    r"(?i)^(?:на|в|с)\s+(?:ютьюб|ютуб|youtube)(?:е|а)?[,\s]+"
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    matched: bool
    query: str = ""
    spoken_text: str = ""
    raw: str = ""


def _clean_query(value: str) -> str:
    query = value.strip().strip(_EDGE_QUOTES)
    for _ in range(2):
        query = _LEADING_NOISE_RE.sub("", query)
        query = _LEADING_YOUTUBE_RE.sub("", query)
    query = _TRAILING_NOISE_RE.sub("", query)
    query = re.sub(r"\s+", " ", query).strip(" \t,;:-—")
    return query.strip(_EDGE_QUOTES)


def _clean_spoken(value: str) -> str:
    text = _FENCE_RE.sub("", value)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_command(text: str) -> ParsedCommand:
    """Найти первую команду и вернуть текст, который останется озвучить.

    Команда удаляется из ``spoken_text`` целиком. Если LLM вернул только
    команду, ``spoken_text`` пустой — вызывающая сторона решает, что сказать.
    """
    if not text:
        return ParsedCommand(matched=False, raw=text or "")

    match = _LINE_RE.search(text)
    if match is None:
        match = _INLINE_RE.search(text)

    if match is None:
        return ParsedCommand(matched=False, raw=text)

    query = _clean_query(match.group("query"))
    if not query:
        return ParsedCommand(matched=False, raw=text)

    start, end = match.span()
    spoken = f"{text[:start]}\n{text[end:]}"
    return ParsedCommand(
        matched=True,
        query=query[:300],
        spoken_text=_clean_spoken(spoken),
        raw=text,
    )


def strip_command(text: str) -> str:
    """Удобный shortcut: вернуть только очищенный текст ответа."""
    parsed = parse_command(text)
    return parsed.spoken_text if parsed.matched else text
