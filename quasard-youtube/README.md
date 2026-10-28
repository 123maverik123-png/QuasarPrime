# quasard-youtube

Мост **YouTube → локальный HTTP MP3 → Яндекс Станция** для `quasard`.
Пользователь говорит «Алиса, включи на YouTube …», LLM возвращает
`PLAY_YOUTUBE: <запрос>`, а этот сервис ищет трек, кодирует его в MP3 на лету
и отправляет Станции команду `audio_play` (или `radio_play` для старых прошивок)
через локальный Glagol WebSocket.

```
quasard (ASR → DeepSeek) ── PLAY_YOUTUBE: query ──► POST /api/command
                                                        │
                                      yt-dlp search ────┤
                                                        ▼
                                   yt-dlp ──► ffmpeg ──► StreamBuffer
                                                        │
                        HTTP GET /stream/<token>.mp3 ◄──┤
                                   Станция ◄────────────┘
                                                        ▲
                   Glagol WSS :1961, audio_play(url) ─────┘
```

> Важно: это не DLNA и не «открыть ссылку на YouTube» — Станция не умеет
> играть YouTube-ссылки напрямую. Она получает **конечный HTTP-поток MP3**
> с известным `Content-Length` и играет его как внешний источник.

## Что уже реализовано

- `yt-dlp`-поиск `ytsearch1:` без скачивания файла целиком;
- `yt-dlp | ffmpeg` → MP3 CBR 192 кбит/с, 44.1 кГц, стерео;
- локальный HTTP-сервер на `aiohttp` (только HTTP, без HTTPS);
- точный `Content-Length` и добивка тишиной, без chunked-потока;
- Glagol WebSocket `wss://<station>:1961` с самоподписанным сертификатом;
- локальный токен quasard `/glagol/token` + app-level ping каждые 5 с
  (прошивка закрывает WSS через ~10 с без него);
- `audio_play` (рабочий путь для 0.369/plum) и `radio_play` (старые прошивки);
- API `/api/command`, `/api/play`, `/api/stop`, `/api/pause`, `/api/resume`,
  `/api/next`, `/api/seek`, `/api/volume`, `/api/status`;
- Python-хук для `quasard`, системный промпт и CLI-диагностика.

## Быстрый старт (Windows)

### 1. Зависимости

Нужны Python 3.11+ и `ffmpeg` в `PATH`.

```powershell
winget install --id Gyan.FFmpeg -e
cd C:\workspace\quasard-youtube
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

`yt-dlp` ставится как Python-зависимость. Если удобнее отдельный `.exe`,
укажите его в `youtube.yt_dlp_path`.

### 2. Конфиг

```powershell
Copy-Item config.example.toml config.toml
```

Минимум, что нужно заполнить:

```toml
[station]
host = "192.168.50.104"          # IP Станции
device_id = ""                    # можно оставить пустым — определится через API
platform = "plum"                 # Lite 2 = plum, Mini 2 = yandexmidi
session_id = "${YANDEX_SESSION_ID}"
# Токен берём у локального quasard: станция проверяет его тем же ключом.
token_url = "http://127.0.0.1:80/glagol/token"

[server]
advertise_host = "192.168.50.95" # IP ПК, который видит Станция
```

`Session_id` — cookie домена `yandex.ru`. Не коммитьте его в git; лучше
задать переменной окружения:

```powershell
$env:YANDEX_SESSION_ID = "..."
```

Если `device_id` пуст, при первом запуске сервис попробует получить список
устройств через `iot.quasar.yandex.ru` и выбрать колонку по `station.host`
или `station.device_name`.

#### Куки YouTube (необязательно)

Если YouTube просит «Sign in to confirm you're not a bot», требует вход для
возрастных видео или аккаунт-специфичных треков — положите экспорт куки рядом
с конфигом. Сервис сам передаст его в `--cookies` (через рабочую копию, чтобы
yt-dlp не перезаписал ваш экспорт):

```toml
[youtube]
cookies_file = "cookies.txt"   # рядом с config.toml; "" = никогда не передавать
```

Без файла yt-dlp работает анонимно, как раньше. Как получить `cookies.txt`,
что делать с правами и почему файл нельзя коммитить — [docs/COOKIES.md](docs/COOKIES.md).
Проверка: `python -m quasard_youtube doctor` покажет строки `cookies` и
`JS-солвер`.

> С куки обязательно нужны `pip install yt-dlp-ejs` и JS-рантайм
> (`extra_args = ["--js-runtimes", "node"]`): авторизованная сессия ходит в
> клиент `web`, которому нужна подпись форматов. Без солвера YouTube отдаёт
> видео без аудио, и в логе будет «Аудиопоток пуст». `doctor` проверяет это
> отдельной строкой.

### 3. Firewall

Станция должна открывать `http://<ПК>:8099/...`. Разрешите входящий TCP 8099
в приватном профиле (PowerShell от администратора):

```powershell
New-NetFirewallRule -DisplayName "quasard-youtube 8099" `
  -Direction Inbound -Protocol TCP -LocalPort 8099 `
  -Action Allow -Profile Private
```

### 4. Проверка

```powershell
python -m quasard_youtube doctor --query "Кино Группа крови"
```

Доктор проверит ffmpeg, yt-dlp, IP, TCP 1961, Session_id, Glagol-токен,
WSS-подключение и (по желанию) поиск YouTube. Секреты не печатаются.

### 5. Запуск

```powershell
python -m quasard_youtube serve -v
```

В другом окне:

```powershell
python -m quasard_youtube play "Кино Группа крови"
```

Или через API:

```powershell
curl.exe -X POST http://127.0.0.1:8099/api/play `
  -H "Content-Type: application/json" `
  -d "{\"query\":\"Кино Группа крови\"}"
```

## Интеграция с quasard

### Вариант A: Python-хук (рекомендуется)

В `alice_server` после получения ответа LLM и **до** TTS:

```python
from quasard_youtube.quasard import process_reply_async

raw_reply = await llm_chat(messages)          # ответ DeepSeek
spoken = await process_reply_async(
    raw_reply,
    bridge_url="http://127.0.0.1:8099",
    timeout=90,
)
await tts.speak(spoken)                        # F5-TTS
```

Хук сам вырезает `PLAY_YOUTUBE: ...`, вызывает bridge и возвращает только
текст для озвучки. Если bridge недоступен, команда не произносится вслух,
а возвращается короткая фраза об ошибке.

### Вариант B: HTTP без импорта

Любой код, умеющий HTTP, может отправить полный ответ LLM:

```http
POST /api/command
Content-Type: application/json

{"text": "PLAY_YOUTUBE: Кино Группа крови"}
```

Ответ:

```json
{
  "ok": true,
  "matched": true,
  "spoken_text": "Включаю.",
  "query": "Кино Группа крови",
  "playback": {"title": "...", "artist": "...", "stream_url": "http://..."}
}
```

### Системный промпт

Текст лежит в [`prompts/quasard_system_prompt.txt`](prompts/quasard_system_prompt.txt).
Вставьте его в системный промпт DeepSeek в `config.toml` quasard (ключ
зависит от вашей версии; обычно это `system_prompt`). Главное правило:

```
Если просят включить музыку с YouTube — верни ровно одну строку:
PLAY_YOUTUBE: <поисковый запрос>
```

Без этого правила LLM может ответить «Сейчас включу» и команда не дойдёт
до моста.

## API

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/play` | `{"query": "...", "directive": "audio_play"}` |
| `POST` | `/api/command` | разобрать ответ LLM целиком |
| `POST` | `/api/stop` | остановить поток и отправить `stop` |
| `POST` | `/api/pause` | пауза на Станции + запоминание позиции |
| `POST` | `/api/resume` | продолжить с той же секунды |
| `POST` | `/api/next` | следующий результат по тому же запросу |
| `POST` | `/api/seek` | `{"position_ms": 180000}` — перемотка текущего трека |
| `POST` | `/api/volume` | `{"level": 0..1}` или `{"delta": 1}` |
| `GET` | `/api/status` | состояние станции/потока |
| `GET` | `/healthz` | health-check |
| `GET` | `/stream/<token>.mp3` | MP3 для Станции (только этот URL) |

Перемотка пересобирает поток: `ffmpeg -ss` кодирует трек с нужной секунды,
`Content-Length` считается от остатка, а Станции уходит `audio_play` с
`offset_ms = 0` — она понимает offset как позицию **внутри потока**, поэтому
иначе перескочила бы за конец короткого потока и сразу его завершила.
Пауза/продолжение после перемотки отправляют Станции позицию внутри потока
(`played_ms - start_ms`), а панели `status.played_ms` всегда отдаёт абсолютную.

### Пауза: только жёсткая, и почему позиция берётся у Станции

Мягкой паузы для `audio_play` у прошивки нет: кольцо на колонке и Glagol
`{"command":"stop"}` дают одно и то же — `audio_stop`, `player_finish` и
стирание сессии проигрывателя (видно в `maind.log` станции). Поэтому
`/api/pause` по умолчанию (`playback.pause_command = true`) отправляет `stop`,
без него музыка продолжала играть. «Продолжай» возвращает трек с
зафиксированной секунды: поток переподключается с `offset_ms`.

Позицию и факт паузы мост берёт из состояния Станции, а не из байтов: она раз
в секунду присылает по Glagol `state.playing` и `playerState.progress`
(позиция внутри отданного потока). Это важно в обе стороны:

* пауза кольцом **не закрывает** HTTP-поток — байты просто перестают
  забираться, поэтому раньше панель продолжала считать время; теперь
  `playing=false` дольше 1.5 с превращается в `state=paused` с
  `paused_by=station`;
* `progress` точнее наших часов (Станция качает поток вперёд на минуты);
  абсолютная позиция = `start_ms + progress`.

Если задан `server.api_token`, `/api/*` требует `X-Api-Token` или
`Authorization: Bearer ...`. Stream-URL защищён случайным токеном и живёт
только на время трека.

## Как обходится ограничение Станции

Станция **не принимает chunked / бесконечный HTTP-поток**. Поэтому:

1. `yt-dlp --dump-single-json` заранее узнаёт `duration`;
2. `Content-Length = (duration + padding) * bitrate/8`;
3. MP3 кодируется в CBR 192 кбит/с = 24 000 байт/с;
4. если источник закончился раньше — хвост добивается MP3-тишиной;
5. если позже — поток обрезается по `Content-Length`;
6. соединение закрывается ровно после обещанных байт.

До отправки команды сервис ждёт prebuffer (по умолчанию 96 КиБ ≈ 4 с
аудио), чтобы Станция не получила пустой ответ. `yt-dlp` и `ffmpeg` работают
через pipe, на диск ничего не скачивается.

### `radio_play` vs `audio_play`

- `audio_play` — рабочий путь для Station Lite 2 (прошивка 0.369, `plum`):
  используется по умолчанию. Станция проверяет `format: "MP3"` до загрузки
  URL и затем сама определяет WAV/FLAC/AAC по содержимому.
- `radio_play` — старая команда. На проверенной прошивке 0.369 она отвечает
  `SUCCESS`, но **не запускает поток**. Оставлена для совместимости.
- `directive = "auto"` выберет `audio_play`, если по WSS пришло
  `supported_features: audio_client`, иначе `radio_play`.

## Структура проекта

```
quasard_youtube/
  command.py      # PLAY_YOUTUBE: ...
  protobuf.py     # externalCommandBypass (radio_play/audio_play)
  glagol.py       # WSS :1961, token, device discovery
  youtube.py      # yt-dlp search/metadata (+ --cookies)
  cookies.py      # проверка cookies.txt (Netscape, авторизация)
  audio.py        # yt-dlp -> ffmpeg -> MP3, silence
  streaming.py    # HTTP-сервер, Content-Length, буфер
  service.py      # оркестрация
  quasard.py      # хук для alice_server
  __main__.py     # CLI: serve/play/doctor/parse
prompts/          # системный промпт для DeepSeek
docs/             # заметки по Glagol, патчу quasard и куки (COOKIES.md)
tests/            # unit/integration-тесты
```

## Тесты

```powershell
python -m pytest -q
```

Покрыты protobuf-формат `radio_play`/`audio_play`, разбор команд, расчёт
`Content-Length`, точная отдача тела без chunked, `HEAD`, добивка тишиной,
конфиг и хуки. Реальное воспроизведение на железе требует валидного
`Session_id` и доступной Станции — в этом окружении его нет.

## Ограничения

- В мосте нет собственной очереди: один трек за команду, `next` ищет следующий
  результат по тому же запросу, `seek` пересобирает поток с нужной секунды.
  Очередь и история живут в панели (static/yt-player.js).
- Прямые трансляции YouTube отклоняются: для них нельзя рассчитать
  `Content-Length`.
- Треки длиннее `audio.max_duration_seconds` (30 мин по умолчанию)
  отклоняются. Это единственный лимит длительности в проекте: сообщение
  «больше лимита 30 мин» — про него, а не про YouTube. Куки на него не влияют;
  для длинных сборников поднимите значение (например, `14400`).
- Старт занимает несколько секунд: поиск + prebuffer + буферизация Станции
  (~2–3 с).
- `Content-Length` считается по метаданным. Если YouTube врёт о длительности,
  хвост будет обрезан или добит тишиной; для обычных клипов это незаметно.
- Локальный API по умолчанию без пароля. Не публикуйте порт 8099 в интернет;
  для домашней сети задайте `server.api_token`.
- Соблюдайте авторские права и условия YouTube. Инструмент для личного
  использования.

## Диагностика

| Симптом | Что проверить |
|---|---|
| `401` от token endpoint | истёк `Session_id`; обновите cookie |
| `Token endpoint вернул HTML` | ПК тоже перенаправляет `quasar.yandex.ru` на локальный DNS; исключите ПК из правила |
| `Station closed WebSocket code=4000 Invalid token` | неверный `platform` (Lite 2 — `plum`) или станция не смогла проверить токен через локальный quasard |
| В логах станции `Can't check token ... backend error` | quasard на 80/443 завис/не отвечает; перезапустите его и проверьте `/glagol/check_token` |
| WSS закрывается через ~10 с (`no pong for long`) | не хватает app-level ping; в этой версии он уже отправляется каждые 5 с |
| `Станция не подключилась к локальному HTTP-потоку` | firewall, `server.advertise_host`, доступность `http://IP:8099/healthz` с другого устройства |
| `410 stream is not active` | Станция запросила старый URL; повторите команду |
| `409 stream already being consumed` | Станция открыла второе соединение; перезапустите трек |
| `yt-dlp ... Sign in to confirm you're not a bot` | нужны куки: `youtube.cookies_file` → [docs/COOKIES.md](docs/COOKIES.md), проверка `doctor` |
| `Трек длится N мин — больше лимита 30 мин` | это не YouTube, а `audio.max_duration_seconds` в `config.toml` |
| `Куки: ... игнорируется` в логе | файл не в Netscape-формате; переэкспортируйте (см. `doctor`) |
| `ffmpeg not found` | `where.exe ffmpeg`, `youtube.ffmpeg_path` |
| Станция молчит, но HTTP-запросы идут | попробуйте `playback.directive = "audio_play"`; проверьте `-v` логи Glagol |

## Источники протокола

- [alice-spotify-bridge — bridge/SPEC.md](https://github.com/volodarskij/alice-spotify-bridge/blob/master/bridge/SPEC.md):
  Glagol WSS, `radio_play`, protobuf, Content-Length, запрет chunked.
- [alice-spotify-bridge — bridge/src/glagol.rs](https://github.com/volodarskij/alice-spotify-bridge/blob/master/bridge/src/glagol.rs):
  точная структура `externalCommandBypass`.
- [ma-provider-yandex-station](https://github.com/trudenboy/ma-provider-yandex-station):
  `audio_play`, `audio_client`, WAV/MP3 и поведение прошивки.
