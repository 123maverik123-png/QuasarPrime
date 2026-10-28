# Патч quasard / alice_server

Точное имя функции и TTS-объекта зависит от вашей сборки `quasard`, поэтому
ниже — точки вставки, а не готовый diff.

## 1. Запустить мост

```powershell
cd C:\workspace\quasard-youtube
.\.venv\Scripts\Activate.ps1
python -m quasard_youtube serve -v
```

Проверка: `curl.exe http://127.0.0.1:8099/healthz`.

## 2. Научить DeepSeek команде

В системный промпт (ключ в `config.toml`, часто `system_prompt`) добавьте
содержимое [`prompts/quasard_system_prompt.txt`](../prompts/quasard_system_prompt.txt).

Проверка без колонки:

```powershell
python -m quasard_youtube parse "PLAY_YOUTUBE: Кино Группа крови"
```

Ожидается `matched: true` и `query: "Кино Группа крови"`.

## 3. Вставить хук до TTS

Найдите в `alice_server` место, где ответ LLM попадает в TTS. Обычно это
одна из форм:

```python
reply = await deepseek.chat(messages)
await tts.speak(reply)
```

или:

```python
reply = await llm(...)
audio = await f5_tts(reply)
```

Замените на:

```python
from quasard_youtube.quasard import process_reply_async

raw_reply = await deepseek.chat(messages)
spoken = await process_reply_async(
    raw_reply,
    bridge_url="http://127.0.0.1:8099",
    timeout=90,
)
await tts.speak(spoken)
```

`process_reply_async`:

- если в ответе нет `PLAY_YOUTUBE` — возвращает текст без изменений;
- если есть — удаляет команду, вызывает `/api/command`, ждёт, пока Станция
  подключится к потоку, и возвращает `spoken_text` (обычно «Включаю.»);
- при ошибке возвращает короткую фразу, а не технический текст.

Важно: вызывайте хук **до** TTS. Иначе Станция озвучит строку
`PLAY_YOUTUBE: ...`.

## 4. Если ответ LLM уже стримится в TTS

Тогда проще не импортировать модуль, а вклиниться перед отправкой текста:

```python
parsed = parse_command(reply)
if parsed.matched:
    reply = parsed.spoken_text or "Включаю."
    await http_post("http://127.0.0.1:8099/api/command", {"text": original_reply})
```

В этом случае TTS не должен ждать окончания загрузки: ответ будет озвучен
сразу, а музыка начнётся через несколько секунд. Это компромисс между
скоростью ответа и обработкой ошибок.

## 5. Проверка end-to-end

1. `python -m quasard_youtube serve -v` — в логах `HTTP-сервер слушает`.
2. Скажите: «Алиса, включи на YouTube Группа крови».
3. В логах должно быть:
   - `YouTube: ...`;
   - `Glagol: audio_play -> http://192.168.50.95:8099/stream/...`;
   - `HTTP: Станция подключилась`.
4. Если LLM ответил текстом, а команды нет — проверьте системный промпт.

## 6. Частые ошибки интеграции

| Ошибка | Причина |
|---|---|
| Станция говорит «PLAY_YOUTUBE ...» | хук вызван после TTS или не вызван |
| `process_reply` вернул исходный текст | LLM не вернул команду; смотрите промпт |
| `HTTP 500` от bridge | смотрите лог `serve`; часто нет Session_id/ffmpeg |
| Ответ LLM задерживается на 10 с | хук ждёт prebuffer и Станцию; это нормально для первого запуска |
| `ModuleNotFoundError: quasard_youtube` | `pip install -e .` в том же venv, где запущен quasard |
