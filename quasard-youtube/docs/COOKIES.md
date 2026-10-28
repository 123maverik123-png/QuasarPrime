# Куки YouTube для yt-dlp

Сервис умеет отдавать yt-dlp тот же файл куки, что вы экспортировали из
браузера. Тогда YouTube видит не «анонимный бот», а залогиненного
пользователя — это убирает часть ограничений и ошибок авторизации.

Сделано так, что **без файла всё работает как раньше**: нет `cookies.txt` —
нет флага `--cookies`.

---

## 0. Сначала проверьте лимит длительности

Если в логах/ответе API есть:

```text
Трек длится 140 мин — больше лимита 30 мин
```

 — это **не YouTube**, а собственная защита сервиса в `config.toml`:

```toml
[audio]
# 1800 = 30 минут. Для длинных сборников поднимайте значение.
max_duration_seconds = 14400   # 4 часа
```

Куки на этот отказ не влияют вообще. Поднимите лимит и перезапустите сервис.

---

## 1. Установите расширение для экспорта куки

Нужен экспорт именно в формате **Netscape** (`cookies.txt`), потому что yt-dlp
читает файл через `MozillaCookieJar` и без строки-заголовка
`# Netscape HTTP Cookie File` отказывается его загружать.

| Браузер | Расширение |
|---|---|
| Chrome / Edge / YaBrowser / Opera | **Get cookies.txt LOCALLY** (в Chrome Web Store) |
| Firefox | **Get cookies.txt LOCALLY** (в addons.mozilla.org) |

Есть похожие расширения с громкими именами и правами «читать все данные на
всех сайтах». Берите то, что явно пишет «LOCALLY»: оно не отправляет куки на
свой сервер.

> Почему не `--cookies-from-browser`: в Chrome 127+ куки зашифрованы
> App-Bound Encryption (v20), и yt-dlp не может их прочитать с диска. Экспорт
> из самого браузера эту проблему обходит.

## 2. Экспортируйте куки

1. Откройте `https://www.youtube.com` и **войдите в аккаунт** (это важно: без
   логина в файле не будет `SID`/`LOGIN_INFO`, и толку от него не будет).
2. Нажмите иконку расширения → **Export** / **Экспортировать** → сохраните
   файл.
3. Убедитесь, что файл называется `cookies.txt` (не `cookies (1).txt`,
   не `www.youtube.com_cookies.txt` — имя неважно, важно содержимое и путь,
   который вы прописали в конфиге).
4. Первые строки файла должны выглядеть примерно так:

```text
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	1790000000	SID	g.a000...
.youtube.com	TRUE	/	TRUE	1790000000	HSID	AVv...
```

Если вы видите `[{ "domain": ... }]` — это JSON, а не Netscape. yt-dlp такой
файл не прочитает, переэкспортируйте.

## 3. Положите файл рядом с конфигом

```text
C:\workspace\quasard-youtube\
    config.toml
    cookies.txt        <-- сюда
```

## 4. Включите его в `config.toml`

```toml
[youtube]
# Относительный путь ищется рядом с config.toml, затем в текущем каталоге.
# Пусто "" = никогда не передавать куки.
cookies_file = "cookies.txt"
```

Можно указать и абсолютный путь вне проекта, если файл не хочется держать
рядом с кодом:

```toml
cookies_file = "C:/secrets/youtube-cookies.txt"
```

Путь можно задать и переменной окружения — подстановка `${...}` работает:

```toml
cookies_file = "${YTDLP_COOKIES}"
```

### Что происходит с файлом

yt-dlp перезаписывает файл из `--cookies` при каждом запуске (он сохраняет
туда свою куки-банку). Если сессия недействительна, YouTube присылает
«удалить куку», и `SID`/`LOGIN_INFO` из экспорта исчезают — то есть при прямой
передаче yt-dlp мог бы испортить ваш `cookies.txt` при первом же запуске.

Поэтому сервис передаёт yt-dlp **рабочую копию** `cookies.work.txt` рядом с
оригиналом, а сам `cookies.txt` не трогает:

```text
cookies.txt        <- ваш экспорт, только чтение
cookies.work.txt   <- копия для yt-dlp, перезаписывается им как хочет
```

Плюсы: экспорт всегда цел (можно переэкспортировать в любой момент и сравнить),
а переэкспорт подхватывается без перезапуска сервиса — копия обновляется,
когда меняется исходный файл. Удалять `cookies.work.txt` вручную не нужно;
`*.work.txt` тоже в `.gitignore`, потому что содержит те же куки.

## 5. Куки требуют JS-солвер (обязательно, иначе пропадёт звук)

Это главная ловушка, проверено на реальном аккаунте: **с куки yt-dlp идёт в
клиент `web`, которому нужна JS-подпись форматов** (`nsig`/`n`). Без солвера
YouTube возвращает видео вообще без аудио-форматов — в списке остаются только
превью-сториборды:

```text
# без солвера, с куки:
WARNING: [youtube] Signature solving failed: Some formats may be missing.
sb3 mhtml 48x27  ... images storyboard
```

Сервис в этом случае пишет в лог `Аудиопоток пуст`. То есть «включил куки и
стало хуже» — это не баг сервиса, а отсутствие солвера.

Нужны две вещи **в том же Python-окружении, из которого запускается сервис**:

1. Пакет `yt-dlp-ejs` (в `requirements.txt` он приходит вместе с
   `yt-dlp[default]`, поэтому обычная установка зависимостей его уже ставит):

```powershell
cd C:\workspace\quasard-youtube
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# либо точечно:
.\.venv\Scripts\python.exe -m pip install yt-dlp-ejs
```

2. JS-рантайм, включённый явно (yt-dlp по умолчанию ищет только `deno`).
   Проще всего node — он уже есть вместе с Windows-инструментами:

```toml
[youtube]
extra_args = ["--js-runtimes", "node"]
```

Проверка одной командой:

```powershell
.\.venv\Scripts\python.exe -m quasard_youtube doctor
```

```text
[OK]   cookies: C:\workspace\quasard-youtube\cookies.txt (записей 4143, youtube.com 86, авторизация есть)
[OK]   JS-солвер: node: C:\Program Files\nodejs\node.exe
```

Если строки `JS-солвер` нет — сервис работает без куки (см. `--` и `WARN` выше),
а если там `[FAIL]`, включать куки нельзя: сначала поставьте пакет и рантайм.

Быстрая проверка форматов без сервиса:

```powershell
.\.venv\Scripts\python.exe -m yt_dlp --cookies cookies.work.txt `
  --js-runtimes node --list-formats "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

В списке должны быть строки `audio only` (`139`, `140`, `251`). Если видите
только `sb0/sb1/sb2/sb3 ... storyboard` — солвер не работает.

## 6. Перезапустите и проверьте

Перезапуск нужен потому, что флаг собирается один раз при старте сервиса.

```powershell
cd C:\workspace\quasard-youtube
.\.venv\Scripts\python.exe -m quasard_youtube doctor
```

Ожидаемые строки:

```text
[OK]   yt-dlp: 2026.10.26
[OK]   cookies: C:\workspace\quasard-youtube\cookies.txt (записей 41, youtube.com 23, авторизация есть)
```

Возможные варианты вместо `[OK]`:

| Вывод | Значение | Что делать |
|---|---|---|
| `[--] cookies: youtube.cookies_file пуст` | куки отключены в конфиге | вписать `cookies_file = "cookies.txt"` |
| `[WARN] cookies: ... не найден` | файла нет, работаем анонимно | положить файл по указанному пути |
| `[FAIL] cookies: ... нет ни одной куки` | пустой файл или не Netscape | переэкспортировать |
| `[FAIL] cookies: ... нет строки '# Netscape HTTP Cookie File'` | JSON/чужой формат | переэкспортировать расширением |
| `[OK] ... НЕТ SID/LOGIN_INFO` | файл валиден, но YouTube не залогинен | войти в аккаунт и экспортировать заново |
| `[FAIL] JS-солвер: нет пакета yt-dlp-ejs` | с куки не будет аудио | поставить пакет (см. раздел 5) |
| `[FAIL] JS-солвер: ... node найден ...` | рантайм есть, но не включён | `extra_args = ["--js-runtimes", "node"]` |

То же самое подтверждает лог при запуске `serve -v`:

```text
Куки: C:\workspace\quasard-youtube\cookies.txt (записей 41, youtube.com 23, авторизация есть)
```

Если куки не подхватились, в логе будет одна из строк:

```text
Куки: файл 'cookies.txt' не найден — yt-dlp работает без авторизации
Куки: C:\...\cookies.txt игнорируется — нет строки '# Netscape HTTP Cookie File' ...
```

## 7. Проверьте, что yt-dlp реально получает флаг

Посмотреть собранную команду можно так (Windows PowerShell):

```powershell
cd C:\workspace\quasard-youtube
.\.venv\Scripts\python.exe -c @"
from quasard_youtube.config import load_config
from quasard_youtube.youtube import TrackInfo, YoutubeResolver

resolver = YoutubeResolver(load_config())
info = TrackInfo('dQw4w9WgXcQ', 'title', 'artist', 214.0,
                 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
print(' '.join(resolver.stream_args(info)))
"@
```

В выводе должны появиться `--cookies` и путь к рабочей копии
(`cookies.work.txt`), а не к вашему экспорту — так и задумано.

Плюс живая проверка стрима (без Станции):

```powershell
.\.venv\Scripts\python.exe -m yt_dlp --cookies .\cookies.txt -f bestaudio -o - `
  "https://www.youtube.com/watch?v=kjeREJjXyzM" | Measure-Object -Property Length -Sum
```

Если yt-dlp с куки выдаёт больше байт за то же время и не пишет
`Sign in to confirm you're not a bot` — куки работают.

## 8. Безопасность — прочитайте

- `cookies.txt` — это **ключ от аккаунта Google**: кто получил файл, тот может
  зайти в аккаунт без пароля и двухфакторки.
- Файл уже добавлен в `.gitignore` (`cookies.txt`, `*cookies*.txt`,
  `*.work.txt`, `cookies/`, `secrets/`). Не обходите это правило и не
  прикладывайте файл к переписке или в issue.
- Рабочая копия `cookies.work.txt` — тоже секрет: в ней те же куки.
- Экспорт «всех сайтов» содержит лишнее: `yandex.ru` (вместе с `Session_id`
  для quasard), соцсети, трекеры. yt-dlp они не нужны — он отправляет только
  куки домена запроса. Можно оставить только YouTube-домены отдельным файлом
  (исходник не меняется):

```powershell
.\.venv\Scripts\python.exe scripts\trim_cookies.py
# cookies.txt -> cookies.youtube.txt: ~200 куки вместо ~4100
```

  Затем укажите в конфиге `cookies_file = "cookies.youtube.txt"`. Если
  переэкспортируете куки — запустите обрезку ещё раз, иначе файл останется
  старым.
- Не пересылайте файл боту, «чтобы он проверил» — проверка делается локально.
- Для таких экспортов лучше отдельный Google-аккаунт: если Google увидит
  подозрительную активность, он может разлогинить сессию (и иногда пометить
  аккаунт). Основной аккаунт для этого использовать не стоит.
- Куки живут ограниченное время (YouTube обновляет `SID`/`__Secure-*`
  периодически). Появились ошибки авторизации снова — переэкспортируйте файл.
- Права на файл: держите его только для своего пользователя.

```powershell
# Windows: убрать наследование и оставить доступ только текущему пользователю
icacls C:\workspace\quasard-youtube\cookies.txt /inheritance:r `
  /grant:r "$($env:USERNAME):(R,W)"
```

## 9. Что куки лечат, а что нет

| Симптом | Куки помогают? |
|---|---|
| `Sign in to confirm you're not a bot` | да, в большинстве случаев |
| `This video is age-restricted` / просит войти | да |
| `Private video` / `members-only` | только если аккаунт имеет доступ |
| «больше лимита N мин» | **нет** — это `audio.max_duration_seconds` |
| Поток обрывается на середине, `403` от `googlevideo.com` | иногда; чаще помогает свежий yt-dlp и `--extractor-args` (см. ниже) |
| `Аудиопоток пуст`, «только storyboard» в форматах | нужен JS-солвер (раздел 5), куки тут только мешают |
| Станция не подключается к потоку | нет — это firewall/`advertise_host` |

Для обрыва на середине сначала обновите yt-dlp (`pip install -U yt-dlp`):
YouTube регулярно меняет требования к PO Token, и старые версии перестают
тянуть длинные потоки. Если не помогло, можно зафиксировать клиента:

```toml
[youtube]
extra_args = ["--extractor-args", "youtube:player_client=web_safari,default"]
```

`extra_args` полностью перекрывает ручную передачу куки: если там уже есть
`--cookies` или `--cookies-from-browser`, `cookies_file` не добавляется, чтобы
не было двух флагов.
