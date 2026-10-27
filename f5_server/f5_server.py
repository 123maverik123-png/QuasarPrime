"""Локальный сервис синтеза F5-TTS (ESpeech RL-V2, русский) с клоном голоса по эталону.

Работает в отдельном окружении (tts_bench\\env_f5, torch 2.8), чтобы не конфликтовать с основным сервером.
API в стиле OpenAI:  POST /v1/audio/speech {"input": "текст"} -> audio/wav (24 кГц)
                     GET  /health

    env_f5\\Scripts\\python tools\\f5_server.py --ref voice\\ref_voice.wav --ref-text voice\\ref_voice.txt
"""
from __future__ import annotations

import argparse
import io
import logging
import threading
import time

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("f5")
app = FastAPI()
_state: dict = {}
_lock = threading.Lock()


def _patch_ruaccent() -> None:
    # RUAccent + новые tokenizers: в onnx-модель не передаётся token_type_ids — подставляем нули
    import onnxruntime as ort

    run = ort.InferenceSession.run

    def run_fixed(self, output_names, feed, *a, **k):
        if "token_type_ids" not in feed and "input_ids" in feed and \
                any(i.name == "token_type_ids" for i in self.get_inputs()):
            feed = {**feed, "token_type_ids": np.zeros_like(feed["input_ids"])}
        return run(self, output_names, feed, *a, **k)
    ort.InferenceSession.run = run_fixed


def load(ref: str, ref_text: str, nfe: int) -> None:
    from f5_tts.infer.utils_infer import load_model, load_vocoder, preprocess_ref_audio_text
    from f5_tts.model import DiT
    from huggingface_hub import hf_hub_download
    from ruaccent import RUAccent

    _patch_ruaccent()
    t0 = time.monotonic()
    repo = "ESpeech/ESpeech-TTS-1_RL-V2"
    ckpt, vocab = hf_hub_download(repo, "espeech_tts_rlv2.pt"), hf_hub_download(repo, "vocab.txt")
    model = load_model(DiT, dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4), ckpt,
                       vocab_file=vocab)
    vocoder = load_vocoder()
    acc = RUAccent()
    acc.load(omograph_model_size="turbo3.1", use_dictionary=True, tiny_mode=False)
    ref_audio, ref_txt = preprocess_ref_audio_text(ref, acc.process_all(ref_text), show_info=lambda *a: None)
    _state.update(model=model, vocoder=vocoder, acc=acc, ref_audio=ref_audio, ref_text=ref_txt, nfe=nfe)
    synth("Прогрев.")
    log.info("F5-TTS загружен за %.1f c (nfe=%d)", time.monotonic() - t0, nfe)


def synth(text: str) -> tuple[np.ndarray, int]:
    from f5_tts.infer.utils_infer import infer_process

    s = _state
    with _lock:
        # «+» в тексте — уже расставленные ударения, иначе ставит RUAccent
        gen = text if "+" in text else s["acc"].process_all(text)
        
        # Добавляем пробелы в начале и конце, чтобы модель не начинала резко
        gen = " " + gen + " "
        
        # Замедляем скорость с 1.0 до 0.85 (можно попробовать 0.8 или 0.9)
        wav, sr, _ = infer_process(s["ref_audio"], s["ref_text"], gen, s["model"], s["vocoder"],
                                   cross_fade_duration=0.15, nfe_step=s["nfe"], speed=0.85,
                                   show_info=lambda *a: None)
        
        # Добавляем 200 мс (0.2 сек) чистой тишины в начало и конец
        pad_len = int(sr * 0.2)
        wav = np.pad(np.asarray(wav, dtype=np.float32), (pad_len, pad_len), mode='constant', constant_values=0.0)
        
    return np.asarray(wav, dtype=np.float32), sr


@app.get("/health")
def health():
    return {"ok": "model" in _state}


@app.post("/v1/audio/speech")
def speech(body: dict):
    text = (body.get("input") or "").strip()
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    t0 = time.monotonic()
    pcm, sr = synth(text)
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
    log.info("%.2fs на %.1fs аудио: %r", time.monotonic() - t0, len(pcm) / sr, text[:60])
    return Response(buf.getvalue(), media_type="audio/wav")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--ref-text", required=True, help="файл с текстом эталона")
    ap.add_argument("--nfe", type=int, default=32, help="шаги: 32 — качество, 16 — быстрее")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    load(args.ref, open(args.ref_text, encoding="utf-8").read().strip(), args.nfe)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
