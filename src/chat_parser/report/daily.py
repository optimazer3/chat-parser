"""Дневной отчёт по чатам — одно сообщение в конце дня.

Что попадает: сводка по каждому чату, о чём говорили, новые боли, острые
сигналы (4–5 из 5) и всплески известных болей. Всё остальное молча копится
в базе. Токены и прочая техника в отчёт не попадают.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from .. import clock
from ..bot import format as fmt
from ..config import settings
from ..links import QUOTE_MESSAGE_SQL, chat_link, message_link
from ..quotes import pick_quotes

TOP_N = 5
CAPTION_LIMIT = 1024  # подпись к файлу в Telegram

log = logging.getLogger("chat_parser.report")


@dataclass
class DailyReport:
    """Готовые итоги дня: данные и все их представления."""

    data: dict[str, Any]
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def day(self) -> datetime:
        return self.data["day"]

    @property
    def text(self) -> str:
        """Полный текст для Telegram — если PDF не собрался."""
        return render(self.data, self.notes)

    @property
    def caption(self) -> str:
        return caption(self.data, self.notes)

    @property
    def filename(self) -> str:
        return f"itogi-dnya-{self.day:%Y-%m-%d}.pdf"

    @property
    def subject(self) -> str:
        return f"Итоги дня · {self.day:%d.%m.%Y} — мониторинг чатов оптики"

    def pdf(self, bot_username: str | None = None) -> bytes | None:
        """PDF или None, если собрать не получилось (отчёт уйдёт текстом)."""
        from . import pdf

        try:
            return pdf.render(self.data, self.notes, bot_username)
        except Exception:  # noqa: BLE001 — без PDF отчёт всё равно должен дойти
            log.exception("PDF итогов дня не собрался")
            return None

    def plain(self) -> str:
        return plain(self.data, self.notes)


async def collect(
    pool: asyncpg.Pool, since: datetime, until: datetime, fresh: datetime
) -> dict[str, Any]:
    """since/until — период отчёта (с прошлого отчёта); сутки для всплесков —
    с местной полуночи. fresh — сигналы из разговоров не старше этого момента:
    архив, разобранный вручную в тот же день, в итоги дня не попадает."""
    day = clock.day_start(until)
    chats = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.title, c.username, c.link,
               (select count(*) from messages m
                 where m.chat_id = c.id and m.ts >= $1 and m.ts < $2) msgs,
               (select count(*) from threads t
                 where t.chat_id = c.id and t.started_at >= $1 and t.started_at < $2) threads,
               (select count(*) from signals s
                 where s.chat_id = c.id and s.created_at >= $1 and s.created_at < $2
                   and s.ts >= $3) signals,
               (select max(message_id) from messages m where m.chat_id = c.id) last_msg
          from chats c where c.is_active
         order by msgs desc, c.id
        """,
        since, until, fresh,
    )]

    new_pains = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.label, c.audience, c.n_authors, c.n_signals
          from clusters c
         where c.created_at >= $1 and c.first_seen >= $2
         order by c.n_authors desc, c.n_signals desc, c.id
         limit $3
        """,
        since, fresh, TOP_N,
    )]
    for p in new_pains:
        p["quote"] = await _best_quote(pool, p["id"])
    new_ids = {p["id"] for p in new_pains}

    sharp = [dict(r) for r in await pool.fetch(
        f"""
        select s.id, s.type, s.audience, s.summary, s.evidence_quote, s.intensity,
               s.cluster_id, s.chat_id, {QUOTE_MESSAGE_SQL} as mid, ch.username,
               s.author_label, a.name a_name, a.company a_company, a.role a_role,
               cl.label pain_label
          from signals s join chats ch on ch.id = s.chat_id
          left join authors a on a.author_label = s.author_label
          left join clusters cl on cl.id = s.cluster_id
         where s.created_at >= $1 and s.created_at < $2 and s.ts >= $3
           and s.intensity >= 4 and s.confidence >= 0.5
         order by s.intensity desc, s.id
         limit $4
        """,
        since, until, fresh, TOP_N,
    )]
    for s in sharp:
        s["link"] = message_link(s["chat_id"], s["username"], s["mid"])

    spikes = []
    for r in await pool.fetch(
        """
        select c.id, c.label,
               count(*) filter (where s.ts >= $1) today,
               count(*) filter (where s.ts >= $1 - interval '30 days' and s.ts < $1) prev
          from clusters c join signals s on s.cluster_id = c.id
         group by c.id
        having count(*) filter (where s.ts >= $1) >= $2
        """,
        day, settings.spike_min,
    ):
        per_day = r["prev"] / 30
        if r["id"] not in new_ids and r["today"] >= settings.spike_factor * per_day:
            spikes.append({**dict(r), "per_day": per_day})
    spikes.sort(key=lambda x: -x["today"])

    topics = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.label, count(*) n
          from signals s join clusters c on c.id = s.cluster_id
         where s.created_at >= $1 and s.created_at < $2 and s.ts >= $3
         group by c.id, c.label order by n desc, c.id limit $4
        """,
        since, until, fresh, TOP_N,
    )]
    types = {r["type"]: r["n"] for r in await pool.fetch(
        "select type, count(*) n from signals where created_at >= $1 and created_at < $2 "
        "and ts >= $3 group by type order by n desc",
        since, until, fresh,
    )}
    return {"day": day, "chats": chats, "new_pains": new_pains, "sharp": sharp,
            "spikes": spikes[:TOP_N], "topics": topics, "types": types}


async def _best_quote(pool: asyncpg.Pool, cluster_id: int) -> dict[str, Any] | None:
    rows = [dict(r) for r in await pool.fetch(
        f"""
        select s.id, s.evidence_quote, s.chat_id, {QUOTE_MESSAGE_SQL} as mid, ch.username,
               s.author_label, a.name a_name, a.company a_company, a.role a_role
          from signals s join chats ch on ch.id = s.chat_id
          left join authors a on a.author_label = s.author_label
         where s.cluster_id = $1 order by s.intensity desc, s.id
        """,
        cluster_id,
    )]
    picked = pick_quotes(None, rows, limit=1)
    if not picked:
        return None
    q = picked[0]
    q["link"] = message_link(q["chat_id"], q["username"], q["mid"])
    return q


def usually(per_day: float) -> str:
    if per_day >= 1:
        return f"обычно ~{per_day:.0f} в день"
    per_week = per_day * 7
    if per_week >= 0.5:
        return f"обычно ~{max(1, round(per_week))} в неделю"
    return "раньше почти не упоминалась"


def render(data: dict[str, Any], notes: dict[str, Any] | None = None) -> str:
    notes = notes or {}
    esc = fmt.esc
    lines = [f"📊 <b>Итоги дня · {data['day']:%d.%m}</b>", ""]

    lines.append("<b>Чаты</b>")
    if not data["chats"]:
        lines.append("Подключённых чатов нет — добавь их кнопкой ➕ Добавить чат.")
    for c in data["chats"]:
        link = chat_link(c["id"], c["username"], c["last_msg"], c["link"])
        title = esc(c["title"] or "без названия")
        name = f'<a href="{fmt.html.escape(link, quote=True)}">{title}</a>' if link else title
        if not c["msgs"]:
            lines.append(f"• {name}: сегодня тишина")
        else:
            lines.append(
                f"• {name}: {fmt.messages_word(c['msgs'])}, "
                f"{fmt.discussions(c['threads'])}, {fmt.signals_word(c['signals'])}"
            )

    total = sum(data["types"].values())
    if data.get("highlights"):
        lines += ["", "🧭 <b>Главное за день</b>"]
        lines += [f"• {esc(h)}" for h in data["highlights"]]
    if data["topics"]:
        lines += ["", "<b>О чём говорили</b>"]
        lines += [f"• {esc(t['label'])} — {fmt.signals_word(t['n'])} → /pain_{t['id']}"
                  for t in data["topics"]]

    if data["new_pains"]:
        lines += ["", "🆕 <b>Новые боли</b>"]
        for p in data["new_pains"]:
            lines.append(
                f"• <b>{esc(p['label'])}</b> — {fmt.people_word(p['n_authors'])}, "
                f"{fmt.signals_word(p['n_signals'])} → /pain_{p['id']}"
            )
            if p.get("quote"):
                lines.append(fmt.fmt_quote(p["quote"]))

    if data["sharp"]:
        lines += ["", "🔥 <b>Острые сигналы</b>"]
        for s in data["sharp"]:
            kind = fmt.TYPE_RU.get(s["type"], s["type"])
            who = fmt.AUDIENCE_RU.get(s["audience"], s["audience"])
            pain = f" → /pain_{s['cluster_id']}" if s["cluster_id"] else ""
            lines.append(f"• <b>{esc(kind)}</b> · {who} · острота {s['intensity']} из 5{pain}")
            lines.append(f"  {esc(s['summary'])}")
            lines.append(fmt.fmt_quote(s))

    if data["spikes"]:
        lines += ["", "📈 <b>Всплески</b>"]
        lines += [
            f"• {esc(x['label'])} — сегодня {x['today']}, {usually(x['per_day'])} "
            f"→ /pain_{x['id']}"
            for x in data["spikes"]
        ]

    lines.append("")
    if total:
        parts = [f"{fmt.TYPE_RU.get(k, k)} {v}" for k, v in data["types"].items()]
        lines.append(f"Всего за день: {fmt.signals_word(total)} — " + ", ".join(parts) + ".")
    else:
        msgs = sum(c["msgs"] for c in data["chats"])
        lines.append(f"Новых сигналов нет. Сообщений за день: {msgs}.")

    if notes.get("paused"):
        lines.append("\n⏸ Разбор на паузе: переписку я собрал, но нейросеть не запускал. "
                     "Включить — /live")
    if notes.get("budget_hit_at"):
        lines.append(
            f"\n⚠️ Дневной лимит на автоматический разбор исчерпан в "
            f"{notes['budget_hit_at']:%H:%M} — разобрано не всё. Остальное можно "
            "разобрать вручную: /extract"
        )
    for err in notes.get("errors", []):
        lines.append(f"\n⚠️ {esc(err)}")
    if notes.get("backlog"):
        lines.append(
            f"\nВ архиве ждут ручного разбора: {fmt.discussions(notes['backlog'])} "
            "(автоматически разбираются только свежие) — /extract"
        )
    return "\n".join(lines)



def _totals(data: dict[str, Any]) -> tuple[int, int, int]:
    return (sum(c["msgs"] for c in data["chats"]), sum(c["threads"] for c in data["chats"]),
            sum(data["types"].values()))


def caption(data: dict[str, Any], notes: dict[str, Any] | None = None) -> str:
    """Коротко — подпись к PDF в Telegram: цифры, главное, новые боли с
    нажимаемыми /pain_…. Не длиннее лимита Telegram."""
    notes = notes or {}
    esc = fmt.esc
    msgs, threads, total = _totals(data)
    lines = [
        f"📊 <b>Итоги дня · {data['day']:%d.%m}</b>",
        f"{fmt.messages_word(msgs)} · {fmt.discussions(threads)} · "
        f"{fmt.signals_word(total)}",
    ]
    optional = []
    if data.get("highlights"):
        optional.append("")
        optional += [f"🧭 {esc(h)}" for h in data["highlights"][:3]]
    if data["new_pains"]:
        optional.append("")
        optional += [f"🆕 {esc(p['label'])} → /pain_{p['id']}" for p in data["new_pains"]]
    extra = []
    if data["sharp"]:
        extra.append(f"🔥 острых сигналов: {len(data['sharp'])}")
    if data["spikes"]:
        extra.append(f"📈 всплесков: {len(data['spikes'])}")
    if extra:
        optional += ["", " · ".join(extra)]
    if notes.get("paused"):
        optional.append("⏸ разбор на паузе — /live")
    if notes.get("budget_hit_at"):
        optional.append(f"⚠️ лимит исчерпан в {notes['budget_hit_at']:%H:%M} — /extract")
    if notes.get("errors"):
        optional.append(f"⚠️ не прочитано чатов: {len(notes['errors'])} — подробности в файле")
    tail = "\nПодробности — в файле."
    for line in optional:  # что не влезает в подпись — остаётся только в файле
        if _visible(lines + [line]) + len(tail) > CAPTION_LIMIT - 24:
            break
        lines.append(line)
    return "\n".join(lines) + tail


def _visible(lines: list[str]) -> int:
    return len(re.sub(r"<[^>]+>", "", "\n".join(lines)))


def plain(data: dict[str, Any], notes: dict[str, Any] | None = None) -> str:
    """Текст письма: коротко, остальное — во вложенном PDF."""
    notes = notes or {}
    msgs, threads, total = _totals(data)
    lines = [
        f"Итоги дня · {data['day']:%d.%m.%Y}",
        "",
        f"{fmt.messages_word(msgs)}, {fmt.discussions(threads)}, "
        f"{fmt.signals_word(total)}, новых болей: {len(data['new_pains'])}.",
    ]
    if data.get("highlights"):
        lines += ["", "Главное за день:"] + [f"• {h}" for h in data["highlights"]]
    if data["new_pains"]:
        lines += ["", "Новые боли:"] + [f"• {p['label']}" for p in data["new_pains"]]
    if notes.get("paused"):
        lines += ["", "Разбор был на паузе — нейросеть не запускалась."]
    if notes.get("budget_hit_at"):
        lines += ["", f"Дневной лимит исчерпан в {notes['budget_hit_at']:%H:%M} — "
                      "разобрано не всё."]
    lines += ["", "Полный отчёт — во вложении (PDF).", "",
              "— chat-parser, мониторинг чатов рынка оптики"]
    return "\n".join(lines)
