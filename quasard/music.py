"""Музыка из Яндекс Музыки (библиотека yandex-music) в формате, который понимает станция.

Как это устроено у Яндекса (по трафику станции):
  - сервер присылает директиву audio_play со stream.url (прямая ссылка на mp3), metadata и callbacks;
  - вместе с ней — server_action «@@mm_stack_engine_get_next»: станция хранит его и присылает обратно,
    когда трек закончился или пользователь сказал «дальше», — сервер отвечает следующим audio_play;
  - callbacks on_started/on_finished — только для статистики (ignore_answer: true).
Здесь то же самое: очередь треков на станцию, «Моя волна» через rotor API, поиск по каталогу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import MusicCfg

log = logging.getLogger("music")

WAVE = "user:onyourwave"
GET_NEXT = "@@mm_stack_engine_get_next"
CB_PREFIX = "aqw_music_"  # имена наших колбэков (on_started/on_finished/on_failed)


@dataclass
class TrackInfo:
    id: str
    title: str
    artists: str
    duration_ms: int
    cover: str  # avatars.yandex.net/get-music-content/.../%%
    album_id: str = ""


@dataclass
class Session:
    kind: str  # "radio" | "list"
    title: str
    station: str = ""  # для радио
    batch_id: str = ""
    queue: list[TrackInfo] = field(default_factory=list)
    pos: int = -1
    started_at: float = 0.0
    fail_streak: int = 0  # сколько треков подряд станция не смогла проиграть

    @property
    def current(self) -> TrackInfo | None:
        return self.queue[self.pos] if 0 <= self.pos < len(self.queue) else None


def _track_info(t) -> TrackInfo:
    return TrackInfo(
        id=str(t.id),
        title=t.title or "",
        artists=", ".join(a.name for a in (t.artists or []) if a.name),
        duration_ms=int(t.duration_ms or 0),
        cover=t.cover_uri or "",
        album_id=str(t.albums[0].id) if t.albums else "",
    )


class Music:
    def __init__(self, cfg: MusicCfg, data_dir: Path) -> None:
        self.cfg = cfg
        self.token_file = data_dir / "music_token.json"
        self._client = None
        self._lock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}

    # ------------------------------------------------------------ клиент
    def _token(self) -> str:
        if self.cfg.token:
            return self.cfg.token
        if self.token_file.exists():
            return json.loads(self.token_file.read_text(encoding="utf-8")).get("access_token", "")
        return ""

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and bool(self._token())

    async def client(self):
        async with self._lock:
            if self._client is None:
                from yandex_music import ClientAsync  # type: ignore

                self._client = await ClientAsync(self._token(), language="ru").init()
                log.info("Яндекс Музыка: вход выполнен (uid=%s)", self._client.me.account.uid)
            return self._client

    # ------------------------------------------------------------ источники
    async def start_wave(self, device: str) -> Session:
        c = await self.client()
        s = Session(kind="radio", title="Моя волна", station=WAVE)
        await self._radio_more(c, s)
        try:
            await c.rotor_station_feedback_radio_started(WAVE, "alice", s.batch_id, time.time())
        except Exception:  # noqa: BLE001 — обратная связь не критична
            pass
        self.sessions[device] = s
        return s

    async def _radio_more(self, c, s: Session) -> None:
        last = s.queue[-1].id if s.queue else None
        res = await c.rotor_station_tracks(s.station, queue=last)
        s.batch_id = res.batch_id or s.batch_id
        s.queue += [_track_info(x.track) for x in res.sequence if x.track]

    async def start_search(self, device: str, query: str) -> Session | None:
        c = await self.client()
        r = await c.search(query, type_="all")
        if r is None:
            return None
        best = r.best
        tracks, title = [], query
        if best is not None:
            obj = best.result
            if best.type == "artist":
                title = obj.name
                tracks = (await c.artists_tracks(obj.id, page_size=50)).tracks
            elif best.type == "track":
                title = f"{obj.title}"
                tracks = [obj]
                if obj.artists:  # после трека — другие песни этого исполнителя
                    more = (await c.artists_tracks(obj.artists[0].id, page_size=30)).tracks
                    tracks += [t for t in more if str(t.id) != str(obj.id)]
            elif best.type == "album":
                title = obj.title
                album = await c.albums_with_tracks(obj.id)
                tracks = [t for vol in (album.volumes or []) for t in vol]
            elif best.type == "playlist":
                title = obj.title
                pl = await c.users_playlists(obj.kind, obj.owner.uid)
                tracks = [x.track or await x.fetch_track_async() for x in (pl.tracks or [])[:100]]
        if not tracks and r.tracks and r.tracks.results:
            tracks = r.tracks.results[:20]
        tracks = [t for t in tracks if t and t.available is not False]
        if not tracks:
            return None
        s = Session(kind="list", title=title, queue=[_track_info(t) for t in tracks])
        self.sessions[device] = s
        return s

    # ------------------------------------------------------------ навигация
    async def step(self, device: str, delta: int, skipped: bool = False) -> TrackInfo | None:
        s = self.sessions.get(device)
        if s is None:
            return None
        c = await self.client()
        if s.kind == "radio" and s.current is not None:
            played = max(0.0, time.time() - s.started_at)
            try:
                if skipped:
                    await c.rotor_station_feedback_skip(s.station, s.current.id, played, s.batch_id)
                else:
                    await c.rotor_station_feedback_track_finished(s.station, s.current.id, played, s.batch_id)
            except Exception:  # noqa: BLE001
                pass
        s.pos = max(0, s.pos + delta)
        if s.kind == "radio" and s.pos >= len(s.queue) - 1:
            await self._radio_more(c, s)
        if s.pos >= len(s.queue):
            return None
        return s.current

    async def stream_url(self, track: TrackInfo) -> tuple[str, int]:
        c = await self.client()
        infos = await c.tracks_download_info(track.id, get_direct_links=True)
        mp3 = sorted((i for i in infos if i.codec == "mp3"), key=lambda i: i.bitrate_in_kbps or 0)
        if not mp3:
            raise RuntimeError(f"нет mp3 для трека {track.id}")
        pick = mp3[-1] if self.cfg.quality == "high" else mp3[0]
        url = pick.direct_link or await pick.get_direct_link_async()
        return await _final_url(url), int(pick.bitrate_in_kbps or 192)

    async def rate(self, device: str, like: bool) -> TrackInfo | None:
        """Лайк / дизлайк текущего трека в аккаунте Яндекс Музыки (влияет на «Мою волну»)."""
        s = self.sessions.get(device)
        if s is None or s.current is None:
            return None
        c = await self.client()
        if like:
            await c.users_likes_tracks_add(s.current.id)
        else:
            await c.users_dislikes_tracks_add(s.current.id)
        return s.current

    async def track_started(self, device: str) -> None:
        s = self.sessions.get(device)
        if s is None or s.current is None:
            return
        s.started_at = time.time()
        s.fail_streak = 0
        if s.kind == "radio":
            try:
                c = await self.client()
                await c.rotor_station_feedback_track_started(s.station, s.current.id, s.batch_id, time.time())
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ директивы для станции
    async def play_directives(self, device: str, request_id: str, track: TrackInfo) -> list[dict]:
        """audio_play + @@mm_stack_engine_get_next (как у Яндекса)."""
        s = self.sessions[device]
        url, bitrate = await self.stream_url(track)
        nxt = s.queue[s.pos + 1] if s.pos + 1 < len(s.queue) else None
        music_meta = {"type": "Radio" if s.kind == "radio" else "Playlist", "id": s.station or s.title,
                      "description": "", "repeat_mode": "None"}
        if nxt:
            music_meta["next_track_info"] = {"type": "Track", "id": nxt.id}

        def cb(name: str, ignore: bool = True) -> dict:
            return {"name": CB_PREFIX + name, "payload": {"track_id": track.id, "@request_id": request_id},
                    "ignore_answer": ignore, "type": "server_action", "is_led_silent": True}

        audio_play = {
            "name": "audio_play",
            "sub_name": "music",
            "type": "client_action",
            "is_led_silent": True,
            "payload": {
                "stream": {"id": track.id, "type": "Track", "url": url, "format": "MP3", "codec": "Mp3",
                           "bitrate": bitrate, "offset_ms": 0, "duration_ms": track.duration_ms},
                "metadata": {"title": track.title, "subtitle": track.artists, "art_image_url": track.cover,
                             "hide_progress_bar": False, "glagol_metadata": {"music_metadata": music_meta}},
                "callbacks": {"on_started": cb("on_started"), "on_stopped": cb("on_stopped"),
                              "on_finished": cb("on_finished"), "on_failed": cb("on_failed", ignore=False)},
                "scenario_meta": {"owner": "music"},
                "background_mode": "Ducking",
                "screen_type": "Music",
                "provider_name": "",
                "set_pause": False,
                "playback_speed": 1,
                "karaoke_mode": "Undefined",
            },
        }
        get_next = {
            "name": GET_NEXT,
            "type": "server_action",
            "is_led_silent": True,
            "payload": {"@request_id": request_id, "stack_session_id": request_id,
                        "@scenario_name": "HollywoodMusic", "stack_product_scenario_name": "music",
                        "@recovery_params": {}, "aqw_device": device},
        }
        return [audio_play, get_next]


async def _final_url(url: str) -> str:
    """api.music.yandex.net/get-mp3/... отвечает 308 на strm-*.strm.yandex.net. Старый плеер станции
    (GStreamer 2022 г.) 308 не проходит — отдаём ему конечную ссылку, как это делает сервер Яндекса."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as h:
        for _ in range(5):
            r = await h.get(url, headers={"Range": "bytes=0-0"})
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = r.headers["location"]
                continue
            break
    return url


def say_track(t: TrackInfo) -> str:
    return f"{t.artists}, {t.title}." if t.artists else f"{t.title}."
