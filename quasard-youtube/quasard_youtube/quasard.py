"""Python-хук для quasard: очистить ответ LLM и запустить YouTube-плейлист.

Использование в alice_server (псевдокод)::

    from quasard_youtube.quasard import process_reply

    reply = await llm_chat(messages)
    spoken = await process_reply_async(reply, "http://127.0.0.1:8099")
    await tts.speak(spoken)

Если bridge недоступен, команда не озвучивается: вместо неё возвращается
короткая фраза об ошибке.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from .command import parse_command

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8099"
DEFAULT_ERROR_REPLY = "Не получилось включить музыку. Проверьте, запущен ли quasard-youtube."


def _post_json(url: str, payload: dict, *, timeout: float, api_token: str = "") -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "quasard-youtube-hook/0.1",
        },
    )
    if api_token:
        request.add_header("X-Api-Token", api_token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def process_reply(
    text: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    *,
    timeout: float = 90.0,
    api_token: str = "",
    empty_reply: str = "Включаю.",
    error_reply: str = DEFAULT_ERROR_REPLY,
) -> str:
    """Синхронный вариант: вернуть текст, который должен озвучить quasard."""
    parsed = parse_command(text)
    if not parsed.matched:
        return text
    try:
        result = _post_json(
            f"{bridge_url.rstrip('/')}/api/command",
            {"text": text},
            timeout=timeout,
            api_token=api_token,
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        return f"{error_reply} {detail}".strip() if detail else error_reply
    except (urllib.error.URLError, TimeoutError, OSError):
        return error_reply

    spoken = str(result.get("spoken_text") or result.get("text") or "").strip()
    if spoken:
        return spoken
    return empty_reply


async def process_reply_async(
    text: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    *,
    timeout: float = 90.0,
    api_token: str = "",
    empty_reply: str = "Включаю.",
    error_reply: str = DEFAULT_ERROR_REPLY,
) -> str:
    """Async-обёртка, не блокирующая event loop quasard."""
    return await asyncio.to_thread(
        process_reply,
        text,
        bridge_url,
        timeout=timeout,
        api_token=api_token,
        empty_reply=empty_reply,
        error_reply=error_reply,
    )


__all__ = [
    "DEFAULT_BRIDGE_URL",
    "DEFAULT_ERROR_REPLY",
    "process_reply",
    "process_reply_async",
]
