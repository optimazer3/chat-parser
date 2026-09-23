"""Проверка сигналов против исходного текста — защита от галлюцинаций.

Модель обязана приводить дословную цитату. Если цитаты нет в исходнике,
сигнал выдуман и выбрасывается. Модуль намеренно без тяжёлых зависимостей,
чтобы его можно было гонять в тестах.
"""

from __future__ import annotations

import re

from .schema import Extraction, Signal

WS_RE = re.compile(r"\s+")
MIN_QUOTE_CHARS = 8
# Цитата из одного-двух слов («jacquemus») ничего не доказывает.
MIN_QUOTE_WORDS = 3


def norm(s: str) -> str:
    return WS_RE.sub(" ", s).strip().lower()


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
        good.append(s)
    return good, dropped
