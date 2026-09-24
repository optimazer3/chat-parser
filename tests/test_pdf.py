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
    for part in ("Итоги дня", "Четверг, 24 сентября 2026", "Главное за день", "Чаты",
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
