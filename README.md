# QuasarPrime

**Оптимус Прайм в Яндекс Станции.** Свой локальный бэкенд вместо облака Алисы: колонка
активируется, слушает и отвечает через твой ПК и твои модели — распознавание речи **GigaAM**,
ответы **DeepSeek** (или любой OpenAI-совместимой LLM), озвучка **клоном голоса** на
**F5-TTS** и музыка из **YouTube** через Glagol-канал самой станции.

```
        «Алиса, привет»
              │
   Яндекс Станция (прошивка 3.x, root)
              │  DNS доменов Яндекса → ваш ПК, HTTPS 443 с вашим CA
              ▼
   ┌──────────────────────────── QuasarPrime ────────────────────────────┐
   │  quasard (80/443)  — uniproxy, активация, ASR, LLM, TTS, музыка     │
   │  f5_server (8777)  — синтез речи клоном голоса                      │
   │  quasard-youtube (8099) — YouTube → MP3-поток → Glagol-плеер        │
   └─────────────────────────────────────────────────────────────────────┘
              │  Ogg/Opus с голосом Оптимуса
              ▼
        Станция говорит
```

> Неофициальный проект, не связан с Яндексом. Только для собственных устройств
> и в исследовательских целях.

---

## Содержание

1. [Что это даёт](#что-это-даёт)
2. [Как это работает](#как-это-работает)
3. [Требования](#требования)
4. [Быстрый старт](#быстрый-старт)
5. [Структура репозитория](#структура-репозитория)
6. [Установка подробно](#установка-подробно)
7. [Как направить станцию на свой ПК (DNS)](#как-направить-станцию-на-свой-пк-dns)
8. [Wi-Fi: закрепить станцию за одной точкой доступа](#wi-fi-закрепить-станцию-за-одной-точкой-доступа)
9. [Запуск: `launcher.py`](#запуск-launcherpy)
10. [Свой голос](#свой-голос)
11. [Музыка и YouTube-мост](#музыка-и-youtube-мост)
12. [Диагностика](#диагностика)
13. [Риски и дисклеймер](#риски-и-дисклеймер)
14. [Лицензия и благодарности](#лицензия-и-благодарности)

---

## Что это даёт

- **Свой характер.** Системный промпт задаёт личность: по умолчанию — Оптимус Прайм,
  который отвечает кратко, по-русски и отправляет любую музыку в YouTube.
- **Свой голос.** Клон голоса по 10–15-секундному образцу (F5-TTS), а не голос Алисы.
- **Свой мозг.** Любая OpenAI-совместимая модель: DeepSeek, Groq, OpenRouter, локальная
  Ollama/vLLM. Ответ стримится по фразам — станция начинает говорить почти сразу.
- **Музыка из YouTube** играет сама станция: мост отдаёт MP3-поток, а команды
  play/pause/next идут по Glagol-каналу.
- **Приватность.** Голос и запросы обрабатываются локально; наружу уходит только текст
  в LLM-API (и то не обязательно — можно локальную модель).
- **Колонка не обновляется** на прошивку, которая всё сломает.

## Как это работает

```
микрофон станции (Ogg/Opus)
   → Silero VAD: где кончилась фраза
   → GigaAM v3: речь в текст
   → LLM: ответ стримится по фразам
   → F5-TTS: фраза → голос Оптимуса
   → Ogg/Opus обратно на станцию
```

Параллельно сервер отвечает на служебные запросы станции: активация (`oauth`,
`register`, `get_sync_info`, `device_list`), конфигурация, метрики, keep-alive.

Два протокола uniproxy поддерживаются автоматически: **JSON** (прошивки ~1.56) и
**бинарный protobuf `AAPI`** (прошивки 3.x) — формат определяется по первому кадру станции.

## Требования

| Что | Зачем |
|---|---|
| **Python 3.12** | проект писался и проверялся на нём |
| **NVIDIA GPU** (CUDA), ~5 ГБ VRAM | GigaAM ≈1 ГБ + F5-TTS ≈3.4 ГБ. На CPU работает, но медленно |
| **Root / adb-доступ к станции** | установить свой CA, поправить `/etc/hosts`, поставить Wi-Fi-скрипт |
| **Ключ DeepSeek API** | <https://platform.deepseek.com/api_keys> (или любой OpenAI-совместимый API) |
| **ПК с постоянным IP в той же сети** | станция ходит на него по 80/443 |
| **ffmpeg в `PATH`** | YouTube-мост и декодирование аудио |
| Windows 10/11 или Linux | проверялось на Windows 11 + RTX 5080 |
| Прошивка станции 3.x | на ней протестировано; для 1.56 нужен JSON-протокол |

Порты **80** и **443** на ПК должны быть свободны (на Linux — запуск под `sudo`).

## Быстрый старт

```bash
# 1. код и окружение
git clone https://github.com/<you>/QuasarPrime.git
cd QuasarPrime
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate

# 2. PyTorch под свою CUDA (для RTX 50xx — cu128)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# 3. остальные зависимости
pip install -r requirements.txt

# 4. конфиг и ключ
copy config.example.toml config.toml        # Linux: cp
#   вписать [llm] api_key

# 5. сертификаты под IP этого ПК
python scripts/gen_certs.py --ip 192.168.50.95

# 6. мост YouTube
copy quasard-youtube\config.example.toml quasard-youtube\config.toml
#   вписать station.host = IP станции, session_id = cookie с yandex.ru

# 7. запуск всех трёх сервисов
python launcher.py
```

Перед первым запуском обязательно пройди разделы
[DNS](#как-направить-станцию-на-свой-пк-dns) и
[Wi-Fi](#wi-fi-закрепить-станцию-за-одной-точкой-доступа) — без них станция
не дойдёт до сервера или будет рвать соединение каждые полминуты.

## Структура репозитория

```
QuasarPrime/
├── launcher.py              запуск всех трёх сервисов + цветные логи + Ctrl+C
├── README.md                этот файл
├── requirements.txt         зависимости Python
├── config.example.toml      шаблон конфигурации (копируется в config.toml)
├── voice/
│   ├── optimus_24k.wav      образец голоса (24 кГц, ~10 с)
│   └── optimus.txt          текст образца (F5 нужен именно он)
├── scripts/
│   ├── wifi-pin-xiaomi.sh   закрепление станции за одной точкой доступа
│   └── gen_certs.py         свой CA + сертификат на домены Яндекса
├── quasard/                 «мозг»: сервер uniproxy, ASR, LLM, TTS, музыка
│   ├── uniproxy.py          голосовой цикл, стриминг, JSON + protobuf
│   ├── aliceapi.py/.desc    protobuf-протокол прошивок 3.x
│   ├── http_api.py          активация и конфигурация станции
│   ├── asr.py               GigaAM + Silero VAD
│   ├── llm.py               стриминг ответа по фразам
│   ├── tts.py               синтез (F5 / Edge / OpenAI)
│   ├── music.py             Яндекс.Музыка
│   ├── youtube_control.py   управление YouTube-мостом
│   └── assets/              Silero VAD (onnx) + лицензия
├── f5_server/
│   └── f5_server.py         HTTP-сервис F5-TTS (клон голоса), порт 8777
├── quasard-youtube/         мост YouTube → Glagol-плеер станции
│   ├── quasard_youtube/     пакет моста (glagol, streaming, service, ...)
│   ├── config.example.toml  шаблон конфигурации моста
│   └── README.md            отдельная инструкция по мосту
└── docs/
    ├── TRAFFIC_CAPTURE.md   как снять трафик станции и собрать протокол
    └── ...
```

`quasard-youtube/docs/` содержит разбор Glagol-канала и голосовых команд.

## Установка подробно

### 1. Код и виртуальное окружение

```bash
python -m venv .venv
.venv\Scripts\activate
python -V          # должно быть 3.12.x
```

### 2. PyTorch под вашу видеокарту

Ставьте **до** `requirements.txt`, иначе pip притянет CPU-сборку:

| Видеокарта | Команда |
|---|---|
| RTX 50xx (Blackwell) | `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128` |
| RTX 20xx–40xx | `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124` |
| Только CPU | `pip install torch torchaudio` |

Проверка:

```bash
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 3. Зависимости

```bash
pip install -r requirements.txt
```

Первая загрузка весов: GigaAM (~1 ГБ) и F5-TTS (~1.5 ГБ) скачиваются с HuggingFace
при старте. Дальше они берутся из кэша `~/.cache/huggingface`.

### 4. Конфигурация

```bash
copy config.example.toml config.toml      # Linux: cp config.example.toml config.toml
```

Минимум, что нужно поправить:

```toml
[llm]
api_key = "sk-..."        # ключ DeepSeek

[tts]
engine = "f5"             # или "edge", если F5 не нужен

[server]
https_port = 443
http_port = 80
```

Любой ключ можно задать переменной окружения: `ALICE_LLM_API_KEY`, `ALICE_SERVER_HTTPS_PORT` и т.д.

### 5. Сертификаты

```bash
python scripts/gen_certs.py --ip 192.168.50.95
```

Создаст `certs/`:

| Файл | Что это |
|---|---|
| `ca.crt` | ваш корневой CA — его надо установить на станцию |
| `ca.key` | приватный ключ CA — **никому не отдавать, в git не коммитить** |
| `server.crt` | сертификат на домены Яндекса + IP вашего ПК |
| `server.key` | приватный ключ сервера |

Проверить, что в сертификат попал ваш IP:

```bash
openssl x509 -in certs/server.crt -text -noout | findstr IPAddress
```

**Установка CA на станцию** (нужен root/adb):

```bash
adb connect 192.168.50.104:5555

# 1) бэкап системного хранилища
adb shell "cp /system/vendor/quasar/certs/ca-certificates.crt \
              /system/vendor/quasar/certs/ca-certificates.crt.orig"

# 2) дописать наш CA (файл ca.crt уже в PEM)
adb push certs/ca.crt /data/local/tmp/ca.crt
adb shell "mount -o remount,rw / && \
           cat /data/local/tmp/ca.crt >> /system/vendor/quasar/certs/ca-certificates.crt && \
           sync"

# 3) проверка: в конце бандла должен быть ваш CA
adb shell "tail -c 400 /system/vendor/quasar/certs/ca-certificates.crt"
```

После этого перезагрузите станцию (`adb shell reboot`) — демоны читают бандл при старте.

> На прошивке станции это **Linux**, а не Android: путь `/system/etc/security/cacerts`
> (Android-style) здесь не работает. Бандл, которому доверяют демоны, задаётся переменной
> `CA_CERTIFICATES=/system/vendor/quasar/ca-certificates.crt` в `launch.json`.

## Как направить станцию на свой ПК (DNS)

Станция должна думать, что серверы Яндекса — это ваш ПК. Для этого: подмена DNS +
принудительный DNS (чтобы станция не обошла его) + свой CA (см. выше).

### Список доменов

```text
quasar.yandex.net            quasar.yandex.ru
oauth.yandex.ru              login.yandex.ru
mobileproxy.passport.yandex.net
startup.mobile.yandex.net    report.appmetrica.yandex.net
clck.yandex.net              analytics.mobile.yandex.net
uniproxy.alice.yandex.net    uniproxy-quic.alice.yandex.net
push.yandex.ru               scbh.yandex.net
rpc.alice.yandex.net         updater.mobile.yandex.net   ← лучше заблокировать (OTA)
```

Полный актуальный список — `KNOWN_HOSTS` в `quasard/http_api.py`.

### Вариант A: AdGuard Home на ПК (проще всего)

1. AdGuard Home → **Filters → DNS rewrites** → добавить для каждого домена:

   ```text
   quasar.yandex.net            → 192.168.50.95
   quasar.yandex.ru             → 192.168.50.95
   oauth.yandex.ru              → 192.168.50.95
   login.yandex.ru              → 192.168.50.95
   uniproxy.alice.yandex.net    → 192.168.50.95
   uniproxy-quic.alice.yandex.net → 192.168.50.95
   scbh.yandex.net              → 192.168.50.95
   ... остальные из списка выше
   ```

   Можно одной wildcard-записью: `*.yandex.net → 192.168.50.95` и `*.yandex.ru → 192.168.50.95`.

2. **Отключить IPv6-ответы** для этих доменов — иначе станция уйдёт по AAAA к настоящему
   Яндексу мимо вас. Правило в *Filters → Custom filtering rules*:

   ```text
   ||quasar.yandex.net^$dnstype=AAAA
   ||uniproxy.alice.yandex.net^$dnstype=AAAA
   ||scbh.yandex.net^$dnstype=AAAA
   ||oauth.yandex.ru^$dnstype=AAAA
   ```

3. На роутере (ASUS/Zyxel/...) в DHCP укажите **DNS-сервер = IP вашего ПК**, чтобы станция
   получала именно AdGuard.
4. Заверните весь DNS на роутере на ПК (иначе станция может сходить на 8.8.8.8):

   ```bash
   # пример для Linux/OpenWRT (nftables)
   nft add table ip dns_hijack
   nft add chain ip dns_hijack prerouting '{ type nat hook prerouting priority -100; policy accept; }'
   nft add rule ip dns_hijack prerouting udp dport 53 dnat to 192.168.50.95
   nft add rule ip dns_hijack prerouting tcp dport 53 dnat to 192.168.50.95
   ```

### Вариант B: dnsmasq на роутере (OpenWRT)

```text
# /etc/dnsmasq.conf или /etc/dnsmasq.d/quasar.conf
address=/quasar.yandex.net/192.168.50.95
address=/uniproxy.alice.yandex.net/192.168.50.95
address=/oauth.yandex.ru/192.168.50.95
# IPv6 закрываем, чтобы колонка не ушла к настоящему Яндексу
address=/quasar.yandex.net/::
address=/uniproxy.alice.yandex.net/::
```

Применить: `/etc/init.d/dnsmasq restart`. Проверить с ПК:
`nslookup uniproxy.alice.yandex.net 192.168.50.1`.

### Вариант C: `/etc/hosts` прямо на станции (быстрый обход)

```bash
adb shell "mount -o remount,rw / && \
  echo '192.168.50.95 quasar.yandex.net quasar.yandex.ru scbh.yandex.net uniproxy.alice.yandex.net oauth.yandex.ru' >> /etc/hosts && sync"
```

Работает, но не покрывает все домены и не мешает станции ходить на свой DNS — используйте
как дополнение к A/B.

### Проверка, что станция видит сервер

```bash
adb shell "curl -s -o /dev/null -w '%{http_code}\n' http://scbh.yandex.net/generate_204"   # 204
adb shell "curl -s -o /dev/null -w '%{http_code}\n' https://quasar.yandex.net/"            # 200
```

## Wi-Fi: закрепить станцию за одной точкой доступа

> **Это обязательный шаг.** Если его пропустить, станция будет рвать WebSocket каждые
> 30–60 секунд, и диалог будет обрываться на середине фразы. Проверено на живой станции:
> без закрепления сессия живёт 30–60 с, с ним — часы.

### Почему обрывы

Прошивка станции сама решает, «есть ли интернет», и проверяет это запросом
`http://www.google.com/generate_204` (ключ `wifid.generate204.controlUrls` в
`/system/vendor/quasar/quasar.cfg`). В России этот адрес недоступен, поэтому вердикт
всегда «интернета нет», и дальше срабатывает защита:

```
[wifi_capability] Start reassociation: noInternet     ← каждые ~60 c
[wifi_reseter]    Reseting wifi. Reason is nointernet ← каждые ~120 c
CTRL-EVENT-DISCONNECTED ... locally_generated=1       ← рвётся ВСЁ, включая WS
```

Плюс отдельная беда: если в сети раздан IPv6 (RA от роутера), а маршрута в IPv6-интернет
нет, станция часть проверок делает по IPv6 и тоже получает отказ.

Ещё один слой — **миграция между точками доступа**. Если в эфире несколько AP с одним
SSID или несколько сохранённых сетей, менеджер станции периодически перескакивает между
ними (у нас — между дальней точкой на −61 dBm и ближней на −26 dBm), и каждый скачок
убивает TCP-соединение. Станция при этом сама себе ставит чужой сети приоритет `2222`,
а «правильной» — `-999`, поэтому `wpa_cli remove_network`/`disable_network` не держатся:
менеджер возвращает сети из облака и заново их включает.

### Что делает `scripts/wifi-pin-xiaomi.sh`

Демон, цикл 3 секунды:

1. всем записям нужного SSID ставит `priority 3000` (выше менеджерских `2222`);
2. у всех остальных сетей **заменяет PSK** на `blocked-by-wifi-pin-xiaomi` — даже если
   менеджер такую сеть выберет, 4-way handshake не пройдёт и станция останется на нужной;
3. если ассоциация всё-таки не на нужной сети — принудительно `select_network` + `reassociate`.

Записи при этом не удаляются (удаление вызывает шторм повторных добавлений каждые ~4 с).
Настоящий пароль остаётся в облачном списке сети — вернуть доступ можно в приложении.

### Установка

```bash
# 1. поправить SSID в скрипте
#    KEEP="Xiaomi_7787"  →  имя вашей точки доступа

# 2. залить на станцию
adb connect 192.168.50.104:5555
adb push scripts/wifi-pin-xiaomi.sh /data/opt/bin/wifi-pin-xiaomi.sh
adb shell "chmod 755 /data/opt/bin/wifi-pin-xiaomi.sh && mkdir -p /data/opt/var"

# 3. запустить
adb shell "setsid /data/opt/bin/wifi-pin-xiaomi.sh </dev/null >/dev/null 2>&1 &"
```

### Автозапуск при загрузке

Станция использует busybox `init` и скрипты `/etc/init.d/S*`. Создайте
`/etc/init.d/S99zwifipin`:

```sh
#!/bin/sh
SCR=/data/opt/bin/wifi-pin-xiaomi.sh
LOG=/data/opt/var/wifi-pin.log

start() {
  [ -x "$SCR" ] || return 1
  ps w 2>/dev/null | grep -q '[w]ifi-pin-xiaomi.sh' && return 0
  setsid "$SCR" </dev/null >/dev/null 2>&1 &
  echo "$(date) wifi-pin started" >> "$LOG"
}
stop() { killall wifi-pin-xiaomi.sh 2>/dev/null; }

case "$1" in
  start)   ( start ) >/dev/null 2>&1 & ;;
  stop)    stop ;;
  restart) stop; sleep 1; ( start ) >/dev/null 2>&1 & ;;
esac
exit 0
```

```bash
adb push S99zwifipin /etc/init.d/S99zwifipin     # предварительно mount -o remount,rw /
adb shell "chmod 755 /etc/init.d/S99zwifipin"
```

### Проверка

```bash
adb shell "wpa_cli -i wlan0 status | head -4"          # ssid нужной сети, wpa_state=COMPLETED
adb shell "wpa_cli -i wlan0 list_networks"             # все прочие — без флагов [CURRENT]
adb shell "tail -20 /data/opt/var/wifi-pin.log"
```

В логе `quasard/data/server.log` сессии должны перестать рваться:

```
WS подключение: device=L01... ssid=...
ACK: станция подтвердила keep-alive — соединение живое
keep-alive push #80: ответов 79, тишина на входе 5 c
```

### Если обрывы остались (продвинутый шаг)

Дополнительно можно вылечить саму проверку интернета в прошивке
(`/system/vendor/quasar/quasar.cfg`, **сделайте бэкап!**):

```jsonc
"wifid": { "generate204": { "controlUrls": [ "http://scbh.yandex.net/generate_204" ] } },
"common": { "wifiReseter": { "minResetIntervalSec": 86400,
                             "noInternet": { "intervalSec": 86400, "method": "old" } } },
"wifi_capability": { "enabledByDefault": true,
                     "reassociateOnNoInternet": false,
                     "reassociateWhenNoInternet": false,
                     "enableReassociateReset": false }
```

Проверка адреса теперь идёт на ваш же сервер (он отвечает `204`), ресетер и
переассоциация по «нет интернета» отключены. После правки — `reboot`.

Полезно также выключить IPv6 на станции (если в сети нет рабочего IPv6):

```bash
adb shell "echo 1 > /proc/sys/net/ipv6/conf/wlan0/disable_ipv6"
```

## Запуск: `launcher.py`

```bash
python launcher.py
```

Что происходит:

1. предварительные проверки (Python, `config.toml`, `certs/`, свободные порты, файлы голоса);
2. запуск **f5_server** → ожидание `/health`;
3. запуск **quasard-youtube** → ожидание `/healthz`;
4. запуск **quasard** → ожидание открытого 443;
5. цветные логи с префиксами, дубли в `logs/<сервис>.log`;
6. `Ctrl+C` — мягкая остановка всех (при необходимости принудительная, вместе с дочерними
   процессами yt-dlp/ffmpeg); повторный `Ctrl+C` — немедленный выход.

Полезные флаги:

| Флаг | Что делает |
|---|---|
| `--check` | только проверки, без запуска |
| `--no-f5` | без клона голоса (например, `engine = "edge"`) |
| `--no-youtube` | без моста YouTube |
| `--only quasard` | только «мозг» (`f5,youtube,quasard`) |
| `--stop-timeout 15` | сколько ждать остановки |
| `--no-color` | без ANSI-цветов |

Отдельные окружения для F5 и моста задаются в `config.toml`:

```toml
[launcher]
f5_python = "C:/QuasarPrime/.venv-f5/Scripts/python.exe"
youtube_python = ""
f5_port = 8777
f5_nfe = 32
```

## Свой голос

В репозитории лежит готовый образец: `voice/optimus_24k.wav` + `voice/optimus.txt`
(«Меня зовут Оптимус Прайм. Мы автономные роботы-организмы с планеты Кибертрон.»).

Чтобы сделать свой:

1. Запишите **10–15 секунд** чистой речи: без музыки, эха и шума, ровным голосом.
2. Конвертируйте в WAV 24 кГц моно:
   `ffmpeg -i my.wav -ar 24000 -ac 1 voice/my_voice.wav`
3. Положите рядом текстовый файл с точной расшифровкой того, что сказано —
   это обязательный вход F5: `voice/my_voice.txt`.
4. Укажите пути в `config.toml`:

   ```toml
   [launcher]
   f5_ref = "voice/my_voice.wav"
   f5_ref_text = "voice/my_voice.txt"

   [tts]
   f5_voice_id = "ref_voice_2"   # сменили голос — смените метку (кэш)
   ```

5. Перезапустите `launcher.py`.

Качество клона сильно зависит от образца: ровный темп, без смеха и пауз «э-э».

## Музыка и YouTube-мост

Мост `quasard-youtube` играет музыку **самой станцией**: он качает трек с YouTube через
`yt-dlp`, отдаёт MP3-поток по HTTP, а станции отправляет команду `audio_play` по
Glagol-каналу (порт 1961 на самой станции).

Настройка (кратко; подробности — в `quasard-youtube/README.md`):

```bash
copy quasard-youtube\config.example.toml quasard-youtube\config.toml
```

```toml
[station]
host = "192.168.50.104"     # IP станции
session_id = "${YANDEX_SESSION_ID}"   # cookie Session_id с yandex.ru
                                    # лучше задать переменной окружения
[server]
port = 8099
[playback]
directive = "audio_play"    # для Station Lite 2; radio_play на 0.369 не играет
```

В основном конфиге мост включается так:

```toml
[youtube]
enabled = true
bridge_url = "http://127.0.0.1:8099"
```

Дальше достаточно сказать «включи Кино Группа крови» — LLM вернёт `PLAY_YOUTUBE: ...`,
а `quasard` передаст это мосту.

Отдельно доступна Яндекс.Музыка (`[music]` в конфиге) — если нужна она, а не YouTube.

## Диагностика

| Симптом | Причина | Что делать |
|---|---|---|
| Станция отвечает голосом Алисы | она не попала на ваш DNS | проверить rewrite'ы, `adb shell nslookup quasar.yandex.net` |
| `TLS handshake failed` в логах станции | не установлен ваш CA | раздел [Сертификаты](#5-сертификаты), затем `reboot` |
| WS рвётся каждые 30–60 с | миграция между AP / «нет интернета» в прошивке | раздел [Wi-Fi](#wi-fi-закрепить-станцию-за-одной-точкой-доступа) |
| Станция молчит после «Алиса» | не поднялся ASR/TTS | `logs/quasard.log`, `logs/f5.log` |
| Долгий первый ответ | модель грузится в память | `launcher.py` ждёт готовности F5, первый ответ дольше |
| `CUDA out of memory` | не хватает VRAM | `f5_nfe = 16`, `engine = "edge"` или карта побольше |
| Музыка не играет, но команда принята | прошивка игнорирует `radio_play` | `[playback] directive = "audio_play"` в мосте |
| `не открылся 443` | порт занят/нет прав | остановить прошлый процесс, на Linux — `sudo` |
| Ответ LLM пустой | неверный ключ/модель | `logs/quasard.log`, проверить `[llm]` |

Логи: `logs/quasard.log`, `logs/f5.log`, `logs/youtube.log`;
трафик uniproxy — `data/traffic/*.jsonl` (при `log_traffic = true`).

Проверка без колонки:

```bash
python -c "import httpx; print(httpx.get('http://127.0.0.1:8777/health').json())"
python -c "import httpx; print(httpx.get('http://127.0.0.1:8099/healthz').status_code)"
curl -k https://127.0.0.1/ -o NUL -w "%{http_code}\n"     # 200
```

## Риски и дисклеймер

Проект **экспериментальный** и вмешивается в работу вашего устройства. Прежде чем начинать:

- **Сделайте бэкап.** Минимум: `certs/`, `data/`, и на станции —
  `/system/vendor/quasar/quasar.cfg`, `/etc/hosts`, `/system/vendor/quasar/certs/ca-certificates.crt`,
  `/bkupgrade/data/wpa_supplicant.conf`. Все правки в этом проекте обратимы, но только если
  есть чем откатить.
- **Root-доступ и правка системного раздела** могут превратить колонку в кирпич при ошибке.
  Не редактируйте `quasar.cfg` и `launch.json` без проверки JSON и без копии оригинала.
- **Wi-Fi-скрипт заменяет пароли** сохранённых сетей на заглушку. Вернуть сеть можно в
  приложении «Дом с Алисой»; пароль от своей сети вы знаете.
- **Ключи и токены** (`config.toml`, `certs/*.key`, cookie `session_id`) — это доступ к
  вашим аккаунтам. В git они не попадают (см. `.gitignore`), но не выкладывайте их и вручную.
- **Свой CA** даёт возможность подписывать сертификаты. Держите `ca.key` только на своём ПК
  и доверяйте CA только своей станции. Не используйте чужие/утёкшие ключи публичных CA —
  это компрометация TLS для всех, а не только для вашего устройства.
- **Прошивка может обновиться** и сломать всё: блокируйте `updater.mobile.yandex.net`.
- **Юридическая сторона:** это интероп с собственным устройством в личных целях. Проект
  не связан с Яндексом, не содержит кода Яндекса и не призывает обходить чужие сервисы.
  Музыка из YouTube и Яндекс.Музыки — по правилам соответствующих сервисов.
- **Никаких гарантий.** Автор не отвечает за сломанные колонки, аккаунты и потерю данных.

## Лицензия и благодарности

Код — **MIT** (см. [LICENSE](LICENSE)). Модели и библиотеки — под своими лицензиями:

- [salute-developers/GigaAM](https://github.com/salute-developers/GigaAM) — распознавание речи (MIT)
- [snakers4/silero-vad](https://github.com/snakers4/silero-vad) — детектор речи (MIT,
  лицензия в `quasard/assets/SILERO_VAD_LICENSE`)
- [SWivid/F5-TTS](https://github.com/SWivid/F5-TTS) — синтез с клонированием голоса;
  русские веса [ESpeech/ESpeech-TTS-1_RL-V2](https://huggingface.co/ESpeech/ESpeech-TTS-1_RL-V2) (Apache-2.0)
- [Den4ikAI/ruaccent](https://github.com/Den4ikAI/ruaccent) — ударения для русского
- [MarshalX/yandex-music-api](https://github.com/MarshalX/yandex-music-api) — Яндекс.Музыка
- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube
- [v3rt3p/quasar-server](https://github.com/v3rt3p/quasar-server) — справочник по протоколу станции
- [PyAV](https://github.com/PyAV-Org/PyAV), [FastAPI](https://github.com/fastapi/fastapi),
  [uvicorn](https://github.com/encode/uvicorn) и остальные из `requirements.txt`

Отдельное спасибо сообществу, которое разобрало протокол uniproxy и Glagol-канал станции.
