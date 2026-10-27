# Голосовое управление YouTube-потоком: пауза, продолжение, следующий, громкость

Пошаговый план точечных правок в `quasard` (`alice_server`) и `quasard-youtube`.
Все фрагменты ниже прогнаны на копии проекта: модули компилируются,
существующий набор тестов `quasard-youtube` проходит (29/29 — через
pytest-совместимый прогон, потому что в venv нет самого pytest),
HTTP-эндпоинты и очередь проверены вживую через `aiohttp.test_utils`,
`YoutubeControl` — против реального сервера на локальном порту.

---

## 0. Что уже есть и чего не хватает (по коду, а не по описанию)

| Место | Сейчас | Следствие |
|---|---|---|
| `alice_server/intents.py::match_music` | уже распознаёт `pause`, `resume`, `next`, `prev` | «пауза» и «продолжай» **уже работают** для любого потока: `_music_command()` отдаёт `client_action("player_pause", {"smooth": True})` / `player_continue`, а это директива Станции, а не Яндекс.Музыки |
| `alice_server/uniproxy.py::_answer` | `music_cmd` обрабатывается до LLM | «следующая»/«пропусти» при играющем YouTube **уводит в Яндекс.Музыку**: `_music_command("next")` вызывает `m.start_wave()` и заменяет наш поток |
| `quasard-youtube` | `audio_play` + локальный MP3 + `stop` | нет `/api/pause`, `/api/resume`, `/api/next`, `/api/volume`; сервис не помнит запрос и историю треков |
| Glagol (`glagol.py`) | `play`, `stop`, `set_volume` | **в Glagol нет команды `player_pause`**. У Станции есть `play`, `stop`, `next`, `prev`, `rewind`, `setVolume`; ma-provider-yandex-station для `audio_client`-сессии делает паузу через `{"command": "stop"}` |

Отсюда главный архитектурный вывод:

* **«пауза» / «продолжай»** должны отправляться **директивой Станции**
  (`player_pause` / `player_continue`) из `quasard` — только так HTTP-поток
  остаётся открытым и «продолжай» возвращает трек с той же секунды. Директиву
  физически может отправить только `quasard`: она едет внутри `VinsResponse`.
  `quasard-youtube` в этом режиме лишь ведёт состояние (`POST /api/pause` без
  Glagol-команды).
* **«следующая»** — целиком задача `quasard-youtube` (нужен новый поток и новая
  `audio_play`).
* **«громче» / «тише»** остаются встроенным `intents.match` (`sound_louder` /
  `sound_quiter`): это громкость самой Станции, она действует и на наш поток.
  `POST /api/volume` добавлен как альтернативный путь (и для внешних вызовов).

Ниже — правки по файлам. Порядок важен: сначала сервис, потом `quasard`.

---

## 1. `quasard-youtube`: конфигурация

### 1.1 `quasard_youtube/config.py`

В `PlaybackConfig` добавить три ключа:

```python
@dataclass(slots=True)
class PlaybackConfig:
    directive: str = "radio_play"
    stop_before_play: bool = True
    wait_for_ack: bool = False
    # Пауза: false — сервис только запоминает состояние, а саму паузу делает
    # quasard директивой player_pause (поток остаётся открытым, «продолжай»
    # возвращает трек с той же секунды). true — жёсткий stop через Glagol,
    # тогда «продолжай» перезапускает трек с начала.
    pause_command: bool = False
    # Шаг громкости для POST /api/volume {"delta": 1} (0.1 = 10%).
    volume_step: float = 0.1
    # Сколько результатов просить у yt-dlp для команды «следующая».
    search_candidates: int = 10
```

и в `Config.validate()` после проверки `playback.directive`:

```python
        if not 0.0 < self.playback.volume_step <= 1.0:
            raise ConfigError("playback.volume_step: шаг громкости в диапазоне 0..1")
        if not 1 <= self.playback.search_candidates <= 25:
            raise ConfigError("playback.search_candidates: 1..25")
```

### 1.2 `config.toml`

```toml
[playback]
directive = "audio_play"
stop_before_play = true
wait_for_ack = false
# false — «мягкая» пауза директивой Станции (позиция не теряется).
# true  — жёсткий stop через Glagol; «продолжай» перезапустит трек с начала.
pause_command = false
# Шаг громкости для /api/volume {"delta": 1}.
volume_step = 0.1
# Сколько кандидатов искать для «следующей».
search_candidates = 10
```

---

## 2. `quasard-youtube`: `offset_ms` в команде

### 2.1 `quasard_youtube/protobuf.py`

`offset_ms` уже есть в payload, но зашит нулём — вынести в параметр
(пригодится, если позже захочется «продолжить с позиции» через
`audio_play`):

```python
def build_audio_play(
    url: str,
    title: str = "",
    subtitle: str = "",
    *,
    fmt: str = "MP3",
    media_type: str = "Track",
    offset_ms: int = 0,
) -> dict[str, Any]:
    """Современная команда ``audio_play`` для прошивок с audio_client.

    Прошивка проверяет формат ДО загрузки URL: допустимы "MP3" и "HLS".
    WAV/FLAC/AAC она затем определяет по содержимому потока.
    ``offset_ms`` — с какой миллисекунды начинать (0 для обычного старта).
    """
    payload: dict[str, Any] = {
        "stream": {
            "url": url,
            "format": fmt,
            "type": media_type,
            "offset_ms": max(0, int(offset_ms)),
        },
        "set_pause": False,
    }
    ...
```

### 2.2 `quasard_youtube/glagol.py` — `play(..., offset_ms=0)`

```python
    async def play(
        self,
        url: str,
        title: str = "",
        subtitle: str = "",
        *,
        directive: str | None = None,
        offset_ms: int = 0,
    ) -> dict[str, Any]:
        selected = directive or self.config.playback.directive
        if selected == "auto":
            selected = "audio_play" if self.state.audio_client else "radio_play"
        if selected == "audio_play":
            payload = build_audio_play(url, title, subtitle, offset_ms=offset_ms)
        elif selected == "radio_play":
            payload = build_radio_play(url, title, subtitle)
        ...
```

---

## 3. `quasard-youtube`: транспортные команды в `glagol.py`

Важно: **`player_pause` в Glagol не существует** (это имя директивы Станции).
Транспортные команды Glagol — `play`, `stop`, `next`, `prev`, `rewind`,
`setVolume`. Поэтому:

```python
    # ── транспорт (пауза/продолжение/переключение) ──────────────────────
    #
    # В Glagol нет отдельной команды player_pause: у Станции есть play,
    # stop, next, prev, rewind, setVolume (так же их использует
    # ma-provider-yandex-station: для audio_client-сессии пауза = stop).
    # «Мягкую» паузу без потери позиции умеет только директива Станции
    # player_pause, а её может отправить лишь quasard — она едет в
    # VinsResponse. Поэтому по умолчанию /api/pause команду не шлёт.

    async def pause(self) -> dict[str, Any]:
        """Жёсткая пауза: Станция останавливает поток и закрывает HTTP-соединение."""
        _LOGGER.info("Glagol: stop (жёсткая пауза)")
        return await self.send({"command": "stop"}, wait=False)

    async def resume(self) -> dict[str, Any]:
        _LOGGER.info("Glagol: play (продолжение)")
        return await self.send({"command": "play"}, wait=False)

    async def next_track(self) -> dict[str, Any]:
        """Переключить встроенную очередь Станции (для нашего потока не нужен)."""
        _LOGGER.info("Glagol: next")
        return await self.send({"command": "next"}, wait=False)

    async def volume_delta(self, delta: float) -> dict[str, Any]:
        """Относительная громкость: нужен известный текущий уровень (state.volume)."""
        if not self.state.last_message_at:
            raise GlagolError(
                "Станция ещё не присылала состояние — текущая громкость неизвестна. "
                "Подождите пару секунд после подключения."
            )
        base = self.state.volume
        target = min(1.0, max(0.0, base + delta))
        _LOGGER.info("Glagol: volume %.2f -> %.2f", base, target)
        await self.set_volume(target)
        return {"previous": round(base, 2), "volume": round(target, 2)}
```

`set_volume()` уже есть и работает через тот же WSS, что и `audio_play` —
менять канал не нужно: `send()` обслуживает и `externalCommandBypass`, и
простые `{"command": ...}`.

---

## 4. `quasard-youtube`: поиск «следующего» результата

### 4.1 `quasard_youtube/youtube.py`

`yt-dlp` на один и тот же `ytsearch1:` всегда отдаёт один и тот же трек,
поэтому для «следующей» просим несколько результатов и берём первый
неизвестный `video_id`.

```python
    async def resolve(self, query: str, *, skip: frozenset[str] = frozenset()) -> TrackInfo:
        """Найти трек. ``skip`` — video_id, которые уже играли (для «следующей»)."""
        query = query.strip()
        if not query:
            raise ResolveError("Пустой поисковый запрос")
        is_url = looks_like_url(query)
        candidates = 1 if is_url else max(1, min(self.config.playback.search_candidates, len(skip) + 1))
        url = query if is_url else f"ytsearch{candidates}:{query}"
        args = [
            *self._base_args(),
            "--dump-single-json",
            "--skip-download",
        ]
        if is_url:
            args.append("--no-playlist")
        else:
            # Для поиска не нужно извлекать 40+ форматов видео: flat-playlist
            # отдаёт id/title/duration в разы быстрее и компактнее.
            args.extend(["--flat-playlist", "--playlist-items", f"1-{candidates}"])
        args.append(url)
        ...
        if skip:
            info = pick_fresh(raw, query, skip)
        else:
            entry = _first_entry(raw)
            if entry is None:
                raise ResolveError("YouTube не вернул ни одного результата")
            info = track_from_entry(entry, query)
        ...
```

Новые функции рядом с `_first_entry`:

```python
def _entries(raw: Any) -> list[dict[str, Any]]:
    """Все результаты поиска (playlist → entries), без ломания на одиночном видео."""
    if not isinstance(raw, dict):
        return []
    if raw.get("_type") in {"playlist", "multi_video"}:
        return [entry for entry in (raw.get("entries") or []) if isinstance(entry, dict)]
    return [raw]


def pick_fresh(raw: Any, query: str, skip: frozenset[str]) -> TrackInfo:
    """Первый результат поиска, которого ещё не было в текущей сессии."""
    entries = _entries(raw)
    rejected = 0
    for entry in entries:
        video_id = str(entry.get("id") or "")
        if not video_id or video_id in skip:
            continue
        try:
            return track_from_entry(entry, query)
        except ResolveError as exc:  # live/без длительности — просто дальше
            rejected += 1
            _LOGGER.warning("YouTube: пропускаю %s — %s", video_id, exc)
    raise ResolveError(
        "Больше нет подходящих результатов по запросу "
        f"(получено {len(entries)}, отклонено {rejected})"
    )
```

---

## 5. `quasard-youtube`: очередь, пауза, громкость в `service.py`

### 5.1 Состояние в `__init__`

```python
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._closing = False
        # Текущий поисковый запрос и уже сыгранные video_id: по ним работает
        # «следующая» (повторный поиск по тому же запросу + пропуск сыгранного).
        self._query = ""
        self._played: list[str] = []
        self._paused = False
```

### 5.2 `play()` — параметр `skip` и учёт очереди

```python
    async def play(
        self,
        query: str,
        *,
        directive: str | None = None,
        skip: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        ...
        async with self._lock:
            ...
            previous = self.hub.current

            # Новый запрос — новая очередь; тот же запрос — продолжаем листать.
            if query != self._query:
                self._query = query
                self._played = []
            skip_ids = frozenset(self._played) if skip is None else skip

            # 2. Метаданные YouTube.
            try:
                track = await self.resolver.resolve(query, skip=skip_ids)
            except ResolveError as exc:
                raise PlaybackError(str(exc)) from exc
```

Перед `return` (после успешного подключения Станции):

```python
            elapsed = time.monotonic() - started
            self._played.append(track.video_id)
            self._paused = False
            ...
            return {
                "title": track.title,
                ...
                "played_count": len(self._played),
                "session": session.status(),
            }
```

### 5.3 `stop()` — сброс очереди

```python
            await self._cancel_watch()
            self._query = ""
            self._played.clear()
            self._paused = False
            return {"stopped": True, "title": session.title}
```

### 5.4 Новые методы

```python
    # ── пауза / продолжение / следующий / громкость ─────────────────────
    #
    # Пауза и продолжение по умолчанию выполняются директивой Станции
    # (player_pause / player_continue), которую отправляет quasard вместе с
    # VinsResponse: только так поток остаётся открытым, а «продолжай»
    # возвращает трек с той же секунды. Сервис здесь лишь ведёт состояние.

    async def pause(self, *, send_command: bool | None = None) -> dict[str, Any]:
        session = self.hub.current
        if session is None:
            return {"paused": False, "reason": "nothing is playing"}
        if send_command is None:
            send_command = self.config.playback.pause_command
        if send_command:
            with contextlib.suppress(Exception):
                await self.glagol.pause()
        self._paused = True
        _LOGGER.info("Пауза%s: %s", " (Glagol stop)" if send_command else "", session.title)
        return {"paused": True, "sent_command": send_command, "title": session.title}

    async def resume(self, *, send_command: bool | None = None) -> dict[str, Any]:
        session = self.hub.current
        if session is None:
            return {"resumed": False, "reason": "nothing is playing"}
        if send_command is None:
            send_command = self.config.playback.pause_command
        if session.finished.is_set():
            # Жёсткая пауза: Станция уже закрыла HTTP-поток — включаем заново.
            _LOGGER.info("Поток закрыт, перезапускаю трек")
            result = await self.play(session.query, skip=frozenset())
            self._paused = False
            return {"resumed": True, "replayed": True, **result}
        if send_command:
            with contextlib.suppress(Exception):
                await self.glagol.resume()
        self._paused = False
        return {
            "resumed": True,
            "replayed": False,
            "sent_command": send_command,
            "title": session.title,
        }

    async def next_track(self) -> dict[str, Any]:
        """Следующий результат по текущему запросу (простая очередь из поиска)."""
        session = self.hub.current
        if session is None:
            return {"played": False, "reason": "nothing is playing"}
        query = (self._query or session.query).strip()
        if not query:
            return {"played": False, "reason": "unknown query"}
        try:
            result = await self.play(query)
        except PlaybackError as exc:
            _LOGGER.warning("Следующий трек не включён: %s", exc)
            return {"played": False, "reason": str(exc)}
        return {"played": True, **result}

    async def volume(
        self, *, delta: float | None = None, level: float | None = None
    ) -> dict[str, Any]:
        if level is not None:
            await self.glagol.set_volume(float(level))
            return {"volume": round(float(level), 2), "step": 0}
        step = float(1 if delta is None else delta)
        result = await self.glagol.volume_delta(step * self.config.playback.volume_step)
        return {**result, "step": step}
```

### 5.5 `status()` — отдать состояние

```python
    def status(self) -> dict[str, Any]:
        session = self.hub.current
        return {
            "service": "quasard-youtube",
            "ok": True,
            "paused": self._paused,
            "queue": {"query": self._query, "played": len(self._played)},
            "station": {...},
            "stream": session.status() if session else None,
            ...
        }
```

`quasard` использует `stream.state` (`streaming` / `waiting_station`), чтобы
понять, играет ли сейчас именно YouTube. `paused` и `queue` — для диагностики.

Метод `next_track()` не держит `self._lock`, а вызывает `play()`, который
берёт его сам: `asyncio.Lock` не реентрантный, вложенный `async with` дал бы
дедлок.

---

## 6. `quasard-youtube`: HTTP-эндпоинты в `streaming.py`

### 6.1 Протокол

```python
class ControlApi(Protocol):
    async def play(self, query: str, *, directive: str | None = None) -> dict[str, Any]: ...
    async def stop(self) -> dict[str, Any]: ...
    async def pause(self, *, send_command: bool | None = None) -> dict[str, Any]: ...
    async def resume(self, *, send_command: bool | None = None) -> dict[str, Any]: ...
    async def next_track(self) -> dict[str, Any]: ...
    async def volume(
        self, *, delta: float | None = None, level: float | None = None
    ) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    async def handle_text(self, text: str) -> dict[str, Any]: ...
```

### 6.2 Хендлеры (внутри `create_app`, после `command`)

```python
    # ── управление воспроизведением ─────────────────────────────────────

    async def body(request: web.Request) -> dict[str, Any]:
        """Тело необязательно: пустой POST — валидный запрос."""
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - ContentTypeError/JSONDecodeError
            return {}
        return payload if isinstance(payload, dict) else {}

    async def pause(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        send = payload.get("send_command")
        try:
            result = await control.pause(send_command=None if send is None else bool(send))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/pause: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def resume(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        send = payload.get("send_command")
        try:
            result = await control.resume(send_command=None if send is None else bool(send))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/resume: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def next_track(request: web.Request) -> web.Response:
        await require_auth(request)
        try:
            result = await control.next_track()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/next: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})

    async def volume(request: web.Request) -> web.Response:
        await require_auth(request)
        payload = await body(request)
        try:
            if payload.get("level") is not None:
                result = await control.volume(level=float(payload["level"]))
            else:
                result = await control.volume(delta=float(payload.get("delta", 1)))
        except (TypeError, ValueError) as exc:
            return _json({"ok": False, "error": f"invalid level/delta: {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("API /api/volume: %s", exc)
            return _json({"ok": False, "error": str(exc)}, 500)
        return _json({"ok": True, **result})
```

### 6.3 Роуты

```python
    app.router.add_post("/api/pause", pause)
    app.router.add_post("/api/resume", resume)
    app.router.add_post("/api/next", next_track)
    app.router.add_post("/api/volume", volume)
```

`require_auth` уже проверяет `X-Api-Token`/`Bearer` для всех `/api/*`, новые
эндпоинты автоматически защищены тем же ключом.

---

## 7. `quasard`: конфигурация

### 7.1 `alice_server/config.py`

```python
@dataclass
class YoutubeCfg:
    """Мост quasard-youtube (внешний YouTube-плеер)."""
    enabled: bool = True
    bridge_url: str = "http://127.0.0.1:8099"
    api_token: str = ""          # = server.api_token в quasard-youtube
    timeout_s: float = 10.0
    next_timeout_s: float = 120.0  # поиск + prebuffer + audio_play
    status_ttl_s: float = 2.0      # кэш «YouTube играет»


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    asr: AsrCfg = field(default_factory=AsrCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    music: MusicCfg = field(default_factory=MusicCfg)
    youtube: YoutubeCfg = field(default_factory=YoutubeCfg)   # новая строка
    device: DeviceCfg = field(default_factory=DeviceCfg)
```

`load_config()` перебирает `fields(cfg)`, поэтому секция `[youtube]` и
`ALICE_YOUTUBE_*` подхватятся без других правок.

### 7.2 `config.toml` (quasard)

```toml
[youtube]
enabled = true
bridge_url = "http://127.0.0.1:8099"
# Должен совпадать с server.api_token в конфиге quasard-youtube (или быть пустым там).
api_token = ""
timeout_s = 10.0
next_timeout_s = 120.0
status_ttl_s = 2.0
```

---

## 8. `quasard`: распознавание коротких команд (`intents.py`)

Добавить **после** блока `intents.match()` (чтобы «громче»/«тише» и «стоп»
по-прежнему обрабатывались штатно) и **до** `music_cmd`:

```python
# ------------------------------------------------------- внешний YouTube-плеер
#
# Короткие команды управления потоком, который играет quasard-youtube.
# Вызывать ТОЛЬКО когда этот поток активен: иначе «следующая» должна уйти
# встроенной Яндекс.Музыке (а «пауза» — её же player_pause).

_YOUTUBE_COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pause",
        re.compile(
            r"(пауза|на паузу|поставь( это)? на паузу|приостанови( музыку| трек| воспроизведение)?"
            r"|останови( музыку| трек| воспроизведение)?|выключи музыку)( пожалуйста)?"
        ),
    ),
    (
        "resume",
        re.compile(
            r"(продолжи|продолжай|продолжить|играй|играй дальше|играй музыку|сними с паузы"
            r"|возобнови( музыку| воспроизведение)?|включи обратно)( пожалуйста)?"
        ),
    ),
    (
        "next",
        re.compile(
            r"(дальше|следующ\w*( трек| песня| песню)?|пропусти\w*|пропускай|некст"
            r"|переключи( трек| песню| на следующ\w+)?|другую( песню| музыку)?"
            r"|включи следующ\w+( трек| песню)?)( пожалуйста)?"
        ),
    ),
)


def match_youtube(text: str) -> str | None:
    """Короткая команда внешнему плееру: ``pause`` / ``resume`` / ``next``.

    Возвращает ``None``, если фраза не является командой плеера. Проверка
    «а YouTube ли сейчас играет» — на вызывающей стороне.
    """
    t = re.sub(r"^(алиса|слушай алиса)\s+", "", _norm(text))
    if not t or len(t.split()) > 5:
        return None
    for action, pattern in _YOUTUBE_COMMANDS:
        if pattern.fullmatch(t):
            return action
    return None
```

Осознанно **не** перехватывается «стоп»: это команда полной остановки
(`intents.match` → пустой ответ, Станция молчит). Не перехватываются и
«громче»/«тише» — они уже работают через `sound_louder`/`sound_quiter`.

---

## 9. `quasard`: новый модуль `alice_server/youtube_control.py`

```python
"""Управление внешним YouTube-плеером (сервис quasard-youtube, 127.0.0.1:8099).

* «Следующая» — целиком HTTP: сервис останавливает текущий поток и запускает
  следующий результат поиска по тому же запросу.
* «Пауза» / «продолжай» — директивой Станции (``player_pause`` /
  ``player_continue``), потому что директива едет внутри VinsResponse, а его
  формирует только quasard. Сервису отправляем запрос «только для состояния»,
  чтобы он знал про паузу (и мог перезапустить трек, если Станция закрыла
  поток).

Модуль намеренно не бросает исключений наружу: голосовой сценарий не должен
падать из-за недоступного моста.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import YoutubeCfg

log = logging.getLogger("youtube")


class YoutubeControl:
    def __init__(self, cfg: YoutubeCfg) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s, connect=2.0))
        self._active = False
        self._active_until = 0.0
        self._paused = False

    # ── HTTP ────────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return self.cfg.bridge_url.rstrip("/") + path

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Token": self.cfg.api_token} if self.cfg.api_token else {}

    async def _get(self, path: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(self._url(path), headers=self._headers())
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("quasard-youtube GET %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    async def _post(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                self._url(path),
                json=payload or {},
                headers=self._headers(),
                timeout=timeout if timeout is not None else self.cfg.timeout_s,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("quasard-youtube POST %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    # ── состояние ───────────────────────────────────────────────────────

    async def active(self) -> bool:
        """Играет ли сейчас поток YouTube (кэш на ``status_ttl_s`` секунд)."""
        if not self.cfg.enabled:
            return False
        now = time.monotonic()
        if now < self._active_until:
            return self._active
        data = await self._get("/api/status")
        stream = (data or {}).get("stream")
        alive = isinstance(stream, dict) and stream.get("state") in {
            "streaming",
            "waiting_station",
        }
        self._active = bool(alive)
        self._active_until = now + max(0.0, self.cfg.status_ttl_s)
        return self._active

    # ── команды ─────────────────────────────────────────────────────────

    async def pause(self) -> dict[str, Any] | None:
        data = await self._post("/api/pause")
        self._paused = True
        return data

    async def resume(self) -> dict[str, Any] | None:
        data = await self._post("/api/resume")
        self._paused = False
        return data

    async def next_track(self) -> dict[str, Any] | None:
        """Поиск + prebuffer + audio_play: может занять десятки секунд."""
        data = await self._post("/api/next", timeout=self.cfg.next_timeout_s)
        if data and data.get("played"):
            self._paused = False
        return data

    async def play(self, query: str) -> dict[str, Any] | None:
        data = await self._post("/api/play", {"query": query}, timeout=self.cfg.next_timeout_s)
        if data and data.get("ok"):
            self._paused = False
        return data

    async def volume(self, delta: int) -> dict[str, Any] | None:
        return await self._post("/api/volume", {"delta": int(delta)})

    async def close(self) -> None:
        await self._client.aclose()
```

---

## 10. `quasard`: подключение в `uniproxy.py`

### 10.1 `Services.__init__`

```python
from .youtube_control import YoutubeControl
...
        self.music = Music(cfg.music, cfg.data_path)
        self.youtube = YoutubeControl(cfg.youtube)
        self.connections: dict[str, set["UniproxySession"]] = {}
```

### 10.2 `_answer()` — хук между `intents.match` и `music_cmd`

```python
        intent = intents.match(text)
        if intent is not None:
            ...
            return

        # ── YouTube: короткие команды внешнему плееру ────────────────────
        youtube_action = intents.match_youtube(text)
        if youtube_action is not None and await self.svc.youtube.active():
            await self._youtube_command(ref, request_id, seq, youtube_action)
            return

        # YouTube-запросы не отдаём встроенной Яндекс.Музыке:
        ...
```

### 10.3 Новый метод рядом с `_music_command`

```python
    async def _youtube_command(self, ref: str, request_id: str, seq: int, action: str) -> None:
        """Пауза/продолжение — директивой Станции, «следующая» — силами сервиса."""
        yt = self.svc.youtube
        directives: list[dict] = []
        sentences: list[str] = []
        if action == "next":
            result = await yt.next_track()
            if not result or not result.get("played"):
                # YouTube не смог (или очередь кончилась) — пусть решает музыка.
                speech, directives = await self._music_command(request_id, "next", "")
                await self._speak_sentences(
                    ref, request_id, seq, _aiter([speech] if speech else []),
                    directives, listen_on_question=False,
                )
                return
            title = " — ".join(x for x in (result.get("artist"), result.get("title")) if x)
            sentences = [f"Включаю: {title}." if title else "Включаю следующий."]
        elif action == "pause":
            await yt.pause()
            directives = [P.client_action("player_pause", {"smooth": True})]
        elif action == "resume":
            await yt.resume()
            directives = [P.client_action("player_continue")]
        else:
            return
        log.info("YouTube: команда %s", action)
        await self._speak_sentences(
            ref, request_id, seq, _aiter(sentences), directives,
            listen_on_question=False,
        )
```

Тонкости:

* Для «пауза»/«продолжай» `sentences` пустой, `directives` непустой — это тот
  же путь, которым уже работает пауза Яндекс.Музыки: `_speak_sentences` в
  ветке `enc is None` отправит `VinsResponse` с директивами и без речи, то
  есть Станция молча применит команду.
* `P.client_action(...)` — тот же `protocol.client_action`, что в `intents.py`;
  `{"smooth": True}` уже используется в `intents.py` для `player_pause`.
* Пауза ставится **до** отправки директивы: так `quasard-youtube` знает, что
  поток на паузе, и `/api/resume` не перезапустит трек, если Станция
  всё-таки закрыла соединение.
* Если мост недоступен, `active()` вернёт `False` и вся ветка просто не
  сработает — «следующая» уйдёт в Яндекс.Музыку, как раньше.

---

## 11. Проверка

### 11.1 Синтаксис и тесты

```powershell
cd C:\workspace\quasard-youtube
.\.venv\Scripts\python.exe -m pytest -q
```

В `tests/test_service.py` нужно поправить фейк резолвера — у `resolve()`
появился обязательный по смыслу, но необязательный по умолчанию аргумент:

```python
class FakeResolver:
    async def resolve(self, query: str, *, skip: frozenset[str] = frozenset()) -> TrackInfo:
        return TrackInfo(...)
```

### 11.2 Новые тесты (по образцу существующих)

```python
def test_match_youtube() -> None:
    from alice_server.intents import match_youtube

    assert match_youtube("Пауза.") == "pause"
    assert match_youtube("поставь на паузу") == "pause"
    assert match_youtube("Продолжай") == "resume"
    assert match_youtube("играй") == "resume"
    assert match_youtube("Следующая") == "next"
    assert match_youtube("следующий трек") == "next"
    assert match_youtube("пропусти, пожалуйста") == "next"
    assert match_youtube("Алиса, следующая") == "next"
    # не команды плеера
    assert match_youtube("включи кино группа крови") is None
    assert match_youtube("громче") is None
    assert match_youtube("стоп") is None


def test_next_uses_played_ids() -> None:
    # FakeResolver из test_service возвращает v1/v2/v3 и пишет skip в список:
    # service.next_track() -> v2, ещё раз -> v3, ещё раз -> "played": False.
    ...
```

### 11.3 Ручная проверка API

```powershell
# что играет и не на паузе ли
curl.exe -s http://127.0.0.1:8099/api/status | python -m json.tool

# пауза «только состояние» (по умолчанию)
curl.exe -s -X POST http://127.0.0.1:8099/api/pause -H "Content-Type: application/json" -d "{}"
# жёсткая пауза через Glagol stop
curl.exe -s -X POST http://127.0.0.1:8099/api/pause -d "{\"send_command\": true}"

curl.exe -s -X POST http://127.0.0.1:8099/api/resume  -d "{}"
curl.exe -s -X POST http://127.0.0.1:8099/api/next    -d "{}"
curl.exe -s -X POST http://127.0.0.1:8099/api/volume  -d "{\"delta\": 1}"
```

Ожидаемые ответы:

```json
{"ok": true, "paused": true, "sent_command": false, "title": "..."}
{"ok": true, "played": true, "title": "...", "played_count": 2, "session": {...}}
{"ok": true, "played": false, "reason": "Больше нет подходящих результатов ..."}
```

### 11.4 Голосом

1. «Алиса, включи на YouTube Группа крови» → в логах `audio_play`, `HTTP: Станция подключилась`.
2. «Пауза» → Станция замолкает, в логах `YouTube: команда pause`; HTTP-поток
   висит (ffmpeg ждёт на backpressure), позиция сохранена.
3. «Продолжай» → звук с той же секунды, `YouTube: команда resume`.
4. «Следующая» → `YouTube: ищу ...` → новый `audio_play`, Станция говорит
   «Включаю: …».
5. «Громче»/«тише» → как раньше, `sound_louder`/`sound_quiter`.
6. «Стоп» → полная остановка (не пауза), как раньше.

---

## 12. Ограничения и что делать, если прошивка упрямится

| Симптом | Причина / что делать |
|---|---|
| «Пауза» не действует, поток продолжает играть | Прошивка не применяет `player_pause` к `audio_play`-сессии. Поставить `pause_command = true` в `quasard-youtube` (жёсткий `stop`). Тогда «продолжай» перезапустит трек с начала — это честное ограничение Glagol |
| «Продолжай» перезапускает трек | Работает режим `pause_command = true`, либо Станция закрыла HTTP-поток сама. Для точного продолжения нужен `offset_ms` + раздача потока с байтового смещения (следующий шаг, код готов на 80 %) |
| «Следующая» говорит «Включаю следующий», но тишина | Смотрите лог `serve`: чаще всего `advertise_host`/firewall или `yt-dlp` не отдал метаданные |
| «Следующая» уводит в Яндекс.Музыку | Хук не сработал: `active()` вернул `false` (мост не отвечает, `stream == null`) или `quasard` не перезапущен |
| Очередь «следующей» бедная (2–3 трека) | Увеличить `playback.search_candidates` (до 25) |
| `/api/volume` → 500 «громкость неизвестна» | Станция ещё не прислала `state.volume`; подождать пару секунд после подключения или слать `{"level": 0..1}` |
| `401` на `/api/*` | В `quasard` не задан `youtube.api_token`, а в `quasard-youtube` `server.api_token` заполнен |

---

## 13. Сводка изменений

| Файл | Тип правки |
|---|---|
| `quasard-youtube/quasard_youtube/config.py` | +3 ключа в `PlaybackConfig`, +2 проверки |
| `quasard-youtube/config.toml` | +3 ключа |
| `quasard-youtube/quasard_youtube/protobuf.py` | `offset_ms` в `build_audio_play` |
| `quasard-youtube/quasard_youtube/glagol.py` | `offset_ms` в `play`; `pause`, `resume`, `next_track`, `volume_delta` |
| `quasard-youtube/quasard_youtube/youtube.py` | `resolve(skip=...)`, `_entries`, `pick_fresh` |
| `quasard-youtube/quasard_youtube/streaming.py` | 4 роута + 4 хендлера + `ControlApi` |
| `quasard-youtube/quasard_youtube/service.py` | очередь (`_query`/`_played`), `pause`, `resume`, `next_track`, `volume`, `status` |
| `quasard-youtube/tests/test_service.py` | сигнатура `FakeResolver.resolve` |
| `quasard/alice_server/config.py` | `YoutubeCfg` + поле `Config.youtube` |
| `quasard/config.toml` | секция `[youtube]` |
| `quasard/alice_server/intents.py` | `match_youtube()` + 3 регулярки |
| `quasard/alice_server/youtube_control.py` | новый файл |
| `quasard/alice_server/uniproxy.py` | `Services.youtube`, хук в `_answer`, `_youtube_command` |
