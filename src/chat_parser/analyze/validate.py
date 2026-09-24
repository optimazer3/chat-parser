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
AUTHOR_LINE_RE = re.compile(r"^\[m:(\d+) \| (u:[0-9a-f]{8}) \|[^\]]*\]\s?(.*)$")
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


def validate_people(
    extraction: Extraction, source: str
) -> list[tuple[str, str | None, str | None, str, int]]:
    """Подсказки «кто есть кто» -> (label, компания, роль, цитата, id сообщения).

    Цитата должна дословно стоять в сообщении ЭТОГО же человека: так чужие
    слова ему не припишутся.
    """
    lines = [m for m in map(AUTHOR_LINE_RE.match, source.splitlines()) if m]
    out = []
    for p in extraction.people:
        company = p.company.strip() or None
        role = p.role.strip() or None
        quote = norm(p.evidence_quote)
        if not (company or role) or len(quote.split()) < MIN_QUOTE_WORDS:
            continue
        own = next((m for m in lines if m.group(2) == p.author_label
                    and quote in norm(m.group(3))), None)
        if own is None:
            continue
        out.append((p.author_label, company, role, p.evidence_quote.strip(), int(own.group(1))))
    return out
