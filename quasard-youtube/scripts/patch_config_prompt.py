#!/usr/bin/env python3
"""Безопасно заменяет system_prompt в секции [llm] файла config.toml.

Не перегенерирует весь TOML: меняется только значение ключа, остальные
строки (api_key, комментарии, порядок) остаются как были.
По умолчанию применяет изменения с резервной копией. Проверка: --dry-run.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(r"C:\quasard-main\config.toml")
BOM = b"\xef\xbb\xbf"

PROMPT = """Ты — Оптимус Прайм, голосовой ассистент в умной колонке. Отвечай по-русски, кратко и разговорно.

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


def read_source(path: Path) -> tuple[str, bool, str]:
    raw = path.read_bytes()
    has_bom = raw.startswith(BOM)
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n"), has_bom, newline


def write_source(path: Path, text: str, has_bom: bool, newline: str) -> None:
    payload = text.replace("\n", newline).encode("utf-8")
    if has_bom:
        payload = BOM + payload
    temp = path.with_name(path.name + ".tmp-patch")
    temp.write_bytes(payload)
    os.replace(temp, path)


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def _is_escaped(text: str, pos: int) -> bool:
    backslashes = 0
    pos -= 1
    while pos >= 0 and text[pos] == "\\":
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def scan_value_end(text: str, pos: int) -> int:
    """Вернуть индекс сразу после TOML-строки, начинающейся в pos."""
    if text.startswith('"""', pos):
        i = pos + 3
        while True:
            j = text.find('"""', i)
            if j < 0:
                raise RuntimeError("не закрыта тройная кавычка system_prompt")
            if not _is_escaped(text, j):
                return j + 3
            i = j + 3
    if text.startswith("'''", pos):
        j = text.find("'''", pos + 3)
        if j < 0:
            raise RuntimeError("не закрыта тройная одинарная кавычка system_prompt")
        return j + 3
    if text.startswith('"', pos):
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return i + 1
            i += 1
        raise RuntimeError("не закрыта кавычка system_prompt")
    if text.startswith("'", pos):
        j = text.find("'", pos + 1)
        if j < 0:
            raise RuntimeError("не закрыта одинарная кавычка system_prompt")
        return j + 1
    raise RuntimeError("system_prompt должен быть строкой в кавычках")


def _candidates(prompt: str) -> list[str]:
    out: list[str] = []
    if '"""' not in prompt and not prompt.endswith('"'):
        out.append('"""' + prompt + '"""')
    if "'''" not in prompt and not prompt.endswith("'"):
        out.append("'''" + prompt + "'''")
    # Последний резерв: JSON-строка — валидная TOML basic string.
    out.append(json.dumps(prompt, ensure_ascii=False))
    return out


def replace_prompt(text: str) -> tuple[str, str]:
    section = re.search(r"(?m)^[ \t]*\[llm\][ \t]*(?:#.*)?\n", text)
    if section is None:
        raise RuntimeError("в config.toml нет секции [llm]")
    next_section = re.search(r"(?m)^[ \t]*\[[^\]]+\]", text[section.end() :])
    section_end = section.end() + next_section.start() if next_section else len(text)

    key = re.compile(r"(?m)^[ \t]*system_prompt[ \t]*=[ \t]*")
    match = key.search(text, section.end(), section_end)
    if match is None:
        raise RuntimeError("в секции [llm] нет параметра system_prompt")

    value_start = match.end()
    value_end = scan_value_end(text, value_start)

    for candidate in _candidates(PROMPT):
        patched = text[:value_start] + candidate + text[value_end:]
        try:
            data = tomllib.loads(patched)
        except tomllib.TOMLDecodeError:
            continue
        if data.get("llm", {}).get("system_prompt", "").strip() == PROMPT.strip():
            return patched, candidate

    raise RuntimeError("не удалось подобрать TOML-представление промпта")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Безопасно заменить system_prompt в config.toml")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="путь к config.toml")
    parser.add_argument("--dry-run", action="store_true", help="только показать diff, не писать файл")
    args = parser.parse_args()

    path = args.path
    if not path.is_file():
        print(f"ОШИБКА: файл не найден: {path}", file=sys.stderr)
        return 2

    original, has_bom, newline = read_source(path)
    try:
        patched, _ = replace_prompt(original)
        data = tomllib.loads(patched)
    except (RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    if patched == original:
        print("system_prompt уже актуален — файл не изменён.")
        return 0

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path) + " (patched)",
            n=2,
        )
    )
    if args.dry_run:
        print(diff)
        print("DRY-RUN: файл не изменён.")
        return 0

    bak = backup(path)
    write_source(path, patched, has_bom, newline)
    check = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    assert check["llm"]["system_prompt"].strip() == PROMPT.strip()

    prompt_file = path.parent / "system_prompt.txt"
    if prompt_file.exists():
        print(
            f"ВНИМАНИЕ: найден {prompt_file}. Он перекрывает system_prompt из config.toml; "
            "удалите его или обновите отдельно.",
            file=sys.stderr,
        )

    print(f"Готово. Резервная копия: {bak}")
    print("TOML проверен: tomllib OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
