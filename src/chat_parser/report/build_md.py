"""Markdown-отчёт по кластерам."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from .. import people
from ..links import QUOTE_MESSAGE_SQL, message_link
from ..quotes import pick_quotes

AUDIENCE_RU = {
    "owner": "Владельцы оптик",
    "staff": "Персонал салонов",
    "optometrist": "Оптометристы и врачи",
    "supplier": "Поставщики",
    "customer": "Покупатели",
    "unknown": "Не определено",
}


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
                f"**Вес {c['score']:.1f}** · {c['n_authors']} чел. · "
                f"{c['n_chats']} чат(ов) · {c['n_signals']} сигнал(ов) · "
                f"последний раз {c['last_seen']:%Y-%m-%d}",
                "",
            ]
            card = c["card"]
            card = json.loads(card) if isinstance(card, str) else card
            signals = [
                dict(r)
                for r in await pool.fetch(
                    f"""
                    select s.id, s.evidence_quote, s.chat_id, {QUOTE_MESSAGE_SQL} as mid,
                           c.username, s.author_label, a.name a_name, a.company a_company, a.role a_role
                      from signals s join chats c on c.id = s.chat_id
                      left join authors a on a.author_label = s.author_label
                     where s.cluster_id = $1 order by s.intensity desc, s.id
                    """,
                    c["id"],
                )
            ]
            quote_lines = []
            for q in pick_quotes(card, signals):
                link = message_link(q["chat_id"], q["username"], q["mid"])
                who = people.display(q["a_name"], q["author_label"], q["a_company"],
                                     q["a_role"])
                tail = f" — {who}" + (f", [сообщение]({link})" if link else "")
                quote_lines.append(f"> «{q['evidence_quote']}»{tail}")
            if card:
                lines += [
                    f"**Кто:** {card['who']}",
                    "",
                    f"**Когда возникает:** {card['when']}",
                    "",
                    "**Как выкручиваются сейчас:**",
                    *[f"- {w}" for w in card["current_workarounds"]],
                    "",
                ]
            if quote_lines:
                lines += ["**Цитаты:**", *[q + "\n" for q in quote_lines]]
            if card:
                lines += [
                    "**Гипотезы решения:**",
                    *[f"- {h}" for h in card["product_hypotheses"]],
                    "",
                    "**Что выяснить:**",
                    *[f"- {q}" for q in card["open_questions"]],
                    "",
                ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
