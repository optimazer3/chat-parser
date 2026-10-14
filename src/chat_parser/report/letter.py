"""Письмо с итогами дня: обзор прямо в теле письма, подробности — в PDF.

Вёрстка таблицами и встроенными стилями: почтовые программы (Gmail, Outlook,
мобильные) не понимают внешний CSS и современную раскладку.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .. import clock, people
from ..bot import format as fmt

INK, INK2, MUTED, HAIR, CARD, ACCENT, LINK = (
    "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#f4f3ef", "#2a78d6", "#1c5cab")
SEVERITY = {5: ("#d03b3b", "#ffffff"), 4: ("#ec835a", "#0b0b0b")}
FONT = "font-family:'PT Sans',Arial,Helvetica,sans-serif;"


def _e(text: Any) -> str:
    return escape(str(text or ""))


def _a(text: str, href: str | None) -> str:
    if not href:
        return text
    return f'<a href="{escape(href, quote=True)}" style="color:{LINK};text-decoration:none">{text}</a>'


def _p(html: str, size: int = 15, color: str = INK, extra: str = "") -> str:
    return (f'<p style="margin:0 0 8px;{FONT}font-size:{size}px;line-height:1.45;'
            f'color:{color};{extra}">{html}</p>')


def _h2(text: str) -> str:
    return (f'<h2 style="margin:28px 0 10px;{FONT}font-size:19px;line-height:1.3;'
            f'color:{INK}">{_e(text)}</h2>')


def _ul(items: list[str]) -> str:
    lis = "".join(f'<li style="margin:0 0 6px">{_e(x)}</li>' for x in items)
    return (f'<ul style="margin:0 0 4px;padding-left:20px;{FONT}font-size:15px;'
            f'line-height:1.45;color:{INK}">{lis}</ul>')


def _card(inner: str, bar: str | None = None) -> str:
    border = f"border-left:3px solid {bar};" if bar else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 10px;border-collapse:separate"><tr><td style="background:{CARD};'
            f'border-radius:8px;{border}padding:14px 16px">{inner}</td></tr></table>')


def _quote(q: dict[str, Any]) -> str:
    out = (f'<p style="margin:8px 0 4px;padding-left:10px;border-left:2px solid #c3c2b7;'
           f'{FONT}font-size:14px;font-style:italic;color:{INK}">«{_e(q.get("evidence_quote"))}»</p>')
    parts = []
    if q.get("author_label"):
        parts.append(_e(people.display(q.get("a_name"), q["author_label"], q.get("a_company"),
                                       q.get("a_role"))))
    if q.get("link"):
        parts.append(_a("открыть сообщение", q["link"]))
    if parts:
        out += _p("— " + " · ".join(parts), size=13, color=INK2)
    return out


def html(data: dict[str, Any], notes: dict[str, Any] | None = None) -> str:
    notes = notes or {}
    day = data["day"]
    period = ""
    if data.get("since") and data.get("until"):
        since = data["since"].astimezone(clock.tz())
        until = data["until"].astimezone(clock.tz())
        period = (f"с {since:%d.%m %H:%M} по {until:%d.%m %H:%M} ({clock.tz_label()})")
    msgs = sum(c["msgs"] for c in data["chats"])
    threads = sum(c["threads"] for c in data["chats"])
    total = sum(data["types"].values())
    tiles = [(msgs, "сообщений"), (data.get("people_total", 0), "участников"),
             (threads, "обсуждений"), (total, "сигналов"),
             (len(data["new_pains"]), "новых болей")]

    b: list[str] = []
    b.append(_p("МОНИТОРИНГ ЧАТОВ · РЫНОК ОПТИКИ", size=11, color=MUTED,
                extra="font-weight:bold;letter-spacing:.5px"))
    b.append(f'<h1 style="margin:0 0 4px;{FONT}font-size:28px;line-height:1.2;color:{INK}">'
             f"Итоги дня · {day:%d.%m.%Y}</h1>")
    if period:
        b.append(_p(_e(period), size=14, color=INK2))
    cells = "".join(
        f'<td style="padding:4px"><div style="background:{CARD};border-radius:8px;'
        f'padding:10px 12px"><div style="{FONT}font-size:22px;font-weight:bold;color:{INK}">'
        f'{_e(fmt.fmt_num(n))}</div><div style="{FONT}font-size:12px;color:{INK2}">{label}'
        f"</div></div></td>"
        for n, label in tiles)
    b.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             f'style="margin:14px 0 6px"><tr>{cells}</tr></table>')

    if data.get("summary") or data.get("highlights"):
        inner = f'<div style="{FONT}font-size:16px;font-weight:bold;margin-bottom:8px">Главное за сутки</div>'
        if data.get("summary"):
            inner += _p(_e(data["summary"]))
        if data.get("highlights"):
            inner += _ul(data["highlights"])
        b.append(_card(inner, bar=ACCENT))
    if data.get("opportunities"):
        inner = (f'<div style="{FONT}font-size:16px;font-weight:bold;margin-bottom:8px">'
                 "Возможности для бизнеса</div>" + _ul(data["opportunities"]))
        b.append(_card(inner))

    if data["new_pains"]:
        b.append(_h2("Новые боли"))
        for p in data["new_pains"]:
            who = fmt.AUDIENCE_RU.get(p["audience"], p["audience"])
            inner = (f'<div style="{FONT}font-size:16px;font-weight:bold">{_e(p["label"])}</div>'
                     + _p(_e(f"{fmt.people_word(p['n_authors'])} · "
                             f"{fmt.signals_word(p['n_signals'])} · {who}"), size=13, color=INK2))
            statement = (p.get("card") or {}).get("statement") or p.get("statement")
            if statement:
                inner += _p(_e(statement), size=14)
            if p.get("quote"):
                inner += _quote(p["quote"])
            b.append(_card(inner))

    if data["sharp"]:
        b.append(_h2("Острые сигналы"))
        for s in data["sharp"]:
            bg, ink = SEVERITY.get(s["intensity"], SEVERITY[4])
            kind = fmt.TYPE_RU.get(s["type"], s["type"])
            who = fmt.AUDIENCE_RU.get(s["audience"], s["audience"])
            badge = (f'<span style="background:{bg};color:{ink};border-radius:3px;'
                     f'padding:1px 6px;font-size:12px">острота {s["intensity"]} из 5</span>')
            inner = (_p(f"{badge} &nbsp;{_e(kind)} · {_e(who)}", size=13, color=INK2)
                     + _p(f"<b>{_e(s['summary'])}</b>") + _quote(s))
            if s.get("pain_label"):
                inner += _p("Входит в боль: " + _e(s["pain_label"]), size=13, color=INK2)
            b.append(_card(inner))

    if data["spikes"]:
        from .daily import usually

        b.append(_h2("Всплески"))
        b.append(_ul([f"{x['label']} — за сутки {x['today']}, {usually(x['per_day'])}"
                      for x in data["spikes"]]))

    if data["chats"]:
        b.append(_h2("Чаты"))
        b.append(_ul([
            f"{c['title'] or 'без названия'}: "
            + (f"{fmt.messages_word(c['msgs'])}, {fmt.people_word(c.get('people', 0))}, "
               f"{fmt.signals_word(c['signals'])}" if c["msgs"] else "тишина")
            for c in data["chats"]
        ]))

    if not total:
        b.append(_p(_e(f"Новых сигналов нет. Сообщений за сутки: {msgs}."), color=INK2))
    from .pdf import _warnings

    warnings = _warnings(notes)
    if warnings:
        b.append(_card("".join(_p(_e(w), size=14) for w in warnings), bar="#ec835a"))

    b.append(f'<hr style="border:none;border-top:1px solid {HAIR};margin:24px 0 12px">')
    b.append(_p("Полный отчёт — во вложении (PDF): все сигналы за сутки по болям с цитатами, "
                "кто писал и что о них известно, подробности по каждой новой боли.",
                size=13, color=INK2))
    b.append(_p("— chat-parser, мониторинг чатов рынка оптики", size=12, color=MUTED))

    return wrap("".join(b))


def wrap(body: str) -> str:
    """Страница письма: колонка до 640 пикселей по центру."""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            f'<body style="margin:0;padding:0;background:#ffffff">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td align="center" style="padding:20px 12px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:640px"><tr><td>{body}</td></tr></table>'
            f"</td></tr></table></body></html>")
