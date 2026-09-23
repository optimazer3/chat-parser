"""Проверка сигналов против исходного текста — защита от галлюцинаций.

Модель обязана приводить дословную цитату. Если цитаты нет в исходнике,
сигнал выдуман и выбрасывается. Модуль намеренно без тяжёлых зависимостей,
чтобы его можно было гонять в тестах.
"""

from __future__ import annotations

import re

from .schema import Extraction, Signal

WS_RE = re.compile(r"\s+")
# строка треда: [m:<id> | <автор> | <дата>…] текст  (см. normalize/chunker.py)
LINE_RE = re.compile(r"^\[m:(\d+) \|[^\]]*\]\s?(.*)$")
MIN_QUOTE_CHARS = 8
# Цитата из одного-двух слов («jacquemus») ничего не доказывает.
MIN_QUOTE_WORDS = 3


def norm(s: str) -> str:
    return WS_RE.sub(" ", s).strip().lower()


def quote_message_id(source: str, quote: str) -> int | None:
    """id сообщения, в котором стоит цитата (quote уже нормализован)."""
    for line in source.splitlines():
        m = LINE_RE.match(line)
        if m and quote in norm(m.group(2)):
            return int(m.group(1))
    return None


def validate(
    extraction: Extraction, source: str, allowed_ids: set[int]
) -> tuple[list[Signal], int]:
    """Возвращает (валидные сигналы, число отбракованных)."""
    haystack = norm(source)
    good: list[Signal] = []
    dropped = 0
    for s in extraction.signals:
        quote = norm(s.evidence_quote)
        if (
            len(quote) < MIN_QUOTE_CHARS
            or len(quote.split()) < MIN_QUOTE_WORDS
            or quote not in haystack
        ):
            dropped += 1
            continue
        s.message_ids = [i for i in s.message_ids if i in allowed_ids]
        if not s.message_ids:
            dropped += 1
            continue
        # Ссылка под цитатой ведёт на message_ids[0] — ставим туда сообщение,
        # где цитата стоит на самом деле, а не первое, что назвала модель.
        where = quote_message_id(source, quote)
        if where is not None and where in allowed_ids:
            s.message_ids = [where] + [i for i in s.message_ids if i != where]
        good.append(s)
    return good, dropped
