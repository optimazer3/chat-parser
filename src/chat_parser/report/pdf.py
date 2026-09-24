"""Итоги дня в PDF: то же содержание, что в сообщении бота, свёрстанное для
чтения, печати и пересылки.

Шрифт PT Sans лежит рядом (fonts/, лицензия OFL) — на Windows ничего ставить
не нужно. Символы, которых в шрифте нет (эмодзи в цитатах), выбрасываются:
иначе вместо них были бы пустые квадраты.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .. import clock, people
from ..bot import format as fmt

FONT_DIR = Path(__file__).parent / "fonts"
FONT, BOLD, ITALIC = "PTSans", "PTSans-Bold", "PTSans-Italic"

# Светлая тема для печати: чернила, линии и один синий для данных
INK = colors.HexColor("#0b0b0b")
INK2 = colors.HexColor("#52514e")
MUTED = colors.HexColor("#898781")
HAIR = colors.HexColor("#e1e0d9")
AXIS = colors.HexColor("#c3c2b7")
CARD = colors.HexColor("#f4f3ef")
ACCENT = colors.HexColor("#2a78d6")
LINK = "#1c5cab"
# острота — цвета статусов, всегда вместе с подписью словами
SEVERITY = {5: ("#d03b3b", "#ffffff"), 4: ("#ec835a", "#0b0b0b")}
WARN_BG = colors.HexColor("#fbeee6")
WARN_BAR = colors.HexColor("#ec835a")

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
REPLACE = {"→": "›", "↗": "›", "←": "‹", "✓": "•", "✅": "", "‍": "", "️": ""}


@lru_cache(maxsize=1)
def _glyphs() -> frozenset[int]:
    """Зарегистрировать шрифты; вернуть символы, которые в них есть."""
    for name, file in ((FONT, "PTSans-Regular.ttf"), (BOLD, "PTSans-Bold.ttf"),
                       (ITALIC, "PTSans-Italic.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=BOLD, italic=ITALIC,
                                  boldItalic=BOLD)
    return frozenset(pdfmetrics.getFont(FONT).face.charToGlyph)


def clean(text: Any) -> str:
    """Текст без символов, которых нет в шрифте, и без лишних пробелов."""
    glyphs = _glyphs()
    out = []
    for ch in str(text or ""):
        ch = REPLACE.get(ch, ch)
        if ch and (ch in "\n\t " or ord(ch) in glyphs):
            out.append(ch)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def _t(text: Any) -> str:
    """Для разметки Paragraph: почищенный и экранированный текст."""
    return escape(clean(text)).replace("\n", "<br/>")


def _a(text: str, href: str | None, color: str = LINK) -> str:
    """text — уже экранированная разметка."""
    if not href:
        return text
    return f'<a href="{escape(href, {chr(34): "&quot;"})}" color="{color}">{text}</a>'


def _styles() -> dict[str, ParagraphStyle]:
    _glyphs()
    base = ParagraphStyle("body", fontName=FONT, fontSize=10, leading=14, textColor=INK)
    return {
        "overline": ParagraphStyle("overline", base, fontName=BOLD, fontSize=8, leading=10,
                                   textColor=MUTED),
        "title": ParagraphStyle("title", base, fontName=BOLD, fontSize=26, leading=30,
                                spaceBefore=4),
        "subtitle": ParagraphStyle("subtitle", base, fontSize=10.5, leading=14,
                                   textColor=INK2),
        # заголовок раздела не остаётся внизу страницы без содержимого
        "h2": ParagraphStyle("h2", base, fontName=BOLD, fontSize=13.5, leading=17,
                             spaceBefore=16, spaceAfter=7, keepWithNext=1),
        "lead": ParagraphStyle("lead", base, fontSize=8.8, leading=12, textColor=INK2,
                               spaceAfter=7, keepWithNext=1),
        "body": base,
        "bullet": ParagraphStyle("bullet", base, leftIndent=11, bulletIndent=0,
                                 spaceAfter=3),
        "card_title": ParagraphStyle("card_title", base, fontName=BOLD, fontSize=11.5,
                                     leading=15),
        "meta": ParagraphStyle("meta", base, fontSize=8.8, leading=12, textColor=INK2),
        "quote": ParagraphStyle("quote", base, fontName=ITALIC, fontSize=10, leading=14),
        "cell": ParagraphStyle("cell", base, fontSize=9.5, leading=12.5),
        "cell_head": ParagraphStyle("cell_head", base, fontSize=8, leading=10,
                                    textColor=MUTED),
        "small": ParagraphStyle("small", base, fontSize=8.8, leading=12, textColor=INK2),
    }


# ------------------------------------------------------------ элементы


class Tiles(Flowable):
    """Ряд плиток «число + подпись» — главные цифры дня."""

    HEIGHT = 62

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__()
        self.items = items

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return avail_width, self.HEIGHT

    def draw(self):
        c, gap = self.canv, 8
        w = (self.width - gap * (len(self.items) - 1)) / len(self.items)
        for i, (number, label) in enumerate(self.items):
            x = i * (w + gap)
            c.setFillColor(CARD)
            c.roundRect(x, 0, w, self.HEIGHT, 6, stroke=0, fill=1)
            c.setFillColor(INK)
            c.setFont(BOLD, 23)
            c.drawString(x + 12, 28, number)
            c.setFillColor(INK2)
            c.setFont(FONT, 8.8)
            c.drawString(x + 12, 12, label)


def _fit(text: str, width: float, font: str, size: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def bar_chart(rows: list[tuple[str, int]], width: float) -> Drawing:
    """Горизонтальные бары одного ряда: подпись слева, значение у конца бара.
    Бар тонкий, конец скруглён, у оси — прямой угол."""
    row_h, bar_h, size = 22, 10, 9.5
    label_w = width * 0.44
    area = width - label_w - 34
    top = max(v for _, v in rows) or 1
    height = row_h * len(rows)
    d = Drawing(width, height)
    for i, (label, value) in enumerate(rows):
        y = height - (i + 1) * row_h + (row_h - bar_h) / 2
        d.add(String(0, y + 1.6, _fit(clean(label), label_w - 10, FONT, size),
                     fontName=FONT, fontSize=size, fillColor=INK))
        w = max(3.0, area * value / top)
        d.add(Rect(label_w, y, w, bar_h, rx=3, ry=3, fillColor=ACCENT, strokeColor=None))
        d.add(Rect(label_w, y, min(w, 3), bar_h, fillColor=ACCENT, strokeColor=None))
        d.add(String(label_w + w + 6, y + 1.6, str(value), fontName=BOLD, fontSize=size,
                     fillColor=INK))
    d.add(Line(label_w, 0, label_w, height, strokeColor=AXIS, strokeWidth=0.75))
    return d


def _card(rows: list[Any], width: float, bar: colors.Color | None = None) -> Table:
    """Скруглённая плашка с содержимым в столбик."""
    t = Table([[r] for r in rows], colWidths=[width])
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), CARD),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
    ]
    if bar is not None:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 3, bar))
    t.setStyle(TableStyle(style))
    return t


def _quote(q: dict[str, Any], st: dict[str, ParagraphStyle], width: float,
           who_link) -> list[Any]:
    """Цитата с чертой слева и подпись: кто сказал и ссылка на сообщение."""
    text = Paragraph(f"«{_t(q.get('evidence_quote'))}»", st["quote"])
    block = Table([[text]], colWidths=[width - 24])
    block.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, -1), 2, AXIS),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    parts = []
    if q.get("author_label"):
        who = people.display(q.get("a_name"), q["author_label"], q.get("a_company"),
                             q.get("a_role"))
        parts.append(_a(_t(who), who_link(q["author_label"]), color="#52514e"))
    if q.get("link"):
        parts.append(_a("открыть сообщение в Telegram", q["link"]))
    rows: list[Any] = [Spacer(1, 3), block]
    if parts:
        rows.append(Paragraph("— " + " · ".join(parts), st["meta"]))
    return rows


def _table(head: list[str], body: list[list[Any]], widths: list[float],
           st: dict[str, ParagraphStyle], right_from: int = 1) -> Table:
    """Таблица без рамок: волосяные линии между строками, числа — вправо."""
    right = ParagraphStyle("head_r", st["cell_head"], alignment=2)
    data = [[Paragraph(_t(h), right if i >= right_from else st["cell_head"])
             for i, h in enumerate(head)]] + body
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, AXIS),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, HAIR),
        ("ALIGN", (right_from, 0), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _cards_flow(head: list[Any], i: int, card: Table) -> list[Any]:
    """Карточка не рвётся между страницами, а первая — ещё и держится вместе с
    заголовком раздела (keepWithNext с KeepTogether в ReportLab не сцепляется)."""
    if i == 0:
        return [KeepTogether(head + [card])]
    return [Spacer(1, 8), KeepTogether(card)]


def _num(st: dict[str, ParagraphStyle], value: int, muted: bool = False) -> Paragraph:
    style = ParagraphStyle("n", st["cell"], alignment=2,
                           textColor=MUTED if muted else INK)
    return Paragraph(fmt.fmt_num(value), style)


def _date_line(day: datetime) -> str:
    return f"{WEEKDAYS[day.weekday()].capitalize()}, {day.day} {MONTHS[day.month - 1]} {day.year}"


# ------------------------------------------------------------ документ


def render(data: dict[str, Any], notes: dict[str, Any] | None = None,
           bot_username: str | None = None) -> bytes:
    """PDF итогов дня. data — из report.daily.collect (+ highlights, since, until)."""
    notes = notes or {}
    st = _styles()
    day: datetime = data["day"]
    width = A4[0] - 36 * mm - 12  # у рамки страницы свои поля по 6 pt

    def bot_link(arg: str) -> str | None:
        return f"https://t.me/{bot_username}?start={arg}" if bot_username else None

    def who_link(label: str) -> str | None:
        return bot_link("who_" + label.removeprefix("u:"))

    story: list[Any] = [
        Paragraph("МОНИТОРИНГ ЧАТОВ · РЫНОК ОПТИКИ", st["overline"]),
        Paragraph("Итоги дня", st["title"]),
    ]
    period = _date_line(day)
    if data.get("since") and data.get("until"):
        since = data["since"].astimezone(clock.tz())
        until = data["until"].astimezone(clock.tz())
        period += (f" · с {since:%d.%m %H:%M} по {until:%d.%m %H:%M} "
                   f"({clock.tz_label()})")
    story += [Paragraph(_t(period), st["subtitle"]), Spacer(1, 14)]

    msgs = sum(c["msgs"] for c in data["chats"])
    threads = sum(c["threads"] for c in data["chats"])
    total = sum(data["types"].values())
    story.append(Tiles([
        (fmt.fmt_num(msgs), "сообщений"),
        (fmt.fmt_num(threads), "обсуждений"),
        (fmt.fmt_num(total), "сигналов"),
        (fmt.fmt_num(len(data["new_pains"])), "новых болей"),
    ]))

    if data.get("highlights"):
        rows: list[Any] = [Paragraph("Главное за день", st["card_title"]), Spacer(1, 4)]
        rows += [Paragraph(_t(h), st["bullet"], bulletText="•") for h in data["highlights"]]
        story += [Spacer(1, 14), _card(rows, width, bar=ACCENT)]

    # чаты
    story.append(Paragraph("Чаты", st["h2"]))
    if not data["chats"]:
        story.append(Paragraph("Подключённых чатов нет.", st["small"]))
    else:
        from ..links import chat_link

        body = []
        for c in data["chats"]:
            link = chat_link(c["id"], c["username"], c["last_msg"], c["link"])
            name = _a(_t(c["title"] or "без названия"), link)
            quiet = not c["msgs"]
            if quiet:
                name += ' <font color="#898781">· тишина</font>'
            body.append([Paragraph(name, st["cell"]), _num(st, c["msgs"], quiet),
                         _num(st, c["threads"], quiet), _num(st, c["signals"], quiet)])
        story.append(_table(["Чат", "Сообщений", "Обсуждений", "Сигналов"], body,
                            [width * 0.52, width * 0.16, width * 0.16, width * 0.16], st))

    # о чём говорили
    if data["topics"]:
        story += [
            Paragraph("О чём говорили", st["h2"]),
            Paragraph("Сигналов за день по болям", st["lead"]),
            bar_chart([(t["label"], t["n"]) for t in data["topics"]], width),
        ]

    # новые боли
    if data["new_pains"]:
        head = [Paragraph("Новые боли", st["h2"]),
                Paragraph("Раньше таких жалоб в чатах не было.", st["lead"])]
        for i, p in enumerate(data["new_pains"]):
            who = fmt.AUDIENCE_RU.get(p["audience"], p["audience"])
            meta = _t(f"{fmt.people_word(p['n_authors'])} · "
                      f"{fmt.signals_word(p['n_signals'])} · {who}")
            link = bot_link(f"pain_{p['id']}")
            if link:
                meta += " · " + _a("подробнее в боте", link)
            rows = [
                Paragraph(_a(_t(p["label"]), link, color="#0b0b0b"), st["card_title"]),
                Paragraph(meta, st["meta"]),
            ]
            if p.get("quote"):
                rows += _quote(p["quote"], st, width, who_link)
            story += _cards_flow(head, i, _card(rows, width))

    # острые сигналы
    if data["sharp"]:
        head = [Paragraph("Острые сигналы", st["h2"]),
                Paragraph("Острота 4–5 из 5: люди жалуются резко или срочно.", st["lead"])]
        for i, s in enumerate(data["sharp"]):
            bg, ink = SEVERITY.get(s["intensity"], SEVERITY[4])
            pad = "\u00a0"
            badge = (f'<font backColor="{bg}" color="{ink}">{pad * 2}острота '
                     f"{s['intensity']} из 5{pad * 2}</font>")
            kind = fmt.TYPE_RU.get(s["type"], s["type"])
            who = fmt.AUDIENCE_RU.get(s["audience"], s["audience"])
            rows = [Paragraph(f"{badge}{pad * 2} {_t(kind)} · {_t(who)}", st["meta"]),
                    Spacer(1, 4),
                    Paragraph(_t(s["summary"]), ParagraphStyle("s", st["body"], fontName=BOLD))]
            rows += _quote(s, st, width, who_link)
            if s.get("pain_label"):
                pain = _a(_t(s["pain_label"]), bot_link(f"pain_{s['cluster_id']}"))
                rows.append(Paragraph("Входит в боль: " + pain, st["meta"]))
            story += _cards_flow(head, i, _card(rows, width))

    # всплески
    if data["spikes"]:
        from .daily import usually

        story.append(Paragraph("Всплески", st["h2"]))
        story.append(Paragraph("Известные боли, о которых сегодня говорят заметно чаще "
                               "обычного.", st["lead"]))
        body = [[Paragraph(_a(_t(x["label"]), bot_link(f"pain_{x['id']}"), color="#0b0b0b"),
                           st["cell"]),
                 _num(st, x["today"]),
                 Paragraph(_t(usually(x["per_day"]).removeprefix("обычно ")), ParagraphStyle(
                     "u", st["cell"], alignment=2, textColor=INK2))]
                for x in data["spikes"]]
        story.append(_table(["Боль", "Сегодня", "Обычно"], body,
                            [width * 0.55, width * 0.15, width * 0.30], st))

    # итог и заметки
    story.append(Spacer(1, 14))
    if total:
        parts = [f"{fmt.TYPE_RU.get(k, k)} — {v}" for k, v in data["types"].items()]
        story.append(Paragraph(_t(f"Всего за день: {fmt.signals_word(total)} ("
                                  + ", ".join(parts) + ")."), st["small"]))
    else:
        story.append(Paragraph(_t(f"Новых сигналов нет. Сообщений за день: {msgs}."),
                               st["small"]))

    warnings = _warnings(notes)
    if warnings:
        rows = [Paragraph(_t(w), st["small"]) for w in warnings]
        story += [Spacer(1, 10), _card(rows, width, bar=WARN_BAR)]
        story[-1].setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG)]))

    footer = f"Итоги дня · {day:%d.%m.%Y}"

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
        bottomMargin=20 * mm, title=f"Итоги дня · {day:%d.%m.%Y}",
        author="chat-parser", subject="Мониторинг чатов рынка оптики",
    )
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


def _warnings(notes: dict[str, Any]) -> list[str]:
    out = []
    if notes.get("paused"):
        out.append("Разбор на паузе: переписку я собрал, но нейросеть не запускал. "
                   "Включить — /live в боте.")
    if notes.get("budget_hit_at"):
        out.append(f"Дневной лимит на автоматический разбор исчерпан в "
                   f"{notes['budget_hit_at']:%H:%M} — разобрано не всё. Остальное можно "
                   "разобрать вручную: /extract.")
    out += list(notes.get("errors", []))
    if notes.get("backlog"):
        out.append(f"В архиве ждут ручного разбора: {fmt.discussions(notes['backlog'])} — "
                   "/extract.")
    return out
