"""Короткий PDF об отклике на публикации подключённых каналов."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from statistics import median

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .. import clock
from ..bot.format import fmt_num
from ..ingest.channel_posts import Post
from ..links import message_link
from .pdf import FONT, INK2, _a, _styles, _t


@dataclass
class Channel:
    id: int
    title: str
    username: str | None
    posts: list[Post] = field(default_factory=list)
    labels: dict[int, str] = field(default_factory=dict)
    status: str = ""


@dataclass
class ChannelWeeklyReport:
    since: datetime
    until: datetime
    channels: list[Channel]

    @property
    def filename(self) -> str:
        return f"channel-posts-{self.until.astimezone(clock.tz()):%Y-%m-%d}.pdf"

    @property
    def subject(self) -> str:
        return "Отклик на посты за неделю"

    def pdf(self) -> bytes:
        styles = _styles()
        since, until = (dt.astimezone(clock.tz()) for dt in (self.since, self.until))
        story = [
            Paragraph(self.subject, styles["title"]),
            Spacer(1, 5),
            Paragraph(f"{since:%d.%m.%Y %H:%M} - {until:%d.%m.%Y %H:%M}", styles["subtitle"]),
        ]
        if not self.channels:
            story.extend([Spacer(1, 14), Paragraph("Каналы ещё не добавлены.", styles["body"])])
        else:
            story.append(Paragraph(
                "Рейтинг по сумме реакций и комментариев. Счётчики на момент сбора.",
                styles["meta"],
            ))
        for channel in self.channels:
            story.append(KeepTogether(_channel_section(channel, styles)))

        def page_number(canvas, doc):
            canvas.setFont(FONT, 8)
            canvas.setFillColor(INK2)
            canvas.drawRightString(A4[0] - 20 * mm, 13 * mm, str(doc.page))

        out = BytesIO()
        doc = SimpleDocTemplate(out, pagesize=A4, title=self.subject, leftMargin=20 * mm,
                                rightMargin=20 * mm, topMargin=17 * mm, bottomMargin=19 * mm)
        doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
        return out.getvalue()


def _number(value: float | int | None) -> str:
    if value is None:
        return "н/д"
    if int(value) == value:
        return fmt_num(int(value))
    return f"{value:.1f}".replace(".", ",")


def _metrics(reactions: float | int, comments: float | int | None) -> str:
    return f"Реакции {_number(reactions)} · Комментарии {_number(comments)}"


def _title(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        return "Пост без подписи"
    return text if len(text) <= 150 else text[:150].rstrip() + "…"


def top_posts(posts: list[Post]) -> list[Post]:
    return sorted((post for post in posts if post.engagement),
                  key=lambda p: (p.engagement, p.comments or 0, p.reactions, p.ts, p.id),
                  reverse=True)[:3]


def top_themes(channel: Channel) -> list[tuple[str, list[Post]]]:
    if not channel.posts:
        return []
    baseline = max(1, median(post.engagement for post in channel.posts))
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in channel.posts:
        if theme := channel.labels.get(post.id):
            groups[theme].append(post)
    ranked = [(theme, posts, median(post.engagement for post in posts))
              for theme, posts in groups.items()]
    ranked.sort(key=lambda item: (len(item[1]) > 1, item[2], len(item[1]), item[0].casefold()),
                reverse=True)
    return [(theme, posts) for theme, posts, score in ranked if score >= baseline][:3]


def _channel_section(channel: Channel, styles) -> list:
    block = [Paragraph(_t(channel.title), styles["h2"])]
    status = f"Публикаций {fmt_num(len(channel.posts))}"
    if channel.status:
        status = f"{status} · {channel.status}" if channel.posts else channel.status
    block.append(Paragraph(_t(status), styles["meta"]))
    ranked = top_posts(channel.posts)
    if not ranked:
        if channel.posts:
            block.append(Paragraph("Отклика за период пока нет.", styles["body"]))
        return block

    block.append(Paragraph("Посты с наибольшим откликом", styles["h3"]))
    for post in ranked:
        block.append(Paragraph(_t(_title(post.text)), styles["body"]))
        date = f"{post.ts.astimezone(clock.tz()):%d.%m}"
        link = message_link(channel.id, channel.username, post.id)
        metadata = _t(f"{date} · {_metrics(post.reactions, post.comments)}")
        if link:
            metadata += " · " + _a("Пост", link)
        block.extend([Paragraph(metadata, styles["meta"]), Spacer(1, 5)])

    themes = top_themes(channel)
    if not themes:
        note = ("Темы с заметным откликом пока не выделились." if channel.labels
                else "Темы по этим публикациям пока не определены.")
        block.append(Paragraph(note, styles["meta"]))
        return block

    block.append(Paragraph("Темы для следующих постов", styles["h3"]))
    for theme, posts in themes:
        reactions = median(p.reactions for p in posts)
        known = [p.comments for p in posts if p.comments is not None]
        comments = median(known) if known else None
        label = _t(theme)
        if len(posts) == 1:
            label += " · стоит проверить ещё одним постом"
        detail = f"Публикаций {len(posts)} · медиана на пост · {_metrics(reactions, comments)}"
        block.append(Paragraph(label, styles["body"]))
        block.append(Paragraph(_t(detail), styles["meta"]))
    return block
