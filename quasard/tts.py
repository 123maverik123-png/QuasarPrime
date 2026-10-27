"""Синтез речи. Каждый движок отдаёт PCM (float32 моно + частота) для одного предложения;
uniproxy склеивает предложения в непрерывный Ogg/Opus 48 кГц и стримит станции.

Движки:
  f5     — F5-TTS ESpeech с клоном голоса по эталону: отдельный процесс tools/f5_server.py (env_f5)
  edge   — Microsoft Edge TTS (онлайн, без ключа) — запасной
  openai — любой OpenAI-совместимый /audio/speech
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

from .audio import decode_to_pcm, pcm_to_ogg_opus
from .config import TtsCfg

log = logging.getLogger("tts")


class Synthesizer:
    def __init__(self, cfg: TtsCfg, cache_dir: Path) -> None:
        self.cfg = cfg
        self.cache_dir = cache_dir / "tts_cache"
        self._lock = threading.Lock()

    # ------------------------------------------------------------ API
    def load(self) -> None:
        """Движки сетевые (f5/edge/openai), грузить при старте нечего."""

    async def synth_pcm(self, text: str) -> tuple[np.ndarray, int]:
        text = _prepare(text)
        if not text:
            return np.zeros(0, dtype=np.float32), 48000
        key = hashlib.sha1(f"{self.cfg.engine}|{self._voice_id()}|{text}".encode()).hexdigest()
        cached = self.cache_dir / f"{key}.npz"
        if self.cfg.cache and cached.exists():
            z = np.load(cached)
            return z["pcm"].astype(np.float32), int(z["rate"])
        t0 = time.monotonic()
        if self.cfg.engine == "edge":
            pcm, rate = await self._edge(text)
        elif self.cfg.engine == "openai":
            pcm, rate = await self._openai(text, self.cfg.base_url)
        elif self.cfg.engine == "f5":  # отдельный процесс tools/f5_server.py (клон голоса)
            pcm, rate = await self._openai(text, self.cfg.f5_url)
        else:
            raise ValueError(f"Неизвестный TTS engine: {self.cfg.engine}")
        log.info("TTS %.2fs (%.1fs аудио): %r", time.monotonic() - t0, len(pcm) / rate, text[:60])
        if self.cfg.cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            np.savez(cached, pcm=pcm.astype(np.float16), rate=rate)
        return pcm, rate

    async def synth(self, text: str) -> bytes:
        """Целиком в Ogg/Opus (для TTS.Generate и /_say)."""
        pcm, rate = await self.synth_pcm(text)
        return pcm_to_ogg_opus(pcm if pcm.size else np.zeros(4800, dtype=np.float32), rate)

    def _voice_id(self) -> str:
        c = self.cfg
        return {"edge": f"{c.voice}{c.rate}", "f5": c.f5_voice_id}.get(c.engine, c.voice)

    # ------------------------------------------------------------ сетевые
    async def _edge(self, text: str) -> tuple[np.ndarray, int]:
        import edge_tts  # type: ignore

        comm = edge_tts.Communicate(text, self.cfg.voice, rate=self.cfg.rate)
        mp3 = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        return await asyncio.to_thread(decode_to_pcm, bytes(mp3)), 48000

    async def _openai(self, text: str, base_url: str) -> tuple[np.ndarray, int]:
        import httpx

        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post(
                base_url.rstrip("/") + "/audio/speech",
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json={"model": self.cfg.openai_model, "voice": self.cfg.voice, "input": text, "response_format": "wav"},
            )
            r.raise_for_status()
        return await asyncio.to_thread(decode_to_pcm, r.content), 48000


_UNITS = {"%": " процентов", "°C": " градусов", "°": " градусов", "км/ч": " километров в час", "&": " и "}


def _prepare(text: str) -> str:
    text = text.strip()
    for k, v in _UNITS.items():
        text = text.replace(k, v)
    text = re.sub(r"[«»\"“”„*_#]", "", text)
    return re.sub(r"\s+", " ", text).strip()
