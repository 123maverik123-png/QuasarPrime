"""Обрезать экспорт куки до доменов YouTube.

Полный экспорт из браузера содержит куки всех сайтов: yandex.ru (вместе с
Session_id для quasard), соцсети, рекламные трекеры. yt-dlp они не нужны —
он отправляет только куки домена запроса. Такой файл меньше и его не так
страшно случайно передать.

Исходный файл не изменяется: результат пишется рядом отдельным файлом.

    python scripts/trim_cookies.py                       # cookies.txt -> cookies.youtube.txt
    python scripts/trim_cookies.py --source C:/secrets/youtube.txt
    python scripts/trim_cookies.py --keep google.ru      # добавить домен
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_KEEP = (
    "youtube.com",
    "youtube-nocookie.com",
    "ytimg.com",
    "ggpht.com",
    "googlevideo.com",
    "google.com",
    "gstatic.com",
    "accounts.google.com",
)
HEADER = "# Netscape HTTP Cookie File"


def keep_line(line: str, keep: tuple[str, ...]) -> bool:
    """Оставить строку, если её домен относится к разрешённым."""
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith("#httponly_"):
        stripped = stripped[len("#HttpOnly_") :]
    elif stripped.startswith("#"):
        # комментарии и заголовок отбрасываем: свой заголовок напишем сами
        return False
    domain = stripped.split("\t")[0].lower().lstrip(".")
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in keep)


def trim(source: Path, target: Path, keep: tuple[str, ...]) -> tuple[int, int]:
    if source.resolve() == target.resolve():
        raise SystemExit("Цель совпадает с источником: укажите другой --target")
    text = source.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in text.splitlines() if keep_line(line, keep)]
    if not lines:
        raise SystemExit(
            f"В {source} не нашлось куки для доменов: {', '.join(keep)}"
        )
    target.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    total = sum(1 for line in text.splitlines() if line.strip() and not line.startswith("#"))
    return len(lines), total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Оставить в cookies.txt только куки доменов YouTube/Google"
    )
    parser.add_argument(
        "--source",
        default="cookies.txt",
        help="полный экспорт (по умолчанию cookies.txt)",
    )
    parser.add_argument(
        "--target",
        default="",
        help="результат (по умолчанию <source>.youtube.txt рядом)",
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        help="дополнительный домен, можно указывать несколько раз",
    )
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"Нет файла: {source}", file=sys.stderr)
        return 2
    target = (
        Path(args.target).expanduser()
        if args.target
        else source.with_name(f"{source.stem}.youtube{source.suffix}")
    )
    keep = (*DEFAULT_KEEP, *[k.strip().lstrip(".").lower() for k in args.keep if k.strip()])
    kept, total = trim(source, target, keep)
    print(f"Готово: {target}")
    print(f"Куки: {kept} из {total} (отброшено {total - kept})")
    print(f"Источник не изменён: {source}")
    print(f"Проверка: python -m quasard_youtube doctor  (после смены cookies_file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
