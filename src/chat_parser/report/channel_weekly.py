"""Отклик на публикации и темы для контента с разбивкой по каналам."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from statistics import median
import unicodedata

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import clock
from ..bot.format import fmt_num
from ..ingest.channel_posts import Post
from ..links import message_link
from .pdf import FONT, HAIR, INK2, _a, _styles, _t


@dataclass
class Channel:
    id: int
    title: str
    username: str | None
    posts: list[Post] = field(default_factory=list)
    labels: dict[int, str] = field(default_factory=dict)
    status: str = ""
    ideas: list[dict] = field(default_factory=list)
    analysis_note: str = ""


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
        return "Посты и темы каналов за четыре недели"

    def email_text(self) -> str:
        lines = [self.subject, "Подробный PDF во вложении."]
        for channel in self.channels:
            lines.extend(["", channel.title,
                          f"Публикаций за четыре недели {len(channel.posts)}."])
            if channel.status:
                lines.append(channel.status)
            for theme, posts in theme_candidates(channel, self.until):
                lines.append(f"{theme} — {len(posts)} публикаций, типичный отклик "
                             f"{_number(median(p.engagement for p in posts))}.")
        return "\n".join(lines)

    def pdf(self) -> bytes:
        st = _styles()
        since, until = (dt.astimezone(clock.tz()) for dt in (self.since, self.until))
        story = [Paragraph(self.subject, st["title"]), Spacer(1, 6),
                 Paragraph(f"{since:%d.%m.%Y %H:%M} - {until:%d.%m.%Y %H:%M}", st["subtitle"]),
                 Spacer(1, 10)]
        if not self.channels:
            story.append(Paragraph("Каналы ещё не добавлены.", st["body"]))
        for index, channel in enumerate(self.channels):
            if index:
                story.append(PageBreak())
            story.extend(_channel_section(channel, self.until, st))

        def footer(canvas, doc):
            canvas.setFont(FONT, 8)
            canvas.setFillColor(INK2)
            canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, str(doc.page))

        out = BytesIO()
        doc = SimpleDocTemplate(out, pagesize=A4, title=self.subject, leftMargin=18 * mm,
                                rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return out.getvalue()


def _number(value: float | int | None) -> str:
    if value is None:
        return "н/д"
    if int(value) == value:
        return fmt_num(int(value))
    return f"{value:.1f}".replace(".", ",")


def _metrics(reactions: float | int, comments: float | int | None) -> str:
    return f"Реакции {_number(reactions)} · Комментарии {_number(comments)}"


def _title(text: str, limit: int = 170) -> str:
    text = " ".join(text.split())
    if not text:
        return "Пост без подписи"
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def top_posts(posts: list[Post]) -> list[Post]:
    return sorted((p for p in posts if p.engagement),
                  key=lambda p: (p.engagement, p.comments or 0, p.reactions, p.ts, p.id),
                  reverse=True)[:3]


def mature_posts(channel: Channel, until: datetime) -> list[Post]:
    return [p for p in channel.posts if p.ts <= until - timedelta(hours=24)]


def top_themes(channel: Channel) -> list[tuple[str, list[Post]]]:
    if not channel.posts:
        return []
    baseline = max(1, median(p.engagement for p in channel.posts))
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in channel.posts:
        if theme := channel.labels.get(post.id):
            groups[theme].append(post)
    ranked = [(theme, posts, median(p.engagement for p in posts))
              for theme, posts in groups.items()]
    ranked.sort(key=lambda item: (len(item[1]) > 1, item[2], len(item[1]), item[0].casefold()),
                reverse=True)
    return [(theme, posts) for theme, posts, score in ranked if score >= baseline][:3]


def theme_candidates(channel: Channel, until: datetime) -> list[tuple[str, list[Post]]]:
    sample = Channel(channel.id, channel.title, channel.username, mature_posts(channel, until),
                     channel.labels)
    return top_themes(sample)


def _post_link(channel: Channel, post: Post, label: str) -> str:
    return _a(_t(label), message_link(channel.id, channel.username, post.id))


def _sum_known(posts: list[Post], attribute: str) -> str:
    if not posts:
        return "0"
    values = [getattr(p, attribute) for p in posts if getattr(p, attribute) is not None]
    if not values:
        return "н/д"
    total = _number(sum(values))
    return total if len(values) == len(posts) else total + f" ({len(values)}/{len(posts)} постов)"


def _table(rows, widths, st, *, repeat=1):
    header = ParagraphStyle("report_header", st["cell_head"], textColor=INK2)
    cells = [[Paragraph(_t(v), header if index < repeat else st["cell"])
              if isinstance(v, str) else v for v in row]
             for index, row in enumerate(rows)]
    table = Table(cells, colWidths=widths, repeatRows=repeat, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, repeat - 1), HAIR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, HAIR),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


REACTIONS = {"👍": "лайки", "👎": "дизлайки", "❤": "сердца", "❤️": "сердца",
             "🔥": "огонь", "👏": "аплодисменты", "😁": "улыбки", "🤔": "размышления",
             "🤯": "удивление", "😢": "грусть", "🎉": "праздник", "💯": "сто баллов",
             "⭐": "звёзды", "свой эмодзи": "свои эмодзи"}


def _reaction_detail(post: Post) -> str:
    if post.detail is None:
        return "Разбивка реакций н/д"
    if not post.detail:
        return "Реакций нет"
    labels = []
    for emoji, count in sorted(post.detail.items(), key=lambda item: -item[1]):
        name = REACTIONS.get(emoji)
        if name is None:
            name = " ".join(unicodedata.name(c, "REACTION") for c in emoji
                            if c != "\ufe0f").lower()
        labels.append(f"{name} {count}")
    return " · ".join(labels)


def _channel_section(channel: Channel, until: datetime, st) -> list:
    width = A4[0] - 36 * mm
    story = [Paragraph(_t(channel.title), st["h2"])]
    if channel.status:
        story.append(Paragraph(_t(channel.status), st["body"]))
    if not channel.posts:
        if not channel.status:
            story.append(Paragraph("За последние четыре недели публикаций нет.", st["body"]))
        return story

    last_week = [p for p in channel.posts if p.ts >= until - timedelta(days=7)]
    story.append(Paragraph(
        _t(f"Публикаций за 4 недели {len(channel.posts)}, за последнюю неделю "
           f"{len(last_week)}. Реакции {_sum_known(channel.posts, 'reactions')}. "
           f"Комментарии {_sum_known(channel.posts, 'comments')}."),
        st["body"],
    ))
    story.append(Paragraph(
        "Счётчики накоплены с даты публикации и обновлены на момент сбора. "
        "Лайки входят в общее число реакций. «н/д» означает, что Telegram не отдал счётчик.",
        st["meta"],
    ))
    story.append(Paragraph("Результаты по неделям", st["h3"]))
    rows = [["Дата публикации", "Посты", "Реакции", "Комментарии", "Просмотры"]]
    for index in range(4):
        end, start = until - timedelta(days=7 * index), until - timedelta(days=7 * (index + 1))
        posts = [p for p in channel.posts if start <= p.ts < end]
        rows.append([f"{start.astimezone(clock.tz()):%d.%m} - {end.astimezone(clock.tz()):%d.%m}",
                     str(len(posts)), _sum_known(posts, "reactions"),
                     _sum_known(posts, "comments"), _sum_known(posts, "views")])
    story.append(_table(rows, [width * .26, width * .12, width * .18, width * .22, width * .22], st))

    established = mature_posts(channel, until)
    baseline = median(p.engagement for p in established) if established else 0
    story.append(Paragraph("Что привлекает внимание", st["h3"]))
    story.append(Paragraph(
        _t(f"Типичный отклик в этом канале — {_number(baseline)} реакций и комментариев "
           "на пост (медиана). Для сравнения берём посты старше 24 часов."),
        st["meta"],
    ))
    ranked = top_posts(established)
    if not ranked:
        story.append(Paragraph("Для сравнения отклика пока мало данных.", st["body"]))
    for post in ranked:
        theme = channel.labels.get(post.id, "")
        heading = _title(post.text)
        if theme:
            heading = theme + ". " + heading
        story.append(Paragraph(_post_link(channel, post, heading), st["body"]))
        factor = ""
        if baseline:
            change = round((post.engagement / baseline - 1) * 100)
            comparison = f"на {abs(change)}% {'выше' if change > 0 else 'ниже'} типичного"
            factor = " · " + (comparison if change else "на уровне типичного отклика")
        story.append(Paragraph(_t(_metrics(post.reactions, post.comments) + factor), st["meta"]))
        story.append(Spacer(1, 5))

    candidates = theme_candidates(channel, until)
    story.append(Paragraph("Темы для своих публикаций", st["h3"]))
    if channel.analysis_note:
        story.append(Paragraph(_t(channel.analysis_note), st["meta"]))
    if not candidates:
        story.append(Paragraph("Темы с устойчивым откликом пока не определены.", st["body"]))
    for theme, posts in candidates:
        comments = [p.comments for p in posts if p.comments is not None]
        note = "Один пост — гипотеза для проверки." if len(posts) == 1 else "Тема повторяется."
        story.append(Paragraph(_t(theme), st["meta_b"]))
        story.append(Paragraph(_t(
            f"{len(posts)} постов · медиана · "
            f"{_metrics(median(p.reactions for p in posts), median(comments) if comments else None)}. "
            + note,
        ), st["meta"]))
        sample = max(posts, key=lambda p: p.engagement)
        story.append(Paragraph(_post_link(channel, sample, "Пример публикации"), st["meta"]))
    for idea in channel.ideas:
        story.append(Paragraph(_t(idea["title"]), st["body"]))
        story.append(Paragraph(_t(idea["angle"]), st["meta"]))
        sources = [p for p in channel.posts if p.id in idea["evidence_ids"]]
        story.append(Paragraph(" · ".join(_post_link(channel, p, f"Основание {i + 1}")
                                         for i, p in enumerate(sources)), st["meta"]))

    story.append(Paragraph("Все публикации за четыре недели", st["h3"]))
    headers = [channel.title, "Все реакции / лайки", "Комментарии", "Просмотры / репосты"]
    rows = [headers]
    for post in sorted(channel.posts, key=lambda p: (p.ts, p.id), reverse=True):
        date = post.ts.astimezone(clock.tz()).strftime("%d.%m %H:%M")
        young = " · меньше суток" if post.ts > until - timedelta(hours=24) else ""
        body = [Paragraph(_post_link(channel, post, date + young), st["meta_b"]),
                Paragraph(_t(_title(post.text, 230)), st["cell"])]
        if theme := channel.labels.get(post.id):
            body.append(Paragraph(_t("Тема - " + theme), st["meta"]))
        body.append(Paragraph(_t(_reaction_detail(post)), st["meta"]))
        if post.views and post.comments is not None:
            body.append(Paragraph(_t(
                f"На 1000 просмотров {_number(post.engagement / post.views * 1000)} "
                "реакций и комментариев"
            ), st["meta"]))
        rows.append([body, _number(post.reactions) + " / " + _number(post.likes),
                     _number(post.comments), _number(post.views) + " / " + _number(post.forwards)])
    story.append(_table(rows, [width * .57, width * .14, width * .12, width * .17], st))
    return story
