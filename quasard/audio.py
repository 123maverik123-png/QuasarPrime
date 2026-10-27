"""Работа с аудио: разбор/сборка Ogg, декодирование Opus, кодирование ответа TTS.

Станция шлёт микрофон как Ogg/Opus 16 кГц моно (по одной Ogg-странице в бинарном
WS-фрейме). Ответ TTS Яндекс отдаёт как Ogg/Opus 48 кГц моно — так же делаем и мы.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

import av
import numpy as np

ASR_RATE = 16000


# ---------------------------------------------------------------- Ogg demux

@dataclass
class OggDemuxer:
    """Инкрементальный разбор Ogg: кормим байтами, получаем готовые пакеты."""

    _buf: bytearray = field(default_factory=bytearray)
    _partial: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        packets: list[bytes] = []
        while True:
            if len(self._buf) < 27:
                break
            start = self._buf.find(b"OggS")
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 27:
                break
            nsegs = self._buf[26]
            if len(self._buf) < 27 + nsegs:
                break
            lacing = self._buf[27:27 + nsegs]
            body_len = sum(lacing)
            total = 27 + nsegs + body_len
            if len(self._buf) < total:
                break
            body = bytes(self._buf[27 + nsegs:total])
            del self._buf[:total]
            pos = 0
            for seg in lacing:
                self._partial += body[pos:pos + seg]
                pos += seg
                if seg < 255:
                    packets.append(bytes(self._partial))
                    self._partial.clear()
        return packets


class OpusStreamDecoder:
    """Ogg/Opus (поток кусками) -> float32 PCM 16 кГц моно."""

    def __init__(self) -> None:
        self._demux = OggDemuxer()
        self._codec: av.CodecContext | None = None
        self._resampler = av.AudioResampler(format="flt", layout="mono", rate=ASR_RATE)
        self._pcm: list[np.ndarray] = []
        self._pts = 0

    def feed(self, data: bytes) -> np.ndarray:
        out: list[np.ndarray] = []
        for pkt in self._demux.feed(data):
            if pkt.startswith(b"OpusHead"):
                self._codec = av.CodecContext.create("opus", "r")
                self._codec.extradata = pkt
                continue
            if pkt.startswith(b"OpusTags") or self._codec is None:
                continue
            try:
                frames = self._codec.decode(av.Packet(pkt))
            except av.error.FFmpegError:
                continue
            for frame in frames:
                frame.pts = None
                for rf in self._resampler.resample(frame):
                    out.append(rf.to_ndarray().reshape(-1))
        chunk = np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
        if chunk.size:
            self._pcm.append(chunk)
        return chunk

    @property
    def pcm(self) -> np.ndarray:
        return np.concatenate(self._pcm) if self._pcm else np.zeros(0, dtype=np.float32)


def decode_any(data: bytes) -> np.ndarray:
    """Любой контейнер (wav/mp3/ogg) -> float32 PCM 16 кГц моно."""
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=ASR_RATE)
        chunks = []
        for frame in container.decode(audio=0):
            frame.pts = None
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


# ---------------------------------------------------------------- encode

def pcm_to_ogg_opus(pcm: np.ndarray, rate: int, bitrate: int = 48000) -> bytes:
    """float32/int16 PCM моно -> Ogg/Opus 48 кГц моно (формат ответа TTS Яндекса)."""
    if pcm.dtype != np.int16:
        pcm = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with av.open(buf, "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        stream.bit_rate = bitrate
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for rf in resampler.resample(frame):
            for p in stream.encode(rf):
                out.mux(p)
        for rf in resampler.resample(None):
            for p in stream.encode(rf):
                out.mux(p)
        for p in stream.encode(None):
            out.mux(p)
    return buf.getvalue()


def decode_to_pcm(data: bytes, rate: int = 48000) -> np.ndarray:
    """mp3/wav/... -> float32 PCM моно с частотой rate."""
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        chunks = []
        for frame in container.decode(audio=0):
            frame.pts = None
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def any_to_ogg_opus(data: bytes) -> bytes:
    """mp3/wav/... -> Ogg/Opus 48 кГц."""
    pcm = decode_to_pcm(data)
    return pcm_to_ogg_opus(pcm if pcm.size else np.zeros(4800, dtype=np.float32), 48000)


class _Sink(io.RawIOBase):
    def __init__(self) -> None:
        self.buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.buf += b
        return len(b)


class OggOpusStreamEncoder:
    """Непрерывный Ogg/Opus 48 кГц, который пополняется кусками PCM (по предложениям).

    add() возвращает готовые Ogg-страницы, их сразу можно отправлять станции.
    """

    def __init__(self, bitrate: int = 48000) -> None:
        self._sink = _Sink()
        # page_duration (мкс) — страницы по ~100 мс, чтобы звук уходил без задержки
        self._out = av.open(self._sink, "w", format="ogg", options={"page_duration": "100000"})
        self._stream = self._out.add_stream("libopus", rate=48000)
        self._stream.layout = "mono"
        self._stream.bit_rate = bitrate
        self._resamplers: dict[int, av.AudioResampler] = {}
        self._pts = 0
        self._sent = 0

    def _take(self) -> bytes:
        data = bytes(self._sink.buf[self._sent:])
        self._sent = len(self._sink.buf)
        return data

    def _encode(self, frame) -> None:
        for p in self._stream.encode(frame):
            self._out.mux(p)

    def add(self, pcm: np.ndarray, rate: int) -> bytes:
        if pcm.dtype != np.int16:
            pcm = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
        rs = self._resamplers.setdefault(rate, av.AudioResampler(format="s16", layout="mono", rate=48000,
                                                                 frame_size=960))
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for rf in rs.resample(frame):
            rf.pts = self._pts
            self._pts += rf.samples
            self._encode(rf)
        return self._take()

    def silence(self, seconds: float) -> bytes:
        return self.add(np.zeros(int(48000 * seconds), dtype=np.int16), 48000)

    def close(self) -> bytes:
        for rs in self._resamplers.values():
            for rf in rs.resample(None):
                rf.pts = self._pts
                self._pts += rf.samples
                self._encode(rf)
        self._encode(None)
        self._out.close()
        return self._take()


def split_ogg_pages(data: bytes, max_chunk: int = 32 * 1024) -> list[bytes]:
    """Режет Ogg по границам страниц на куски не больше max_chunk (для стриминга)."""
    pages: list[bytes] = []
    pos = 0
    while pos + 27 <= len(data) and data[pos:pos + 4] == b"OggS":
        nsegs = data[pos + 26]
        total = 27 + nsegs + sum(data[pos + 27:pos + 27 + nsegs])
        pages.append(data[pos:pos + total])
        pos += total
    if pos < len(data):
        pages.append(data[pos:])
    chunks: list[bytes] = []
    cur = b""
    for p in pages:
        if cur and len(cur) + len(p) > max_chunk:
            chunks.append(cur)
            cur = b""
        cur += p
    if cur:
        chunks.append(cur)
    return chunks


def frame_stream(stream_id: int, payload: bytes) -> bytes:
    """Бинарный WS-фрейм uniproxy: 4 байта streamId (big-endian) + данные."""
    return struct.pack(">I", stream_id) + payload


def parse_stream_frame(data: bytes) -> tuple[int, bytes]:
    return struct.unpack(">I", data[:4])[0], data[4:]
