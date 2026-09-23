"""Рендер сообщений бота. Telegram режет сообщения на 4096 символах."""

from __future__ import annotations

import html
import json
from typing import Any

LIMIT = 3800

AUDIENCE_RU = {
    "owner": "владельцы оптик",
    "staff": "персонал салонов",
    "optometrist": "оптометристы",
    "supplier": "поставщики",
    "customer": "покупатели",
    "unknown": "не определено",
}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Режет по строкам, чтобы не рвать разметку посреди тега."""
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


def fmt_status(chats: list[Any], pending: int, last_run: Any) -> str:
    lines = ["<b>Состояние</b>", ""]
    if not chats:
        lines.append("Чатов нет. Добавь: /addchat &lt;ссылка&gt;")
    for c in chats:
        flag = "✅" if c["backfill_done"] else "⏳"
        when = f"обновлён {c['last_run']:%d.%m %H:%M}" if c["last_run"] else "ещё не синхронизирован"
        lines.append(f"{flag} <b>{esc(c['title'])}</b>")
        lines.append(f"   {c['msgs']} сообщ. · {when}")
        if c["retry_after"]:
            lines.append(f"   ⛔ флуд-пауза до {c['retry_after']:%d.%m %H:%M}")
    lines += ["", f"Тредов в очереди на анализ: <b>{pending}</b>"]
    if last_run:
        lines.append(f"Последний полный прогон: {last_run:%d.%m %H:%M} UTC")
    return "\n".join(lines)


def fmt_top(clusters: list[Any]) -> str:
    if not clusters:
        return "Кластеров пока нет. Сначала /run."
    lines = ["<b>Топ болей</b>", ""]
    current = None
    for c in clusters:
        if c["audience"] != current:
            current = c["audience"]
            lines.append(f"\n<u>{AUDIENCE_RU.get(current, current)}</u>")
        lines.append(
            f"<b>#{c['id']}</b> {esc(c['label'])}\n"
            f"   вес {c['score']:.1f} · {c['n_authors']} чел. · "
            f"{c['n_chats']} чат. · {c['n_signals']} сигн."
        )
    lines.append("\nПодробно: /pain &lt;номер&gt;")
    return "\n".join(lines)


def fmt_card(cluster: Any, quotes: list[Any]) -> str:
    lines = [
        f"<b>#{cluster['id']} {esc(cluster['label'])}</b>",
        f"<i>{AUDIENCE_RU.get(cluster['audience'], cluster['audience'])}</i>",
        "",
        f"<blockquote>{esc(cluster['statement'])}</blockquote>",
        "",
        f"вес {cluster['score']:.1f} · {cluster['n_authors']} чел. · "
        f"{cluster['n_chats']} чат. · {cluster['n_signals']} сигн.",
    ]
    card = cluster["card"]
    if card:
        card = json.loads(card) if isinstance(card, str) else card
        lines += ["", f"<b>Кто:</b> {esc(card['who'])}", f"<b>Когда:</b> {esc(card['when'])}"]
        if card.get("current_workarounds"):
            lines.append("\n<b>Как выкручиваются:</b>")
            lines += [f"• {esc(w)}" for w in card["current_workarounds"]]
        if card.get("evidence"):
            lines.append("\n<b>Цитаты:</b>")
            lines += [f"<blockquote>{esc(q)}</blockquote>" for q in card["evidence"]]
        if card.get("product_hypotheses"):
            lines.append("\n<b>Гипотезы:</b>")
            lines += [f"• {esc(h)}" for h in card["product_hypotheses"]]
        if card.get("open_questions"):
            lines.append("\n<b>Выяснить интервью:</b>")
            lines += [f"• {esc(q)}" for q in card["open_questions"]]
    elif quotes:
        lines.append("\n<b>Цитаты:</b>")
        lines += [f"<blockquote>{esc(q['evidence_quote'])}</blockquote>" for q in quotes]
    return "\n".join(lines)


def fmt_signals(rows: list[Any]) -> str:
    if not rows:
        return "Сигналов пока нет."
    lines = ["<b>Последние сигналы</b>", ""]
    for r in rows:
        lines.append(
            f"<b>{esc(r['type'])}</b> · {AUDIENCE_RU.get(r['audience'], r['audience'])}"
            f" · острота {r['intensity']}\n"
            f"{esc(r['summary'])}\n"
            f"<blockquote>{esc(r['evidence_quote'])}</blockquote>"
        )
    return "\n".join(lines)


def fmt_digest(stats: dict[str, Any], new_signals: int, new_msgs: int, top: list[Any]) -> str:
    lines = [
        "<b>Дайджест</b>",
        "",
        f"Новых сообщений: <b>{new_msgs}</b>",
        f"Новых сигналов: <b>{new_signals}</b>",
    ]
    ext = stats.get("extract") or {}
    if ext.get("drop_rate", 0) > 0.05:
        lines.append(
            f"⚠️ отбраковка цитат {ext['drop_rate']:.0%} — промпт стоит поправить"
        )
    if top:
        lines.append("\n<b>Топ болей сейчас</b>")
        for c in top:
            lines.append(
                f"<b>#{c['id']}</b> {esc(c['label'])} — вес {c['score']:.1f}, "
                f"{c['n_authors']} чел."
            )
        lines.append("\nПодробно: /pain &lt;номер&gt; · весь отчёт: /report")
    return "\n".join(lines)
