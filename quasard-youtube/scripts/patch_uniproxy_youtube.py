#!/usr/bin/env python3
"""Патчер C:\\quasard-main\\alice_server\\uniproxy.py.

Меняет два места:
  1. music_cmd = intents.match_music(text) -> пропуск для YouTube/Ютуб.
  2. внутреннюю async def llm_sentences() -> безопасный хук PLAY_YOUTUBE.

По умолчанию применяет изменения, сделав резервную копию.
Проверить без записи: --dry-run.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import os
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

DEFAULT_PATH = Path(r"C:\quasard-main\alice_server\uniproxy.py")
BOM = b"\xef\xbb\xbf"

NEW_LLM_FUNCTION = '''\
async def llm_sentences():
    youtube_started = False
    try:
        async for s in self.svc.dialog.stream(self.device_id, text):
            match = re.search(r"PLAY\\s*[-_]?\\s*YOUTUBE\\s*:\\s*(.+)", s, flags=re.IGNORECASE)
            if match:
                query = match.group(1).strip().strip("`\\"'«» ")
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
'''

MUSIC_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)music_cmd = intents\.match_music\(text\)[^\n]*\n"
    r"(?P=indent)if music_cmd and music_cmd\[0\] in \(\"like\", \"dislike\"\) "
    r"and self\.device_id not in self\.svc\.music\.sessions:\n"
    r"(?P=indent)[ \t]+music_cmd = None[^\n]*\n"
)

MUSIC_NEW = '''\
{indent}# YouTube-запросы не отдаём встроенной Яндекс.Музыке:
{indent}# их должен обработать LLM и вернуть PLAY_YOUTUBE.
{indent}is_youtube_request = bool(
{indent}    re.search(r"(?i)\\b(?:youtube|youtu\\.be|ютуб\\w*|ютьюб\\w*)\\b", text)
{indent})
{indent}music_cmd = None if is_youtube_request else intents.match_music(text)
{indent}if music_cmd and music_cmd[0] in ("like", "dislike") and self.device_id not in self.svc.music.sessions:
{indent}    music_cmd = None  # «мне нравится» без играющей музыки — обычный разговор
'''


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


def find_nested_function(tree: ast.AST, class_name: str, function_name: str) -> ast.AsyncFunctionDef:
    answer = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == class_name:
            answer = node
            break
    if answer is None:
        raise RuntimeError(f"не найден метод {class_name}()")

    for node in ast.walk(answer):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return node
    raise RuntimeError(f"не найдена функция {function_name}() внутри {class_name}()")


def patch_music(text: str) -> tuple[str, bool]:
    if "is_youtube_request" in text:
        print("[=] Блок music_cmd уже пропускает YouTube — пропуск.")
        return text, False

    matches = list(MUSIC_RE.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(
            "не удалось однозначно найти блок music_cmd; "
            f"совпадений: {len(matches)}. Файл не изменён."
        )
    match = matches[0]
    indent = match.group("indent")
    replacement = MUSIC_NEW.format(indent=indent)
    patched = text[: match.start()] + replacement + text[match.end() :]
    print("[+] Блок music_cmd заменён (YouTube обходит Яндекс.Музыку).")
    return patched, True


def patch_llm_function(text: str) -> tuple[str, bool]:
    tree = ast.parse(text)
    node = find_nested_function(tree, "_answer", "llm_sentences")
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno or node.lineno
    old = "".join(lines[start:end])

    if "youtube_started" in old:
        print("[=] llm_sentences уже содержит безопасный хук — пропуск.")
        return text, False

    indent_match = re.match(r"[ \t]*", lines[start])
    indent = indent_match.group(0) if indent_match else ""
    new_func = textwrap.indent(textwrap.dedent(NEW_LLM_FUNCTION).rstrip("\n"), indent)
    if not new_func.endswith("\n"):
        new_func += "\n"
    patched = "".join(lines[:start]) + new_func + "".join(lines[end:])
    print("[+] Функция llm_sentences заменена.")
    return patched, True


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Патч uniproxy.py для YouTube -> quasard-youtube")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="путь к uniproxy.py")
    parser.add_argument("--dry-run", action="store_true", help="только показать diff, не писать файл")
    args = parser.parse_args()

    path = args.path
    if not path.is_file():
        print(f"ОШИБКА: файл не найден: {path}", file=sys.stderr)
        return 2

    original, has_bom, newline = read_source(path)
    try:
        patched, music_changed = patch_music(original)
        patched, llm_changed = patch_llm_function(patched)
        ast.parse(patched)
    except (SyntaxError, RuntimeError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    if not music_changed and not llm_changed:
        print("Изменений нет — файл уже пропатчен.")
        return 0

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path) + " (patched)",
            n=4,
        )
    )
    if args.dry_run:
        print(diff)
        print("DRY-RUN: файл не изменён.")
        return 0

    bak = backup(path)
    write_source(path, patched, has_bom, newline)
    ast.parse(path.read_text(encoding="utf-8-sig"))
    print(f"Готово. Резервная копия: {bak}")
    print("Синтаксис проверен: ast.parse OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
