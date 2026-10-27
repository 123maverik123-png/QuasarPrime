"""Распознавание речи (GigaAM) и детектор конца фразы (Silero VAD).

Настоящий uniproxy сам решает, когда пользователь договорил: шлёт ASR.Result c
endOfUtt=true и закрывает входной поток. Станция до этого продолжает слать микрофон,
поэтому VAD на нашей стороне обязателен.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave

from pathlib import Path

import numpy as np

from .audio import ASR_RATE
from .config import AsrCfg

log = logging.getLogger("asr")


# ---------------------------------------------------------------- VAD

class _SileroVad:
    """Silero VAD v6 (assets/silero_vad.onnx, MIT, github.com/snakers4/silero-vad) через onnxruntime,
    окна по 512 сэмплов.

    Модель рекуррентная: состояние (h, c) и 64 сэмпла контекста переносим между вызовами,
    иначе на коротких кусках она «не успевает разогреться» и пропускает речь.
    """

    WINDOW = 512
    CONTEXT = 64
    MODEL = Path(__file__).parent / "assets" / "silero_vad.onnx"

    def __init__(self) -> None:
        import onnxruntime  # type: ignore

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(str(self.MODEL), providers=["CPUExecutionProvider"],
                                                     sess_options=opts)
        self._lock = threading.Lock()

    def new_state(self) -> dict:
        return {"h": np.zeros((1, 1, 128), dtype=np.float32), "c": np.zeros((1, 1, 128), dtype=np.float32),
                "ctx": np.zeros(self.CONTEXT, dtype=np.float32)}

    def probs(self, pcm: np.ndarray, state: dict) -> np.ndarray:
        n = len(pcm) // self.WINDOW
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        windows = np.asarray(pcm[: n * self.WINDOW], dtype=np.float32).reshape(n, self.WINDOW)
        prev = np.concatenate([state["ctx"][None, :], windows[:-1, -self.CONTEXT:]], axis=0)
        batch = np.concatenate([prev, windows], axis=1)
        with self._lock:
            out, state["h"], state["c"] = self._session.run(None, {"input": batch, "h": state["h"], "c": state["c"]})
        state["ctx"] = windows[-1, -self.CONTEXT:].copy()
        return np.asarray(out, dtype=np.float32).reshape(-1)[:n]


_silero: _SileroVad | None = None
_silero_failed = False


def _get_silero() -> _SileroVad | None:
    global _silero, _silero_failed
    if _silero is None and not _silero_failed:
        try:
            _silero = _SileroVad()
        except Exception as e:  # noqa: BLE001
            log.warning("Silero VAD недоступен (%s), использую энергетический VAD", e)
            _silero_failed = True
    return _silero


class EndpointDetector:
    """Решает, закончилась ли фраза, по накопленному PCM 16 кГц."""

    def __init__(self, cfg: AsrCfg) -> None:
        self.cfg = cfg
        self._silero = _get_silero()
        self._flags: list[bool] = []  # речь/не речь по окнам
        self._win = _SileroVad.WINDOW
        self._audio = np.zeros(0, dtype=np.float32)  # всё полученное аудио
        self._done = 0  # сколько сэмплов уже размечено
        self._vad_state = self._silero.new_state() if self._silero else None
        self.speech_started = False

    def feed(self, chunk: np.ndarray) -> None:
        if chunk.size == 0:
            return
        self._audio = np.concatenate([self._audio, chunk])
        n = (len(self._audio) - self._done) // self._win
        if n == 0:
            return
        block = self._audio[self._done: self._done + n * self._win]
        if self._silero is not None:
            flags = (self._silero.probs(block, self._vad_state) >= self.cfg.vad_threshold).tolist()
        else:
            w = block.reshape(n, self._win)
            rms = np.sqrt(np.mean(w * w, axis=1) + 1e-12)
            flags = (20 * np.log10(rms) > self.cfg.vad_energy_db).tolist()
        self._done += n * self._win
        self._flags.extend(flags)
        # речь считается начавшейся после ~100 мс подряд (не считая фразы активации)
        if not self.speech_started:
            run = 0
            for f in self._flags[self._start_window:]:
                run = run + 1 if f else 0
                if run >= 3:
                    self.speech_started = True
                    break

    _start_window = 0  # с какого окна ищем речь команды
    SPOTTER_TAIL = 10  # ~320 мс: «Алиса» обычно заходит за маркер конца активации

    def mark_spotter_end(self) -> None:
        """Станция прислала streamcontrol action=2: до этого шёл 2-секундный буфер с «Алиса»."""
        self._start_window = -(-len(self._audio) // self._win) + self.SPOTTER_TAIL
        self.speech_started = False

    @property
    def duration_ms(self) -> int:
        return max(0, len(self._flags) - self._start_window) * self._win * 1000 // ASR_RATE

    def is_end(self) -> bool:
        ms_per = self._win * 1000 / ASR_RATE
        if self.duration_ms >= self.cfg.max_utterance_ms:
            return True
        if not self.speech_started:
            return self.duration_ms >= self.cfg.no_speech_timeout_ms
        need = int(self.cfg.vad_silence_ms / ms_per)
        tail = self._flags[-need:]
        return len(tail) >= need and not any(tail)

    def speech_only(self, pcm: np.ndarray, pad_ms: int = 200, max_gap_ms: int = 300) -> np.ndarray:
        """Вырезает тишину по разметке VAD: обрезает края и сжимает длинные паузы.

        Модели меньше работы, а в паузах не появляются «призрачные» слова."""
        flags = np.array(self._flags, dtype=bool)
        if not flags.any():
            return pcm
        win, pad = self._win, max(1, pad_ms * ASR_RATE // 1000 // self._win)
        keep = flags.copy()
        for shift in range(1, pad + 1):  # расширяем речь на pad окон в обе стороны
            keep[shift:] |= flags[:-shift]
            keep[:-shift] |= flags[shift:]
        max_gap = max(1, max_gap_ms * ASR_RATE // 1000 // win)
        out, gap = [], 0
        for i, k in enumerate(keep):
            gap = 0 if k else gap + 1
            if k or gap <= max_gap:
                out.append(pcm[i * win:(i + 1) * win])
        out.append(pcm[len(flags) * win:] if keep[-1] else pcm[:0])
        return np.concatenate(out) if out else pcm


# ---------------------------------------------------------------- ASR

class Recognizer:
    """engine: gigaam (по умолчанию) | openai (внешний /v1/audio/transcriptions)."""

    def __init__(self, cfg: AsrCfg) -> None:
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        _get_silero()
        if self._model is not None or self.cfg.engine == "openai":
            return
        import gigaam  # type: ignore
        import torch

        device = self.cfg.device if self.cfg.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
        log.info("Загрузка GigaAM '%s' (device=%s)...", self.cfg.model, device)
        self._model = gigaam.load_model(self.cfg.model, device=device)
        self._transcribe_gigaam(np.zeros(ASR_RATE, dtype=np.float32))  # прогрев CUDA
        log.info("GigaAM загружен")

    def _transcribe_gigaam(self, pcm: np.ndarray) -> str:
        import torch

        m = self._model
        with self._lock, torch.inference_mode():
            wav = torch.from_numpy(np.ascontiguousarray(pcm, dtype=np.float32)).to(m._device).to(m._dtype).unsqueeze(0)
            length = torch.full([1], wav.shape[-1], device=m._device)
            encoded, encoded_len = m.forward(wav, length)
            return m._decode(encoded, encoded_len, length)[0][0].strip()

    def _transcribe_local(self, pcm: np.ndarray) -> str:
        self.load()
        return self._transcribe_gigaam(pcm[: 24 * ASR_RATE])  # .transcribe GigaAM — до 25 с

    def _transcribe_openai(self, pcm: np.ndarray) -> str:
        import httpx

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(ASR_RATE)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
        r = httpx.post(
            self.cfg.base_url.rstrip("/") + "/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
            data={"model": self.cfg.model, "language": self.cfg.language},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()

    def transcribe_sync(self, pcm: np.ndarray) -> str:
        if pcm.size < ASR_RATE // 5:
            return ""
        # станция отдаёт очень тихий сигнал после VQE (пик ~ -40 дБ) — нормализуем
        peak = float(np.abs(pcm).max())
        if peak > 1e-5:
            pcm = pcm * min(0.5 / peak, 300.0)
        if self.cfg.engine == "openai":
            return self._transcribe_openai(pcm)
        return self._transcribe_local(pcm)

    async def transcribe(self, pcm: np.ndarray) -> str:
        text = await asyncio.to_thread(self.transcribe_sync, pcm)
        return _clean_hallucinations(text)


_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
)


def _clean_hallucinations(text: str) -> str:
    low = text.lower().strip(" .!")
    if any(low.startswith(h) for h in _HALLUCINATIONS):
        return ""
    return text
