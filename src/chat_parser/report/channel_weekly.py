"""Короткий PDF со сводкой подтверждённых сигналов по каналам."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

import asyncpg
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .. import clock
from ..bot.format import messages_word
from ..links import message_link
from .pdf import FONT, INK2, _a, _styles, _t

SIGNALS_PER_CHANNEL = 3


@dataclass
class ChannelWeeklyReport:
    since: datetime
    until: datetime
    channels: list[dict[str, Any]]
    analysis_note: str = ""

    @property
    def filename(self) -> str:
        return f"channels-weekly-{self.until.astimezone(clock.tz()):%Y-%m-%d}.pdf"

    @property
    def subject(self) -> str:
        return "Еженедельный отчёт по каналам"

    def pdf(self) -> bytes:
        styles = _styles()
        quote_style = ParagraphStyle("channel_quote", styles["quote"],
                                     leftIndent=styles["bullet"].leftIndent, spaceAfter=5)
        since, until = self.since.astimezone(clock.tz()), self.until.astimezone(clock.tz())
        story = [Paragraph(self.subject, styles["title"]), Spacer(1, 5),
                 Paragraph(f"{since:%d.%m.%Y %H:%M} - {until:%d.%m.%Y %H:%M}",
                           styles["subtitle"]), Spacer(1, 12)]
        if self.analysis_note:
            story.append(Paragraph(_t(self.analysis_note), styles["meta"]))
        if not self.channels:
            story.append(Paragraph("Каналы ещё не добавлены.", styles["body"]))
        for channel in self.channels:
            title = channel["title"] or channel["username"] or f"Канал {channel['id']}"
            status = messages_word(channel["messages"])
            if channel.get("status"):
                status += " · " + channel["status"]
            elif channel["messages"] and not channel["signals"]:
                status += " · подтверждённых сигналов нет"
            section = [Paragraph(_t(title), styles["h2"]),
                       Paragraph(_t(status), styles["meta"]), Spacer(1, 5)]
            for signal in channel["signals"]:
                summary = _t(_excerpt(signal["summary"]))
                quote = _t(_excerpt(signal["evidence_quote"]))
                section.append(Paragraph(f"<b>{summary}</b>", styles["bullet"], bulletText="•"))
                section.append(Paragraph(f"«{quote}»" + (
                    " · " + _a("Источник", signal["link"]) if signal["link"] else ""
                ), quote_style))
            story.append(KeepTogether(section))

        def page_number(canvas, doc):
            canvas.setFont(FONT, 8)
            canvas.setFillColor(INK2)
            canvas.drawRightString(A4[0] - 20 * mm, 13 * mm, str(doc.page))

        out = BytesIO()
        doc = SimpleDocTemplate(out, pagesize=A4, title=self.subject, leftMargin=20 * mm,
                                rightMargin=20 * mm, topMargin=17 * mm, bottomMargin=19 * mm)
        doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
        return out.getvalue()


def _excerpt(text: str, limit: int = 200) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit].rstrip() + "…"


async def collect(pool: asyncpg.Pool, since: datetime, until: datetime) -> ChannelWeeklyReport:
    records = await pool.fetch(
        """
        select c.id, c.title, c.username, c.is_active, coalesce(m.n, 0) messages
          from chats c
          left join (
             select chat_id, count(*) n from messages where ts >= $1 and ts < $2
              and not is_bot group by chat_id
          ) m on m.chat_id = c.id
         order by lower(coalesce(c.title, c.username, '')), c.id
        """,
        since, until,
    )
    channels = {row["id"]: {**row, "signals": []} for row in records}
    signals = await pool.fetch(
        """
        with source_signals as (
            select distinct on (s.chat_id, lower(s.summary)) s.chat_id, s.id, s.summary,
                   s.evidence_quote, s.intensity, s.confidence, m.message_id, m.ts
              from signals s join messages m
                on m.chat_id = s.chat_id and m.message_id = s.message_ids[1]
             where m.ts >= $1 and m.ts < $2 and not m.is_bot and s.confidence >= 0.5
               and position(lower(regexp_replace(trim(s.evidence_quote), '\\s+', ' ', 'g'))
                            in lower(regexp_replace(m.text, '\\s+', ' ', 'g'))) > 0
             order by s.chat_id, lower(s.summary), s.intensity desc nulls last,
                      s.confidence desc, m.ts desc, s.id
        ), ranked as (
            select *, row_number() over (partition by chat_id order by intensity desc nulls last,
                       confidence desc, ts desc, id) n from source_signals
        )
        select * from ranked where n <= $3 order by chat_id, n
        """,
        since, until, SIGNALS_PER_CHANNEL,
    )
    for row in signals:
        channel = channels.get(row["chat_id"])
        if channel is not None:
            channel["signals"].append({**row, "link": message_link(
                channel["id"], channel["username"], row["message_id"],
            )})
    return ChannelWeeklyReport(since, until, list(channels.values()))
