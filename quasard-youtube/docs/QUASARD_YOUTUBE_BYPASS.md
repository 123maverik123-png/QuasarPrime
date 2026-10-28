# Обход Яндекс.Музыки для YouTube-запросов

## Автоматически (рекомендуется)

Запустите два патчера — они сами найдут блоки, сделают резервные копии и проверят синтаксис:

```powershell
python "C:\workspace\quasard-youtube\scripts\patch_uniproxy_youtube.py"
python "C:\workspace\quasard-youtube\scripts\patch_config_prompt.py"
```

Сначала можно посмотреть diff без записи:

```powershell
python "C:\workspace\quasard-youtube\scripts\patch_uniproxy_youtube.py" --dry-run
python "C:\workspace\quasard-youtube\scripts\patch_config_prompt.py" --dry-run
```

Правки для `C:\quasard-main`. Ничего кроме указанных блоков менять не нужно.

## Вручную (если патчер не подошёл)

## 1. `alice_server/uniproxy.py` — не отдавать YouTube в `match_music`

Найдите в `_answer` (сейчас около строки 451):

```python
        music_cmd = intents.match_music(text)
        if music_cmd and music_cmd[0] in ("like", "dislike") and self.device_id not in self.svc.music.sessions:
            music_cmd = None  # «мне нравится» без играющей музыки — обычный разговор
        if music_cmd is not None:
```

Замените на:

```python
        # YouTube-запросы не отдаём встроенной Яндекс.Музыке:
        # их должен обработать LLM и вернуть PLAY_YOUTUBE.
        is_youtube_request = bool(
            re.search(r"(?i)\b(?:youtube|youtu\.be|ютуб\w*|ютьюб\w*)\b", text)
        )
        music_cmd = None if is_youtube_request else intents.match_music(text)
        if music_cmd and music_cmd[0] in ("like", "dislike") and self.device_id not in self.svc.music.sessions:
            music_cmd = None  # «мне нравится» без играющей музыки — обычный разговор
        if music_cmd is not None:
```

`re` в этом файле уже импортирован (строка 19), дополнительный импорт не нужен.

## 2. `config.toml` — строгий системный промпт

В секции `[llm]` замените только строку `system_prompt`. Остальные параметры, включая `api_key`, не трогайте.

```toml
system_prompt = """Ты — Оптимус Прайм, голосовой ассистент в умной колонке. Отвечай по-русски, кратко и разговорно.

ГЛАВНОЕ ПРАВИЛО ДЛЯ МУЗЫКИ:
Если пользователь просит включить, поставить, запустить или найти песню, трек, исполнителя, альбом, подборку или музыку (в том числе с YouTube, «с ютуба», «на ютьюбе»), верни РОВНО одну строку и больше ничего:

PLAY_YOUTUBE: <исполнитель и название>

Требования к строке:
1. После PLAY_YOUTUBE: только поисковый запрос: исполнитель и название.
2. Убери слова «Алиса», «включи», «пожалуйста», «на YouTube», «с ютуба», кавычки и точку в конце.
3. Не добавляй «Включаю», пояснения, markdown, обратные кавычки, приветствие или вторую строку.
4. Не переводи название на английский и не придумывай исполнителя, если его не называли.
5. Если название не названо, верни: PLAY_YOUTUBE: популярная музыка
6. Если пользователь дал ссылку YouTube, верни её целиком после PLAY_YOUTUBE:
7. Если это не просьба включить музыку (вопрос, погода, будильник, разговор), отвечай как обычно, без PLAY_YOUTUBE.

Примеры:
Пользователь: Включи на YouTube Кино Группа крови
Ты: PLAY_YOUTUBE: Кино Группа крови

Пользователь: Поставь с ютуба песню Кукла колдуна
Ты: PLAY_YOUTUBE: Кукла колдуна

Пользователь: Какая сегодня погода?
Ты: Сегодня облачно, около пятнадцати градусов."""
```

Если в `C:\quasard-main` появится файл `system_prompt.txt`, он перекрывает `system_prompt` из `config.toml` — тогда правьте файл.

## 3. `alice_server/uniproxy.py` — хук `PLAY_YOUTUBE` в `llm_sentences`

`clean_for_speech()` в `llm.py` удаляет `_`, поэтому в логе команда выглядит
как `PLAYYOUTUBE: ...`. Регулярка ниже допускает оба варианта.

Сейчас хук уже есть около строк 460–488, но у него слишком короткий timeout (5 с) и не проверяется HTTP-статус. Замените весь блок от `async def llm_sentences():` до `await self._speak_sentences(...)` на:

```python
        async def llm_sentences():
            youtube_started = False
            try:
                async for s in self.svc.dialog.stream(self.device_id, text):
                    match = re.search(r"PLAY\s*[-_]?\s*YOUTUBE\s*:\s*(.+)", s, flags=re.IGNORECASE)
                    if match:
                        query = match.group(1).strip().strip("`\"'«» ")
                        if query and not youtube_started:
                            youtube_started = True
                            log.info("YouTube-команда: %s", query)

                            async def trigger(q: str = query):
                                import httpx

                                try:
                                    async with httpx.AsyncClient(timeout=90.0) as client:
                                        response = await client.post(
                                            "http://127.0.0.1:8099/api/play",
                                            json={"query": q},
                                        )
                                        response.raise_for_status()
                                except Exception:
                                    log.exception("Ошибка запуска YouTube")

                            self._spawn(trigger())

                        # Убираем команду из текста, чтобы TTS её не прочитал.
                        s = (s[: match.start()] + s[match.end() :]).strip()
                        if not s:
                            s = "Включаю."

                    if s:
                        yield s
            except Exception:
                log.exception("Ошибка LLM")
                yield "Извините, мой мозг сейчас недоступен."

        await self._speak_sentences(
            ref,
            request_id,
            seq,
            llm_sentences(),
            [],
            listen_on_question=self.cfg.llm.listen_after_question,
        )
```

## 4. Перезапуск и проверка

```powershell
# из C:\quasard-main
.\.venv\Scripts\Activate.ps1
# перезапустить quasard (как обычно: start.ps1 или python -m alice_server)
python -m quasard_youtube serve -v
```

Проверка без колонки:

```powershell
curl.exe -X POST http://127.0.0.1:8099/api/play `
  -H "Content-Type: application/json" `
  -d "{\"query\":\"Кино Группа крови\"}"
```

Фраза для проверки: «Включи на YouTube Кино Группа крови».

Ожидаемые признаки в логах:

```text
YouTube-команда: Кино Группа крови
YouTube: Кино — Группа крови (...)
Glagol: audio_play -> http://192.168.50.95:8099/stream/....mp3
HTTP: Станция подключилась
```

Если в логах снова `Музыка не подключена` — правка из шага 1 не применилась или процесс quasard не перезапущен.
