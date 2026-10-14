"""Еженедельный отчёт: какие темы постов откликаются подписчикам.

Пост — длинное сообщение, в котором автор раскрывает тему (решает модель).
Интерес темы — реакции на её посты: в среднем на пост и всего; комментарии
(ответы под постом) — дополнительный признак. Сначала — посты фокусного автора,
потом то, что выделяется у других.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from statistics import median
from typing import Any

import asyncpg

from .. import clock, people
from ..bot import format as fmt

log = logging.getLogger("chat_parser.report")

PERIODS = (("week", "неделю", 7), ("month", "месяц", 30), ("half", "полгода", 182))
TOP_FOCUS = {"week": 5, "month": 7, "half": 10}
TOP_OTHERS = {"week": 3, "month": 5, "half": 7}


def focus_matches(spec: str, name: str | None, username: str | None) -> bool:
    """Подходит ли автор под WEEKLY_FOCUS: «Анжелла Шаталина» (по началу каждого
    слова — «Анжела Шаталина» тоже), «@username» или ссылка t.me/username."""
    def stem(word: str) -> str:
        word = word.casefold().replace("ё", "е")
        return word[:5] if len(word) > 5 else word

    for item in (x.strip() for x in spec.split(",")):
        if not item:
            continue
        if item.startswith("@") or "t.me/" in item:
            wanted = item.rsplit("/", 1)[-1].lstrip("@").casefold()
            if username and username.casefold() == wanted:
                return True
        elif name:
            words = re.findall(r"\w+", item)
            have = name.casefold().replace("ё", "е")
            if words and all(stem(w) in have for w in words):
                return True
    return False


def post_link(chat_id: int, username: str | None, topic_id: int, message_id: int) -> str | None:
    """Ссылка на сообщение в теме: открывает его прямо в нужной теме."""
    if username:
        return f"https://t.me/{username}/{topic_id}/{message_id}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{topic_id}/{message_id}"
    return None


def themes_of(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Темы с откликом: в среднем реакций на пост (главное), всего, комментарии."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in posts:
        groups.setdefault(p["theme"] or "Без темы", []).append(p)
    out = []
    for theme, items in groups.items():
        reactions = sum(p["reactions"] for p in items)
        out.append({
            "theme": theme, "posts": len(items), "reactions": reactions,
            "avg": reactions / len(items), "comments": sum(p["comments"] for p in items),
            "best": max(items, key=lambda p: (p["reactions"], p["ts"])),
        })
    out.sort(key=lambda t: (-t["avg"], -t["reactions"], t["theme"]))
    return out


def standout(others: list[dict[str, Any]], mine: list[dict[str, Any]], limit: int) -> list[dict]:
    """Посты других, которые откликаются не хуже типичного поста фокусного автора
    (или, если его постов нет, — заметно лучше обычного у остальных)."""
    with_reactions = [p for p in others if p["reactions"] > 0]
    if not with_reactions:
        return []
    if mine:
        bar = median(p["reactions"] for p in mine)
    else:
        values = sorted(p["reactions"] for p in with_reactions)
        bar = values[int(len(values) * 0.75)] if len(values) > 3 else values[0]
    picked = [p for p in with_reactions if p["reactions"] >= bar]
    return sorted(picked, key=lambda p: (-p["reactions"], -p["comments"]))[:limit]


async def collect(pool: asyncpg.Pool, chat: dict[str, Any], topic_id: int, topic_title: str,
                  focus: set[str], focus_names: list[str], now: datetime) -> dict[str, Any]:
    half_year = now - timedelta(days=PERIODS[-1][2])
    rows = await pool.fetch(
        """
        select p.message_id, p.ts, p.author_hash, p.author_label, p.theme, p.summary, p.text,
               p.reactions, p.reactions_detail, p.views,
               (select count(*) from topic_messages r
                 where r.chat_id = p.chat_id and r.reply_to = p.message_id) comments,
               a.name a_name, a.username, a.company a_company, a.role a_role
          from topic_messages p left join authors a on a.author_hash = p.author_hash
         where p.chat_id = $1 and p.topic_id = $2 and p.is_post and p.ts >= $3
         order by p.reactions desc, p.ts desc
        """,
        chat["id"], topic_id, half_year,
    )
    posts = []
    for r in rows:
        p = dict(r)
        detail = p.pop("reactions_detail")
        p["detail"] = json.loads(detail) if isinstance(detail, str) else (detail or {})
        p["focus"] = p["author_hash"] in focus
        p["link"] = post_link(chat["id"], chat["username"], topic_id, p["message_id"])
        posts.append(p)

    periods = []
    for key, title, days in PERIODS:
        since = now - timedelta(days=days)
        inside = [p for p in posts if p["ts"] >= since]
        mine = [p for p in inside if p["focus"]]
        rest = [p for p in inside if not p["focus"]]
        periods.append({
            "key": key, "title": title, "days": days, "since": since,
            "posts": len(inside), "reactions": sum(p["reactions"] for p in inside),
            "comments": sum(p["comments"] for p in inside),
            "focus_posts": mine[:TOP_FOCUS[key]], "focus_count": len(mine),
            "focus_reactions": sum(p["reactions"] for p in mine),
            "focus_themes": themes_of(mine),
            "other_posts": standout(rest, mine, TOP_OTHERS[key]), "other_count": len(rest),
            "other_themes": themes_of(rest),
        })
    week_messages = await pool.fetchval(
        "select count(*) from topic_messages where chat_id = $1 and topic_id = $2 and ts >= $3",
        chat["id"], topic_id, now - timedelta(days=7),
    )
    return {
        "chat_title": chat["title"], "topic_title": topic_title, "now": now,
        "focus_names": focus_names, "periods": periods, "week_messages": week_messages,
    }


# ------------------------------------------------------------ представления


def _n(n: int, one: str, few: str, many: str) -> str:
    """_n(32, "реакция", "реакции", "реакций") -> «32 реакции»."""
    return f"{fmt.fmt_num(n)} {fmt.plural(n, one, few, many)}"


def _who(p: dict[str, Any]) -> str:
    return people.display(p.get("a_name"), p["author_label"], p.get("a_company"), p.get("a_role"))


def _focus_title(data: dict[str, Any]) -> str:
    return ", ".join(data.get("focus_names") or []) or "Фокусный автор"


def reactions_line(p: dict[str, Any], emoji: bool = True) -> str:
    """«32 реакции (👍 20 · 🔥 12) · 5 комментариев»; без эмодзи — для PDF."""
    out = _n(p["reactions"], "реакция", "реакции", "реакций")
    if emoji and p.get("detail"):
        parts = sorted(p["detail"].items(), key=lambda kv: -kv[1])[:5]
        out += " (" + " · ".join(f"{k} {v}" for k, v in parts) + ")"
    if p.get("comments"):
        out += " · " + _n(p["comments"], "комментарий", "комментария", "комментариев")
    return out


@dataclass
class WeeklyReport:
    data: dict[str, Any]
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def end(self) -> datetime:
        return self.data["now"].astimezone(clock.tz())

    @property
    def week(self) -> dict[str, Any] | None:
        periods = self.data.get("periods") or []
        return periods[0] if periods else None

    @property
    def subject(self) -> str:
        chat = self.data.get("chat_title") or "группа"
        return f"Отклик на темы · {chat} — неделя по {self.end:%d.%m.%Y}"

    @property
    def filename(self) -> str:
        return f"otklik-na-temy-{self.end:%Y-%m-%d}.pdf"

    @property
    def caption(self) -> str:
        """Подпись к PDF, если отчёт пришлось прислать в Telegram."""
        lines = [f"📅 <b>Отклик на темы · неделя по {self.end:%d.%m}</b>"]
        if self.week:
            lines.append(f"{_n(self.week['posts'], 'пост', 'поста', 'постов')} · "
                         f"{_n(self.week['reactions'], 'реакция', 'реакции', 'реакций')}")
        if self.data.get("summary"):
            lines += ["", fmt.esc(self.data["summary"][:600])]
        return "\n".join(lines + ["Подробности — в файле."])

    @property
    def text(self) -> str:
        """Короткий текст, если PDF не собрался."""
        return self.caption.replace("Подробности — в файле.", "") + self.plain()

    def plain(self) -> str:
        d, lines = self.data, [self.subject, ""]
        if self.notes.get("problem"):
            return "\n".join(lines + [self.notes["problem"]])
        if d.get("summary"):
            lines += [d["summary"], ""]
        week = self.week
        if week:
            lines.append(f"За неделю: {_n(week['posts'], 'пост', 'поста', 'постов')}, "
                         f"{_n(week['reactions'], 'реакция', 'реакции', 'реакций')}.")
        if d.get("focus"):
            lines += ["", f"{_focus_title(d)}:"] + [f"• {x}" for x in d["focus"]]
        if d.get("ideas"):
            lines += ["", "Идеи для постов:"] + [f"• {x}" for x in d["ideas"]]
        lines += ["", "Подробно — во вложении (PDF): темы и лучшие посты за неделю, месяц "
                      "и полгода.", "", "— chat-parser, мониторинг чатов рынка оптики"]
        return "\n".join(lines)

    def html(self) -> str:
        from .letter import ACCENT, INK2, _card, _h2, _p, _ul, wrap

        d, b = self.data, []
        b.append(_p("ОТКЛИК НА ТЕМЫ · " + escape(d.get("chat_title") or "").upper(), size=11,
                    color="#898781", extra="font-weight:bold;letter-spacing:.5px"))
        b.append(f'<h1 style="margin:0 0 4px;font-family:Arial,sans-serif;font-size:26px">'
                 f"Еженедельный отчёт · {self.end:%d.%m.%Y}</h1>")
        b.append(_p(escape(f"Тема «{d.get('topic_title') or ''}» · реакции на посты за "
                           "неделю, месяц и полгода"), size=14, color=INK2))
        if self.notes.get("problem"):
            b.append(_card(_p(escape(self.notes["problem"])), bar="#ec835a"))
            return wrap("".join(b))
        if d.get("summary") or d.get("focus"):
            inner = _p("<b>Главное</b>", size=16)
            if d.get("summary"):
                inner += _p(escape(d["summary"]))
            if d.get("focus"):
                inner += _p(f"<b>{escape(_focus_title(d))}</b>", size=14) + _ul(d["focus"])
            if d.get("others"):
                inner += _p("<b>Другие участники</b>", size=14) + _ul(d["others"])
            b.append(_card(inner, bar=ACCENT))
        if d.get("ideas"):
            b.append(_card(_p("<b>Идеи для постов</b>", size=16) + _ul(d["ideas"])))
        for period in d["periods"]:
            b.append(_h2(f"За {period['title']}: что откликается"))
            if period["focus_themes"]:
                b.append(_p(f"<b>{escape(_focus_title(d))}</b> — темы по реакциям на пост:",
                            size=14))
                b.append(_ul([f"{t['theme']} — в среднем {t['avg']:.0f}, всего "
                              f"{t['reactions']} ({_n(t['posts'], 'пост', 'поста', 'постов')})"
                              for t in period["focus_themes"][:6]]))
            for p in period["focus_posts"][:3]:
                b.append(_post_html(p))
            if period["other_posts"]:
                b.append(_p("<b>Выделяется у других участников:</b>", size=14))
                for p in period["other_posts"][:3]:
                    b.append(_post_html(p, author=True))
            if not period["posts"]:
                b.append(_p("Постов за этот срок нет.", color=INK2))
        b.append(_p("Подробно — во вложении (PDF): все темы с цифрами и лучшие посты за "
                    "каждый срок.", size=13, color=INK2))
        return wrap("".join(b))

    def pdf(self, bot_username: str | None = None) -> bytes | None:
        from . import weekly_pdf

        try:
            return weekly_pdf.render(self.data, self.notes, bot_username)
        except Exception:  # noqa: BLE001 — без PDF письмо всё равно уйдёт
            log.exception("PDF еженедельного отчёта не собрался")
            return None


def _post_html(p: dict[str, Any], author: bool = False) -> str:
    from .letter import INK2, LINK, _card, _p

    head = f"{p['ts'].astimezone(clock.tz()):%d.%m} · {escape(p['theme'] or '')}"
    inner = _p(head, size=13, color=INK2)
    inner += _p(f"<b>{escape(p['summary'] or (p['text'] or '')[:200])}</b>")
    meta = escape(reactions_line(p))
    if author:
        meta = escape(_who(p)) + " · " + meta
    if p.get("link"):
        meta += (f' · <a href="{escape(p["link"], quote=True)}" '
                 f'style="color:{LINK};text-decoration:none">открыть пост</a>')
    return _card(inner + _p(meta, size=13, color=INK2))
