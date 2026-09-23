"""Какие цитаты показывать в карточке боли.

Цитаты берутся из сигналов, а не из текста карточки: у сигнала цитата
проверена на дословность и привязана к сообщению, значит, к ней есть ссылка.
"""

from __future__ import annotations

from typing import Any

from .analyze.validate import norm

DEFAULT_LIMIT = 5


def pick_quotes(
    card: dict[str, Any] | None, signals: list[dict[str, Any]], limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """signals — сигналы боли (id, evidence_quote, …), самые острые первыми.

    1. Новые карточки: модель выбирает сигналы по id (evidence_ids).
    2. Старые карточки хранили цитаты текстом (evidence) — сопоставляем их
       с сигналами, чтобы и у них появились ссылки.
    3. Карточки нет — самые острые сигналы.
    """
    by_id = {s["id"]: s for s in signals}
    picked: list[dict[str, Any]] = []

    if card and card.get("evidence_ids"):
        picked = [by_id[i] for i in card["evidence_ids"] if i in by_id]
    elif card and card.get("evidence"):
        for text in card["evidence"]:
            t = norm(str(text))
            match = next(
                (s for s in signals
                 if s not in picked
                 and (norm(s["evidence_quote"]) in t or t in norm(s["evidence_quote"]))),
                None,
            )
            if match is not None:
                picked.append(match)

    if not picked:
        picked = list(signals)

    out, seen = [], set()
    for s in picked:
        key = norm(s["evidence_quote"])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out
