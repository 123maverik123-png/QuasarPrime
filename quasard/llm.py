"""Диалог с LLM через OpenAI-совместимый Chat Completions API со стримингом.

Ответ режется на фразы по знакам препинания, чтобы озвучка начиналась с первой фразы,
пока модель ещё пишет остальное.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import ROOT, LlmCfg

log = logging.getLogger("llm")

_MD_RE = re.compile(r"[*_#`>|]+")
_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
# конец фразы: . ! ? … ; или перевод строки, за которыми пробел/конец
_SENT_END = re.compile(r"([.!?…;]+[»\")]*|\n)(\s+|$)")
_SOFT_END = re.compile(r"[,:—–](\s+)")


def clean_for_speech(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # markdown-ссылки
    text = _MD_RE.sub("", text)
    text = re.sub(r"^\s*([-•]|\d+[.)])\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Копит поток токенов и отдаёт законченные фразы."""

    def __init__(self, first_min: int = 12, soft_max: int = 120) -> None:
        self.buf = ""
        self.first_min = first_min  # первая фраза может быть короткой — быстрее старт озвучки
        self.soft_max = soft_max  # длинную фразу без точки режем по запятой
        self.count = 0

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out = []
        while True:
            need = self.first_min if self.count == 0 else 2
            cut = next((m.end() for m in _SENT_END.finditer(self.buf) if len(self.buf[:m.end()].strip()) >= need),
                       None)
            if cut is None and len(self.buf) > self.soft_max:
                soft = [s for s in _SOFT_END.finditer(self.buf) if s.end() >= 30]
                if soft:
                    cut = soft[-1].end()
            if cut is None:
                break
            sentence = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if sentence:
                out.append(sentence)
                self.count += 1
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


class Dialog:
    def __init__(self, cfg: LlmCfg, tz: str = "Europe/Moscow") -> None:
        self.cfg = cfg
        self.tz = tz
        self._history: dict[str, list[dict]] = {}
        self._last: dict[str, float] = {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s, connect=10))
        self._extra_ok = True  # сервер принимает доп. поля (reasoning и т.п.)
        self._prompt_mtime: float | None = None
        self._prompt_text = ""

    def reset(self, key: str) -> None:
        self._history.pop(key, None)

    def _system_prompt(self) -> str:
        """Промпт из файла (перечитывается при изменении) или из конфига."""
        path = Path(self.cfg.system_prompt_file)
        if not path.is_absolute():
            path = ROOT / path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self.cfg.system_prompt
        if mtime != self._prompt_mtime:
            self._prompt_text = path.read_text(encoding="utf-8-sig").strip()
            self._prompt_mtime = mtime
            log.info("Системный промпт загружен из %s", path)
        return self._prompt_text or self.cfg.system_prompt

    def _messages(self, key: str, text: str) -> list[dict]:
        if time.time() - self._last.get(key, 0) > self.cfg.history_ttl_s:
            self._history[key] = []
        hist = self._history.setdefault(key, [])
        dt = datetime.now(ZoneInfo(self.tz))
        now = f"{_WEEKDAYS[dt.weekday()]}, {dt:%d.%m.%Y %H:%M}"
        system = f"{self._system_prompt()}\nТекущие дата и время: {now} ({self.tz})."
        return [{"role": "system", "content": system}, *hist, {"role": "user", "content": text}]

    def _remember(self, key: str, text: str, answer: str) -> None:
        hist = self._history.setdefault(key, [])
        hist += [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
        del hist[: max(0, len(hist) - 2 * self.cfg.history_turns)]
        self._last[key] = time.time()

    async def _tokens(self, messages: list[dict]) -> AsyncIterator[str]:
        body: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        }
        if self._extra_ok:
            body.update(self.cfg.extra_body)
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key or os.environ.get('GROQ_API_KEY', 'none')}"}
        async with self._client.stream("POST", url, json=body, headers=headers) as r:
            if r.status_code == 400 and self._extra_ok and self.cfg.extra_body:
                err = (await r.aread()).decode(errors="replace")
                log.warning("Сервер не принял extra_body (%s), повторяю без него: %.200s", list(self.cfg.extra_body), err)
                self._extra_ok = False
                async for t in self._tokens(messages):
                    yield t
                return
            if r.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("content"):  # reasoning_content и т.п. пропускаем
                    yield delta["content"]

    async def stream(self, key: str, text: str) -> AsyncIterator[str]:
        """Отдаёт фразы ответа по мере генерации (уже очищенные для озвучки)."""
        if self.cfg.base_url == "echo":  # отладка без LLM
            for s in (f"Вы сказали: {text}", "Это тестовый ответ."):
                yield s
            return
        t0 = time.monotonic()
        splitter = SentenceSplitter()
        full: list[str] = []
        state, pending = "start", ""  # вырезаем <think>…</think> в начале ответа на лету
        async for tok in self._tokens(self._messages(key, text)):
            if state != "normal":
                pending += tok
                head = pending.lstrip()
                if state == "start":
                    if head.startswith("<think>"):
                        state = "think"
                    elif head and not "<think>".startswith(head):
                        state, tok = "normal", pending
                if state == "think":
                    if "</think>" not in pending:
                        continue
                    state, tok = "normal", pending.split("</think>", 1)[1]
                if state != "normal":
                    continue
            for s in splitter.feed(tok):
                s = clean_for_speech(s)
                if s:
                    if not full:
                        log.info("LLM первая фраза через %.2fs", time.monotonic() - t0)
                    full.append(s)
                    yield s
        for s in splitter.flush():
            s = clean_for_speech(s)
            if s:
                full.append(s)
                yield s
        answer = " ".join(full)
        log.info("LLM %.2fs: %r -> %r", time.monotonic() - t0, text, answer)
        if answer:
            self._remember(key, text, answer)
