"""PDF еженедельного отчёта «Отклик на темы» — на стилях ежедневного (report/pdf.py)."""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import clock
from ..bot import format as fmt
from .pdf import (
    ACCENT,
    FONT,
    HAIR,
    MUTED,
    WARN_BAR,
    WARN_BG,
    Tiles,
    _a,
    _card,
    _num,
    _styles,
    _t,
    _table,
    bar_chart,
)
from .weekly import _focus_title, _n, _who, reactions_line


def _posts(posts: list[dict[str, Any]], st, width: float, author: bool) -> Table:
    """Посты строками: дата · тема · отклик, суть, начало текста, ссылка."""
    rows = []
    for p in posts:
        meta = f"{p['ts'].astimezone(clock.tz()):%d.%m.%Y} · {p['theme'] or 'без темы'}"
        if author:
            meta += f" · {_who(p)}"
        cell = [
            Paragraph(_t(meta), st["meta"]),
            Paragraph(_t(p["summary"] or "") or _t((p["text"] or "")[:200]), st["body"]),
        ]
        excerpt = (p["text"] or "").strip().replace("\n", " ")
        if excerpt:
            cell.append(Paragraph("«" + _t(excerpt[:280] + ("…" if len(excerpt) > 280 else ""))
                                  + "»", st["quote"]))
        line = f"<b>{_t(reactions_line(p, emoji=False))}</b>"
        if p.get("link"):
            line += " · " + _a("открыть пост", p["link"])
        cell.append(Paragraph(line, st["meta"]))
        rows.append([cell])
    t = Table(rows, colWidths=[width])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, HAIR),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _themes(themes: list[dict[str, Any]], st, width: float, limit: int) -> Table:
    body = [[Paragraph(_t(t["theme"]), st["cell"]), _num(st, t["posts"]),
             _num(st, t["reactions"]), _num(st, round(t["avg"])), _num(st, t["comments"])]
            for t in themes[:limit]]
    return _table(["Тема", "Постов", "Реакций", "В среднем", "Комментариев"], body,
                  [width * 0.44] + [width * 0.14] * 4, st)


def render(data: dict[str, Any], notes: dict[str, Any] | None = None,
           bot_username: str | None = None) -> bytes:
    notes = notes or {}
    st = _styles()
    width = A4[0] - 36 * mm - 12
    end = data["now"].astimezone(clock.tz())
    focus = _focus_title(data)

    story: list[Any] = [
        Paragraph(_t("ОТКЛИК НА ТЕМЫ · " + (data.get("chat_title") or "").upper()),
                  st["overline"]),
        Paragraph("Еженедельный отчёт", st["title"]),
        Paragraph(_t(f"Тема «{data.get('topic_title') or ''}» · неделя по {end:%d.%m.%Y} · "
                     "реакции на посты за неделю, месяц и полгода"), st["subtitle"]),
        Spacer(1, 14),
    ]
    if notes.get("problem"):
        story.append(_warn([notes["problem"]], st, width))
        return _build(story, end)

    week = data["periods"][0]
    avg = week["reactions"] / week["posts"] if week["posts"] else 0
    story.append(Tiles([
        (fmt.fmt_num(week["posts"]), "постов за неделю"),
        (fmt.fmt_num(week["reactions"]), "реакций за неделю"),
        (f"{avg:.0f}", "в среднем на пост"),
        (fmt.fmt_num(week["focus_count"]), "постов автора в фокусе"),
    ]))

    if data.get("summary") or data.get("focus") or data.get("others"):
        rows: list[Any] = [Paragraph("Главное", st["card_title"]), Spacer(1, 4)]
        if data.get("summary"):
            rows += [Paragraph(_t(data["summary"]), st["body"]), Spacer(1, 6)]
        if data.get("focus"):
            rows.append(Paragraph(_t(focus) + ":", st["meta_b"]))
            rows += [Paragraph(_t(x), st["bullet"], bulletText="•") for x in data["focus"]]
        if data.get("others"):
            rows.append(Paragraph("Другие участники:", st["meta_b"]))
            rows += [Paragraph(_t(x), st["bullet"], bulletText="•") for x in data["others"]]
        story += [Spacer(1, 14), _card(rows, width, bar=ACCENT)]
    if data.get("ideas"):
        rows = [Paragraph("Идеи для постов", st["card_title"]),
                Paragraph("Темы, которые стоит развивать, — из того, что откликается.",
                          st["meta"]), Spacer(1, 4)]
        rows += [Paragraph(_t(x), st["bullet"], bulletText="•") for x in data["ideas"]]
        story += [Spacer(1, 10), _card(rows, width)]

    # автор в фокусе — первым
    story.append(Paragraph(_t(focus), st["h2"]))
    if not data.get("focus_names"):
        story.append(Paragraph("Автор в фокусе не найден среди тех, кто писал в теме. "
                               "Проверь WEEKLY_FOCUS в .env.", st["lead"]))
    else:
        story.append(Paragraph("Посты автора в фокусе: какие темы откликаются сильнее всего. "
                               "Темы отсортированы по реакциям в среднем на пост.", st["lead"]))
        for period in data["periods"]:
            head = (f"За {period['title']} · {_n(period['focus_count'], 'пост', 'поста', 'постов')}"
                    f" · {_n(period['focus_reactions'], 'реакция', 'реакции', 'реакций')}")
            block: list[Any] = [Paragraph(_t(head), st["h3"])]
            if not period["focus_count"]:
                story += block + [Paragraph("Постов за этот срок нет.", st["small"])]
                continue
            if period["key"] == "half" and len(period["focus_themes"]) > 1:
                block += [Paragraph("Реакций в среднем на пост по темам", st["lead"]),
                          bar_chart([(t["theme"], round(t["avg"]))
                                     for t in period["focus_themes"][:10]], width),
                          Spacer(1, 8)]
            block.append(_themes(period["focus_themes"], st, width, 12))
            story.append(KeepTogether(block))
            label = ParagraphStyle("label", st["meta_b"], keepWithNext=1, spaceBefore=6)
            story += [Paragraph("Лучшие посты:", label),
                      _posts(period["focus_posts"], st, width, author=False)]

    # другие участники
    story.append(Paragraph("Другие участники", st["h2"]))
    story.append(Paragraph("Посты, которые откликаются не хуже типичного поста автора в "
                           "фокусе, и темы остальных участников.", st["lead"]))
    for period in data["periods"]:
        story.append(Paragraph(_t(f"За {period['title']} · "
                                  f"{_n(period['other_count'], 'пост', 'поста', 'постов')}"),
                               st["h3"]))
        if period["other_posts"]:
            story.append(_posts(period["other_posts"], st, width, author=True))
        else:
            story.append(Paragraph("Ничего заметного.", st["small"]))
        if period["other_themes"]:
            story += [Spacer(1, 6), _themes(period["other_themes"], st, width, 8)]

    story += [Spacer(1, 14), _card([
        Paragraph("Как считается", st["meta_b"]),
        Paragraph(_t("Пост — длинное сообщение, в котором автор раскрывает тему; это решает "
                     "нейросеть по содержанию. Короткие ответы под постом — комментарии. "
                     "Интерес темы — реакции на её посты: в среднем на пост и всего. Свежие "
                     "посты ещё набирают реакции, поэтому за неделю цифры ниже, чем за полгода. "
                     "Реакции обновляются у постов за последний месяц; у более старых — "
                     "цифры на момент последнего прочтения."),
                  st["small"]),
    ], width)]
    if notes.get("warnings"):
        story += [Spacer(1, 10), _warn(notes["warnings"], st, width)]
    return _build(story, end)


def _warn(lines: list[str], st, width: float) -> Table:
    card = _card([Paragraph(_t(x), st["small"]) for x in lines], width, bar=WARN_BAR)
    card.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG)]))
    return card


def _build(story: list[Any], end) -> bytes:
    footer = f"Отклик на темы · неделя по {end:%d.%m.%Y}"

    def decorate(canvas, doc):
        canvas.saveState()
        if doc.page == 1:
            canvas.setFillColor(ACCENT)
            canvas.rect(0, A4[1] - 5, A4[0], 5, stroke=0, fill=1)
        canvas.setStrokeColor(HAIR)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
        canvas.setFont(FONT, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 9 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"стр. {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
        bottomMargin=20 * mm, title=f"Отклик на темы · {end:%d.%m.%Y}", author="chat-parser",
        subject="Еженедельный отчёт: какие темы откликаются",
    )
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
