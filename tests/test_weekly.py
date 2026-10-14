"""Еженедельный отчёт: тема форума, реакции, автор в фокусе, расписание, PDF и письмо."""

from datetime import datetime, timedelta

import pytest
from telethon.tl.types import (
    MessageReactions, MessageReplyHeader, ReactionCount, ReactionCustomEmoji, ReactionEmoji,
)

from chat_parser import clock
from chat_parser.bot import weekly
from chat_parser.config import settings
from chat_parser.ingest.topics import TopicNotFound, match_topic, reaction_counts, reply_target
from chat_parser.report.weekly import WeeklyReport, focus_matches, standout, themes_of

TOPICS = [(1, "Общий"), (12, "Бизнес. Управление ресурсами"), (20, "Маркетинг")]


def test_topic_found_despite_typo():
    assert match_topic("Бизнес. Управления ресурсами", TOPICS) == TOPICS[1]
    assert match_topic("бизнес управление ресурсами", TOPICS) == TOPICS[1]
    assert match_topic("Управление ресурсами", TOPICS) == TOPICS[1]
    assert match_topic("Логистика", TOPICS) is None
    err = TopicNotFound("Логистика", [t for _, t in TOPICS])
    assert "«Бизнес. Управление ресурсами»" in str(err)


def test_post_or_reply_inside_topic():
    class M:
        def __init__(self, head):
            self.reply_to = head

    assert reply_target(M(MessageReplyHeader(forum_topic=True, reply_to_msg_id=12)), 12) is None
    assert reply_target(M(MessageReplyHeader(reply_to_msg_id=105, reply_to_top_id=12)), 12) == 105
    assert reply_target(M(None), 12) is None


def test_reactions_by_emoji():
    class M:
        reactions = MessageReactions(results=[
            ReactionCount(reaction=ReactionEmoji(emoticon="👍"), count=30),
            ReactionCount(reaction=ReactionEmoji(emoticon="🔥"), count=10),
            ReactionCount(reaction=ReactionCustomEmoji(document_id=7), count=2),
        ])

    assert reaction_counts(M()) == {"👍": 30, "🔥": 10, "свой эмодзи": 2}
    assert reaction_counts(object()) == {}


def test_focus_author():
    spec = "Анжелла Шаталина"
    assert focus_matches(spec, "Анжелла Шаталина", None)
    assert focus_matches(spec, "Анжела Шаталина 👓", None)  # другое написание имени
    assert not focus_matches(spec, "Анна Шаталова", None)
    assert not focus_matches(spec, None, "angella")
    assert focus_matches("@Angella_Sh, Иван Петров", None, "angella_sh")
    assert focus_matches("t.me/angella_sh", "кто угодно", "Angella_Sh")


def test_every_monday_at_ten(monkeypatch):
    monkeypatch.setattr(settings, "weekly_weekday", 0)
    monkeypatch.setattr(settings, "weekly_hour", 10)
    tz = clock.tz()
    monday = datetime(2026, 10, 12, 9, 0, tzinfo=tz)  # понедельник
    assert weekly.slot_after(monday) == datetime(2026, 10, 12, 10, 0, tzinfo=tz)
    assert weekly.slot_after(monday.replace(hour=10, minute=30)) == datetime(
        2026, 10, 19, 10, 0, tzinfo=tz)
    wednesday = datetime(2026, 10, 14, 15, 0, tzinfo=tz)
    assert weekly.slot_after(wednesday) == datetime(2026, 10, 19, 10, 0, tzinfo=tz)


def post(theme, reactions, comments=0, focus=True, days=1, name="Анжелла Шаталина"):
    now = datetime(2026, 10, 13, 10, 0, tzinfo=clock.tz())
    return {"message_id": hash((theme, reactions, days)) % 10000, "ts": now - timedelta(days=days),
            "author_label": "u:aaaaaaaa" if focus else "u:bbbbbbbb", "a_name": name,
            "a_company": None, "a_role": None, "theme": theme,
            "summary": f"пост про {theme.lower()}", "text": "длинный текст поста " * 20,
            "reactions": reactions, "detail": {"👍": reactions}, "comments": comments,
            "focus": focus, "link": "https://t.me/optiki_go/12/100"}


def test_themes_by_reactions_per_post():
    themes = themes_of([post("Мотивация", 40, 3), post("Мотивация", 60, 1), post("Закупка", 45)])
    assert [t["theme"] for t in themes] == ["Мотивация", "Закупка"]  # 50 в среднем > 45
    assert themes[0]["reactions"] == 100 and themes[0]["posts"] == 2 and themes[0]["comments"] == 4
    assert themes[0]["best"]["reactions"] == 60


def test_standout_others():
    mine = [post("Мотивация", 40), post("Закупка", 10)]  # медиана 25
    others = [post("Аренда", 45, focus=False), post("Касса", 2, focus=False),
              post("Реклама", 25, focus=False)]
    assert [p["theme"] for p in standout(others, mine, 5)] == ["Аренда", "Реклама"]
    assert standout([post("Касса", 0, focus=False)], mine, 5) == []


def sample_data():
    now = datetime(2026, 10, 13, 7, 0, tzinfo=clock.tz())
    week = [post("Мотивация продавцов", 40, 3), post("Закупка оправ", 12, days=3)]
    other = [post("Аренда помещения", 45, 2, focus=False, name="Павел")]
    periods = []
    for key, title, days in (("week", "неделю", 7), ("month", "месяц", 30),
                             ("half", "полгода", 182)):
        mine = week + ([post("Мотивация продавцов", 60, days=100)] if key == "half" else [])
        periods.append({
            "key": key, "title": title, "days": days, "since": now - timedelta(days=days),
            "posts": len(mine) + 1, "reactions": sum(p["reactions"] for p in mine) + 45,
            "comments": 5, "focus_posts": sorted(mine, key=lambda p: -p["reactions"]),
            "focus_count": len(mine), "focus_reactions": sum(p["reactions"] for p in mine),
            "focus_themes": themes_of(mine), "other_posts": other, "other_count": 1,
            "other_themes": themes_of(other),
        })
    return {"chat_title": "Оптики go", "topic_title": "Бизнес. Управление ресурсами",
            "now": now, "focus_names": ["Анжелла Шаталина"], "periods": periods,
            "summary": "Лучше всего заходят посты про мотивацию продавцов.",
            "focus": ["Мотивация продавцов — 50 реакций в среднем на пост."],
            "others": ["У других выделяется аренда помещения — 45 реакций."],
            "ideas": ["Разбор системы бонусов для продавцов."]}


def test_weekly_pdf_and_letter():
    pypdf = pytest.importorskip("pypdf")
    import io

    report = WeeklyReport(sample_data(), {"warnings": ["тестовое предупреждение"]})
    pdf = report.pdf()
    assert pdf and pdf.startswith(b"%PDF")
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    for part in ("Еженедельный отчёт", "Тема «Бизнес. Управление ресурсами»", "Главное",
                 "Лучше всего заходят посты про мотивацию продавцов", "Идеи для постов",
                 "Анжелла Шаталина", "За неделю · 2 поста · 52 реакции", "За месяц", "За полгода",
                 "Реакций в среднем на пост по темам", "Мотивация продавцов", "Лучшие посты",
                 "40 реакций · 3 комментария", "Другие участники", "Аренда помещения",
                 "Павел", "Как считается", "тестовое предупреждение"):
        assert part in text, part
    assert report.subject == "Отклик на темы · Оптики go — неделя по 13.10.2026"
    assert report.filename == "otklik-na-temy-2026-10-13.pdf"

    html = report.html()
    for part in ("Еженедельный отчёт · 13.10.2026", "Главное", "Идеи для постов",
                 "За неделю: что откликается", "Мотивация продавцов — в среднем 40",
                 "40 реакций (👍 40) · 3 комментария", "Выделяется у других участников",
                 "Павел", "открыть пост"):
        assert part in html, part
    plain = report.plain()
    assert "Лучше всего заходят" in plain and "Идеи для постов:" in plain


def test_problem_report():
    report = WeeklyReport({"chat_title": "Оптики go", "topic_title": "Логистика",
                           "now": datetime(2026, 10, 13, 7, 0, tzinfo=clock.tz()),
                           "periods": [], "focus_names": []},
                          {"problem": "Тема «Логистика» не найдена."})
    assert report.pdf().startswith(b"%PDF")
    assert "Тема «Логистика» не найдена." in report.html()
    assert "Тема «Логистика» не найдена." in report.plain()


async def test_chat_found_when_session_cache_is_empty():
    """Строковая сессия: по одному номеру чат не находится — ищем через диалоги."""
    from chat_parser.ingest.collector import entity_for

    class Client:
        def __init__(self):
            self.cached, self.calls = False, []

        async def get_entity(self, ref):
            self.calls.append(ref)
            if ref == "optiki_go" or self.cached:
                return "чат"
            raise ValueError("Could not find the input entity")

        async def get_dialogs(self):
            self.calls.append("dialogs")
            self.cached = True

    c = Client()
    assert await entity_for(c, -100123, "optiki_go") == "чат"
    assert c.calls == [-100123, "optiki_go"]
    c = Client()
    assert await entity_for(c, -100123) == "чат"
    assert c.calls == [-100123, "dialogs", -100123]
