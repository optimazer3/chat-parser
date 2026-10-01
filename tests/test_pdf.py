"""PDF итогов дня: собирается, читается, ссылки кликабельны, подпись влезает в Telegram."""

from datetime import datetime, timedelta, timezone

import pytest

from chat_parser import clock
from chat_parser.report import daily
from chat_parser.report.pdf import clean

pypdf = pytest.importorskip("pypdf")

UNTIL = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)


def quote(text, name, label, link, company=None, role=None):
    return {"evidence_quote": text, "a_name": name, "a_company": company, "a_role": role,
            "author_label": label, "link": link}


def sample(**over):
    data = {
        "day": clock.day_start(UNTIL), "since": UNTIL - timedelta(days=1), "until": UNTIL,
        "chats": [
            {"id": -1001, "title": "Оптики Про 🔬", "username": "optika_pro", "link": None,
             "msgs": 184, "threads": 23, "signals": 17, "last_msg": 5012},
            {"id": -1002, "title": "Покупатели линз", "username": None, "link": None,
             "msgs": 0, "threads": 0, "signals": 0, "last_msg": 12},
        ],
        "highlights": ["Поставщики оправ срывают сроки на 4–6 недель."],
        "topics": [{"id": 12, "label": "Срыв сроков поставки оправ", "n": 9},
                   {"id": 3, "label": "Нехватка оборудования", "n": 7}],
        "new_pains": [{"id": 12, "label": "Срыв сроков поставки оправ", "audience": "owner",
                       "n_authors": 5, "n_signals": 9,
                       "quote": quote("поставщик опять сорвал сроки 😡", "Иван Петров",
                                      "u:aaaaaaaa", "https://t.me/optika_pro/4988",
                                      "Оптика Люкс", "владелец")}],
        "sharp": [{"type": "pain", "audience": "owner", "summary": "Нужен второй пупиллометр",
                   "intensity": 5, "cluster_id": 3, "pain_label": "Нехватка оборудования",
                   **quote("срочно нужен второй пупиллометр", "Ольга", "u:cccccccc",
                           "https://t.me/optika_pro/5001")}],
        "spikes": [{"id": 3, "label": "Нехватка оборудования", "today": 7, "per_day": 0.4}],
        "types": {"pain": 16, "need": 6},
    }
    data.update(over)
    return data


def read(pdf: bytes):
    import io

    r = pypdf.PdfReader(io.BytesIO(pdf))
    text = "\n".join(p.extract_text() for p in r.pages)
    links = [a.get_object().get("/A", {}).get("/URI") for p in r.pages
             for a in p.get("/Annots", [])]
    return r, text, links


def test_pdf_contents_and_links():
    notes = {"errors": ["не удалось прочитать «Клуб»: FloodWait"], "backlog": 12}
    pdf = daily.DailyReport(sample(), notes).pdf("optika_bot")
    assert pdf.startswith(b"%PDF")
    r, text, links = read(pdf)
    assert r.metadata.title == "Итоги дня · 24.09.2026"
    for part in ("Итоги дня", "Четверг, 24 сентября 2026", "Главное за сутки", "Чаты",
                 "О чём говорили", "Новые боли", "Острые сигналы", "острота 5 из 5",
                 "Всплески", "~3 в неделю", "Иван Петров · Оптика Люкс, владелец",
                 "поставщик опять сорвал сроки", "FloodWait", "12 обсуждений"):
        assert part in text, part
    assert "😡" not in text and "🔬" not in text  # чего нет в шрифте — не квадратиками
    assert "https://t.me/optika_pro/4988" in links and "https://t.me/optika_pro/5001" in links
    assert "https://t.me/optika_bot?start=pain_12" in links  # открывает карточку в боте
    assert "https://t.me/optika_bot?start=who_aaaaaaaa" in links


def test_empty_day_and_no_bot_links():
    data = sample(chats=[], highlights=[], topics=[], new_pains=[], sharp=[], spikes=[],
                  types={})
    pdf = daily.DailyReport(data, {"paused": True}).pdf()
    _, text, links = read(pdf)
    assert "Новых сигналов нет" in text and "Разбор на паузе" in text
    assert not [x for x in links if x and "start=" in x]


def test_long_report_spans_pages():
    many = [{"id": i, "label": f"Боль номер {i}", "audience": "owner", "n_authors": 2,
             "n_signals": 3, "quote": quote("очень длинная цитата " * 12, "Иван", "u:aaaaaaaa",
                                            f"https://t.me/optika_pro/{i}")}
            for i in range(1, 30)]
    r, text, _ = read(daily.DailyReport(sample(new_pains=many)).pdf())
    assert len(r.pages) > 2 and "стр. 3" in text


def test_caption_fits_telegram():
    many = [{"id": i, "label": "Очень длинное название боли про поставщиков и сроки " * 2,
             "audience": "owner", "n_authors": 2, "n_signals": 3, "quote": None}
            for i in range(1, 40)]
    cap = daily.caption(sample(new_pains=many, highlights=["вывод дня " * 20] * 5))
    assert daily._visible([cap]) <= daily.CAPTION_LIMIT
    assert cap.startswith("📊 <b>Итоги дня · 24.09</b>") and cap.endswith("Подробности — в файле.")
    short = daily.caption(sample())
    assert "/pain_12" in short and "🔥 острых сигналов: 1" in short


def test_plain_text_for_email():
    text = daily.plain(sample(), {"budget_hit_at": clock.now().replace(hour=19, minute=5)})
    assert text.startswith("Итоги дня · 24.09.2026")
    assert "184 сообщения" in text and "Срыв сроков поставки оправ" in text
    assert "лимит исчерпан в 19:05" in text and "во вложении (PDF)" in text


def test_clean_drops_missing_glyphs():
    assert clean("жду → 6 недель 😡✅") == "жду › 6 недель"
    assert clean(None) == ""


def test_report_day_is_the_day_it_covers():
    tz = clock.tz()
    evening = datetime(2026, 9, 24, 22, 0, tzinfo=tz)
    assert daily.report_day(evening - timedelta(days=1), evening).day == 24
    after_midnight = datetime(2026, 9, 25, 0, 1, tzinfo=tz)  # отчёт в 00:01 — за 24.09
    assert daily.report_day(after_midnight - timedelta(days=1), after_midnight).day == 24
    morning = datetime(2026, 9, 25, 9, 0, tzinfo=tz)
    assert daily.report_day(morning - timedelta(days=1), morning).day == 24


def detailed():
    sig = {"type": "pain", "audience": "owner", "intensity": 3, "summary": "жду оправы 6 недель",
           "chat_title": "Оптики Про",
           **quote("поставщик опять сорвал сроки", "Иван Петров", "u:aaaaaaaa",
                   "https://t.me/optika_pro/4988", "Оптика Люкс", "владелец")}
    return sample(
        summary="Сутки прошли под знаком поставок <и> нехватки приборов.",
        opportunities=["Региональный склад оправ"],
        signals=[{**sig, "cluster_id": 12, "pain_label": "Срыв сроков поставки оправ"},
                 {**sig, "cluster_id": None, "pain_label": None, "type": "question",
                  "summary": "где проверить зрение ребёнку"}],
        people=[{"author_label": "u:aaaaaaaa", "a_name": "Иван Петров", "username": "ivan",
                 "a_company": "Оптика Люкс", "a_role": "владелец", "note": "3 салона",
                 "msgs": 42, "chats": 1, "signals": 2},
                {"author_label": "u:dddddddd", "a_name": None, "username": None,
                 "a_company": None, "a_role": None, "note": None, "msgs": 3, "chats": 1,
                 "signals": 0}],
        people_total=5,
    )


def test_detailed_sections():
    data = detailed()
    data["new_pains"][0]["card"] = {"statement": "Оправы идут по 6 недель",
                                    "current_workarounds": ["заказывают у двух сразу"],
                                    "product_hypotheses": ["региональный склад"],
                                    "open_questions": ["сколько клиентов уходит"]}
    _, text, _ = read(daily.DailyReport(data).pdf())
    for part in ("5\nучастников", "Сутки прошли под знаком поставок <и> нехватки приборов.",
                 "Возможности для бизнеса", "Региональный склад оправ",
                 "Оправы идут по 6 недель", "Как сейчас выкручиваются", "заказывают у двух сразу",
                 "Что можно предложить", "Что стоит выяснить",
                 "Все сигналы за сутки", "Срыв сроков поставки оправ · 1 сигнал",
                 "Пока не привязаны к боли · 1 сигнал", "где проверить зрение ребёнку",
                 "Кто писал за сутки", "@ivan", "владелец, Оптика Люкс", "3 салона",
                 "участник dddddddd", "нет сведений", "И ещё 3 человека"):
        assert part in text, part


def test_letter_html():
    report = daily.DailyReport(detailed(), {"budget_hit_at": clock.now().replace(hour=19)})
    body = report.html()
    assert body.startswith("<!doctype html>") and "max-width:640px" in body
    for part in ("Итоги дня · 24.09.2026", "Главное за сутки",
                 "Сутки прошли под знаком поставок &lt;и&gt; нехватки приборов.",  # экранировано
                 "Возможности для бизнеса", "Новые боли", "Срыв сроков поставки оправ",
                 "Острые сигналы", "острота 5 из 5", "https://t.me/optika_pro/5001",
                 "Всплески", "лимит", "во вложении (PDF)"):
        assert part in body, part
    assert "Сутки прошли под знаком поставок" in report.plain()
    assert "Возможности для бизнеса:" in report.plain()
