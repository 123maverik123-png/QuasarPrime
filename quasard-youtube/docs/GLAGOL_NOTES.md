# Glagol: что именно отправляется на Станцию

Краткая выжимка из рабочей реализации и двух открытых проектов. Полезно,
если придётся отлаживать команду вручную.

## Подключение

- Адрес: `wss://<station-ip>:1961`
- TLS: самоподписанный сертификат, проверка отключена (`ssl=False`).
- Токен: локальный quasard, `http://127.0.0.1:80/glagol/token` (подпись
  проверяется тем же quasard через `/glagol/check_token`). Внешний
  `https://quasar.yandex.ru/glagol/token` для этой схемы не подходит.
- Токен — JWT; кэшируем до `exp - 60 c`.
- Каждое сообщение:

```json
{
  "conversationToken": "<glagol token>",
  "id": "<uuid>",
  "sentTime": 1730000000000,
  "payload": { "command": "..." }
}
```

## `radio_play`

```json
{
  "command": "externalCommandBypass",
  "data": "<base64 protobuf>"
}
```

Protobuf — два length-delimited поля:

```
1: "radio_play"
2: "{\"streamUrl\":\"http://192.168.50.95:8099/stream/xxx.mp3\",\"force_restart_player\":true,\"title\":\"...\",\"subtitle\":\"...\"}"
```

`force_restart_player: true` важен при переключении трека.

## `audio_play` (рабочий путь для 0.369/plum)

```json
{
  "stream": {
    "url": "http://192.168.50.95:8099/stream/xxx.mp3",
    "format": "MP3",
    "type": "Track",
    "offset_ms": 0
  },
  "set_pause": false,
  "metadata": {"title": "...", "subtitle": "..."}
}
```

Формат проверяется до загрузки URL. Допустимы `MP3` и `HLS`; WAV/FLAC/AAC
прошивка определяет уже по содержимому. На прошивке 0.369 `radio_play`
отвечает `SUCCESS`, но поток не запускает.

## HTTP-поток

Станция требует:

- `Content-Type: audio/mpeg`;
- известный `Content-Length`;
- конечный поток; chunked/бесконечный не принимается;
- HTTP, не HTTPS, на локальном адресе;
- app-level ping `{"payload":{"command":"ping"}}` каждые ~5 секунд:
  прошивка закрывает WSS через ~10 секунд без него.

Ответ, который отдаёт этот проект:

```http
HTTP/1.1 200 OK
Content-Type: audio/mpeg
Content-Length: 5160000
Accept-Ranges: none
Cache-Control: no-store
Connection: close
```

`Content-Length` считается как `(duration + 2 c) * 24000` для 192 кбит/с.
Хвост при нехватке добивается MP3-тишиной, излишек обрезается.

## Почему нельзя просто отдать поток из `yt-dlp`

`yt-dlp` не знает итоговый размер MP3 до конца кодирования, а Станция
отвергает chunked. Поэтому используется связка:

```
yt-dlp (metadata) -> duration
yt-dlp (audio to stdout) -> ffmpeg (CBR MP3 to stdout) -> buffer -> HTTP
```

Пока Станция не подключилась, buffer заполняется до prebuffer и producer
засыпает; это одновременно prebuffer и backpressure для ffmpeg.

## Ссылки

- https://github.com/volodarskij/alice-spotify-bridge/blob/master/bridge/SPEC.md
- https://github.com/volodarskij/alice-spotify-bridge/blob/master/bridge/src/glagol.rs
- https://github.com/trudenboy/ma-provider-yandex-station/blob/dev/provider/player.py
- https://github.com/trudenboy/ma-provider-yandex-station/blob/dev/provider/glagol.py
