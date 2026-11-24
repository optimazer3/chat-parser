"""Отклик на публикации и темы для контента с разбивкой по каналам."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from statistics import median
import unicodedata

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, KeepTogether, NextPageTemplate, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from .. import clock
from ..bot.format import fmt_num
from ..ingest.channel_posts import Post
from ..links import message_link
from .pdf import BOLD, FONT, HAIR, INK2, _a, _styles, _t


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
        st = _channel_styles()
        width = A4[0] - 36 * mm
        period = (f"{self.since.astimezone(clock.tz()):%d.%m.%Y} - "
                  f"{self.until.astimezone(clock.tz()):%d.%m.%Y}")
        out = BytesIO()
        doc = BaseDocTemplate(out, pagesize=A4, title=self.subject,
                              leftMargin=18 * mm, rightMargin=18 * mm,
                              topMargin=18 * mm, bottomMargin=17 * mm)
        story = []
        channels = self.channels or [Channel(0, "Еженедельный отчёт", None)]
        for index, channel in enumerate(channels):
            heading = Paragraph(_t(channel.title), st["channel"])
            _, height = heading.wrap(width, A4[1])
            frame_top = A4[1] - 18 * mm - height - 35

            def header(canvas, document, heading=heading, height=height, frame_top=frame_top):
                canvas.saveState()
                heading.drawOn(canvas, 18 * mm, A4[1] - 18 * mm - height)
                canvas.setFont(FONT, 9)
                canvas.setFillColor(INK2)
                canvas.drawString(18 * mm, A4[1] - 18 * mm - height - 15, period)
                canvas.setStrokeColor(colors.HexColor("#b8cec7"))
                canvas.line(18 * mm, frame_top + 7, A4[0] - 18 * mm, frame_top + 7)
                canvas.setFont(FONT, 8)
                canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, str(document.page))
                canvas.restoreState()

            # Каждому каналу — шаблон страниц со своим постоянным заголовком.
            name = f"channel_{index}"
            frame = Frame(18 * mm, 17 * mm, width, frame_top - 17 * mm,
                          leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
            doc.addPageTemplates(PageTemplate(id=name, frames=[frame], onPage=header))
            if index:
                story.extend([NextPageTemplate(name), PageBreak()])
            if self.channels:
                story.extend(_channel_section(channel, self.until, st))
            else:
                story.append(Paragraph("Каналы ещё не добавлены.", st["body"]))
        doc.build(story)
        return out.getvalue()


def _channel_styles() -> dict:
    st = _styles()
    st["channel"] = ParagraphStyle("channel_name", st["body"], fontName=BOLD, fontSize=23,
                                    leading=28, textColor=colors.HexColor("#173f37"))
    st["post_title"] = ParagraphStyle("post_title", st["body"], fontName=BOLD,
                                      fontSize=11.5, leading=16, spaceAfter=8)
    st["section"] = ParagraphStyle("channel_section", st["h3"], fontSize=14, leading=19,
                                   spaceBefore=18, spaceAfter=10,
                                   textColor=colors.HexColor("#173f37"))
    st["stat"] = ParagraphStyle("post_stat", st["body"], fontSize=10, leading=15)
    st["post_date"] = ParagraphStyle("post_date", st["meta"], spaceAfter=5)
    return st


def _number(value: float | int | None) -> str:
    if value is None:
        return "н/д"
    if int(value) == value:
        return fmt_num(int(value))
    return f"{value:.1f}".replace(".", ",")


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


def _post_card(channel: Channel, post: Post, until: datetime, st) -> KeepTogether:
    width = A4[0] - 36 * mm
    date = post.ts.astimezone(clock.tz()).strftime("%d.%m.%Y, %H:%M")
    if post.ts > until - timedelta(hours=24):
        date += " · опубликован меньше суток назад"
    card = [Paragraph(_t(date), st["post_date"]),
            Paragraph(_t(_title(post.text, 350)), st["post_title"])]
    if theme := channel.labels.get(post.id):
        card.append(Paragraph(_t("Тема - " + theme), st["meta"]))
        card.append(Spacer(1, 7))

    def stat(label, value):
        return Paragraph(_t(label) + "<br/><font size='14'><b>" + _t(_number(value))
                         + "</b></font>", st["stat"])

    link = _post_link(channel, post, "Открыть пост")
    rows = [
        [stat("Все реакции", post.reactions), stat("Лайки", post.likes),
         stat("Комментарии", post.comments)],
        [stat("Просмотры", post.views), stat("Репосты", post.forwards),
         Paragraph(link, st["stat"])],
    ]
    metrics = Table(rows, colWidths=[width / 3] * 3, hAlign="LEFT")
    metrics.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef4f1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    card.extend([metrics, Spacer(1, 7),
                 Paragraph(_t("Реакции по видам · " + _reaction_detail(post)), st["meta"])])
    if post.views and post.comments is not None:
        card.append(Paragraph(_t(
            f"На 1000 просмотров {_number(post.engagement / post.views * 1000)} реакций "
            "и комментариев"
        ), st["meta"]))
    card.extend([Spacer(1, 13), HRFlowable(width="100%", color=HAIR, thickness=0.5),
                 Spacer(1, 16)])
    return KeepTogether(card)


def _channel_section(channel: Channel, until: datetime, st) -> list:
    width = A4[0] - 36 * mm
    story = []
    if channel.status:
        story.append(Paragraph(_t(channel.status), st["body"]))
    if not channel.posts:
        if not channel.status:
            story.append(Paragraph("За последние четыре недели публикаций нет.", st["body"]))
        return story

    story.append(Paragraph(_t(
        f"Публикаций за 4 недели {len(channel.posts)}. "
        f"Реакции {_sum_known(channel.posts, 'reactions')}. "
        f"Комментарии {_sum_known(channel.posts, 'comments')}."
    ), st["body"]))
    story.append(Paragraph(
        "Лайки входят в число реакций. «н/д» - Telegram не передал данные. "
        "Все счётчики обновлены на момент сбора.",
        st["meta"],
    ))
    story.append(Paragraph("Посты этого канала", st["section"]))
    story.append(Paragraph("Все публикации за четыре недели, от новых к старым.", st["meta"]))
    story.append(Spacer(1, 12))
    for post in sorted(channel.posts, key=lambda p: (p.ts, p.id), reverse=True):
        story.append(_post_card(channel, post, until, st))

    # Итоги и рекомендации остаются внутри того же канала, после его публикаций.
    story.append(Paragraph("Итоги этого канала по неделям", st["section"]))
    story.append(Paragraph(
        "Посты сгруппированы по дате публикации. Цифры показывают весь накопленный "
        "отклик этих постов.",
        st["meta"],
    ))
    story.append(Spacer(1, 8))
    rows = [["Период", "Постов", "Реакции", "Комментарии", "Просмотры"]]
    for index in range(4):
        end = until - timedelta(days=7 * index)
        start = end - timedelta(days=7)
        posts = [p for p in channel.posts if start <= p.ts < end]
        rows.append([f"{start.astimezone(clock.tz()):%d.%m} - {end.astimezone(clock.tz()):%d.%m}",
                     str(len(posts)), _sum_known(posts, "reactions"),
                     _sum_known(posts, "comments"), _sum_known(posts, "views")])
    story.append(_table(rows, [width * .26, width * .12, width * .18,
                               width * .22, width * .22], st))

    story.append(Paragraph("Темы для своих публикаций", st["section"]))
    if channel.analysis_note:
        story.append(Paragraph(_t(channel.analysis_note), st["meta"]))
    candidates = theme_candidates(channel, until)
    if not candidates:
        story.append(Paragraph("Темы с устойчивым откликом пока не определены.", st["body"]))
    for theme, posts in candidates:
        comments = [p.comments for p in posts if p.comments is not None]
        note = "Один пост - гипотеза для проверки." if len(posts) == 1 else "Тема повторяется."
        story.append(Paragraph(_t(theme), st["post_title"]))
        story.append(Paragraph(_t(
            f"Публикаций по теме {len(posts)}. Обычно на пост приходится "
            f"{_number(median(p.reactions for p in posts))} реакций и "
            f"{_number(median(comments) if comments else None)} комментариев (медиана). "
            + note
        ), st["body"]))
        sample = max(posts, key=lambda p: p.engagement)
        story.append(Paragraph(_post_link(channel, sample, "Посмотреть пример"), st["meta"]))
        story.append(Spacer(1, 10))
    for idea in channel.ideas:
        story.append(Paragraph(_t(idea["title"]), st["post_title"]))
        story.append(Paragraph(_t(idea["angle"]), st["body"]))
        sources = [p for p in channel.posts if p.id in idea["evidence_ids"]]
        story.append(Paragraph(" · ".join(_post_link(channel, p, "Пост с похожей темой")
                                         for p in sources), st["meta"]))
        story.append(Spacer(1, 10))
    return story
