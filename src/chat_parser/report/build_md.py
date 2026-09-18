"""Markdown-отчёт по кластерам."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

AUDIENCE_RU = {
    "owner": "Владельцы оптик",
    "staff": "Персонал салонов",
    "optometrist": "Оптометристы и врачи",
    "supplier": "Поставщики",
    "customer": "Покупатели",
    "unknown": "Не определено",
}


def _msg_link(username: str | None, chat_id: int, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}"


async def build(pool: asyncpg.Pool, out: Path, top: int = 30) -> Path:
    clusters = await pool.fetch(
        "select * from clusters order by audience, score desc"
    )
    totals = await pool.fetchrow(
        "select (select count(*) from messages) m, (select count(*) from threads) t,"
        " (select count(*) from signals) s"
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Боли и потребности рынка оптики",
        "",
        f"_Сформировано {now}. "
        f"Сообщений: {totals['m']}, тредов: {totals['t']}, сигналов: {totals['s']}._",
        "",
    ]

    by_audience: dict[str, list] = {}
    for c in clusters:
        by_audience.setdefault(c["audience"], []).append(c)

    for audience, items in by_audience.items():
        lines += [f"## {AUDIENCE_RU.get(audience, audience)}", ""]
        for i, c in enumerate(items[:top], 1):
            lines += [
                f"### {i}. {c['label']}",
                "",
                f"> {c['statement']}",
                "",
                f"**Вес {c['score']}** · {c['n_authors']} чел. · "
                f"{c['n_chats']} чат(ов) · {c['n_signals']} сигнал(ов) · "
                f"последний раз {c['last_seen']:%Y-%m-%d}",
                "",
            ]
            card = c["card"]
            if card:
                card = json.loads(card) if isinstance(card, str) else card
                lines += [
                    f"**Кто:** {card['who']}",
                    "",
                    f"**Когда возникает:** {card['when']}",
                    "",
                    "**Как выкручиваются сейчас:**",
                    *[f"- {w}" for w in card["current_workarounds"]],
                    "",
                    "**Цитаты:**",
                    *[f"> {q}" for q in card["evidence"]],
                    "",
                    "**Гипотезы решения:**",
                    *[f"- {h}" for h in card["product_hypotheses"]],
                    "",
                    "**Что выяснить интервью:**",
                    *[f"- {q}" for q in card["open_questions"]],
                    "",
                ]
            else:
                quotes = await pool.fetch(
                    """
                    select s.evidence_quote, s.chat_id, s.message_ids[1] as mid, c.username
                      from signals s join chats c on c.id = s.chat_id
                     where s.cluster_id = $1 order by s.intensity desc limit 4
                    """,
                    c["id"],
                )
                lines += ["**Цитаты:**"]
                for q in quotes:
                    link = _msg_link(q["username"], q["chat_id"], q["mid"])
                    lines.append(f"> «{q['evidence_quote']}» — [источник]({link})")
                lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
