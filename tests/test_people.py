from chat_parser.analyze.schema import Extraction, PersonHint
from chat_parser.analyze.validate import validate_people
from chat_parser.bot.format import fmt_person, fmt_quote
from chat_parser.people import (
    display, parse_fields, parse_person_info, split_company_role, who_command,
)

SOURCE = (
    "[m:10 | u:aaaaaaaa | 2026-09-24 11:00] у меня два салона в Казани, оптика Люкс\n"
    "[m:11 | u:bbbbbbbb | 2026-09-24 11:05] а у Ивана оптика Люкс, он владелец\n"
)


def hint(label, quote, company="Оптика Люкс", role="владелец"):
    return PersonHint(author_label=label, company=company, role=role, evidence_quote=quote)


def test_person_hint_from_own_words_is_kept():
    ex = Extraction(has_signals=False, signals=[],
                    people=[hint("u:aaaaaaaa", "у меня два салона в Казани")])
    assert validate_people(ex, SOURCE) == [
        ("u:aaaaaaaa", "Оптика Люкс", "владелец", "у меня два салона в Казани", 10)
    ]


def test_words_of_others_are_not_attributed():
    """u:bbbbbbbb рассказал про Ивана — это не слова самого u:aaaaaaaa."""
    ex = Extraction(has_signals=False, signals=[],
                    people=[hint("u:aaaaaaaa", "у Ивана оптика Люкс, он владелец")])
    assert validate_people(ex, SOURCE) == []


def test_empty_or_invented_hints_are_dropped():
    ex = Extraction(has_signals=False, signals=[], people=[
        hint("u:aaaaaaaa", "у меня два салона в Казани", company="", role=""),
        hint("u:aaaaaaaa", "я директор крупной сети"),
    ])
    assert validate_people(ex, SOURCE) == []


def test_company_role_input():
    assert split_company_role("Оптика Люкс, владелец") == ("Оптика Люкс", "владелец")
    assert split_company_role("Оптика Люкс") == ("Оптика Люкс", None)
    assert split_company_role(" ,оптометрист") == (None, "оптометрист")
    assert split_company_role("-") == (None, None)


def test_signature_under_quote():
    q = {"evidence_quote": "жду оправы по 6 недель", "link": "https://t.me/c/1/2",
         "author_label": "u:ab12cd34", "a_name": "Иван Петров",
         "a_company": "Оптика Люкс", "a_role": "владелец"}
    text = fmt_quote(q)
    assert "— Иван Петров · Оптика Люкс, владелец /who_ab12cd34" in text
    assert "↗ сообщение в чате" in text
    assert display(None, "u:ab12cd34") == "участник ab12cd34"
    assert who_command("u:ab12cd34") == "/who_ab12cd34"


def test_person_card_shows_hint_with_quote():
    text = fmt_person({
        "author_label": "u:ab12cd34", "name": "Иван Петров", "username": "ivan",
        "company": None, "role": None, "note": None,
        "company_hint": "Оптика Люкс", "role_hint": "владелец",
        "hint_quote": "у меня два салона", "hint_link": "https://t.me/c/1/10",
        "chats": [{"title": "Оптики", "n": 12}], "signals": 3,
        "pains": [{"id": 5, "label": "Долгие поставки"}],
    })
    assert "Компания: — не указано" in text
    assert "по словам самого участника:</b> владелец, Оптика Люкс" in text
    assert "у меня два салона" in text and "/pain_5" in text and "12 сообщений" in text


def test_person_info_line():
    assert parse_person_info("@ivan_optika Оптика Люкс, владелец") == (
        "ivan_optika", None, "Оптика Люкс, владелец")
    assert parse_person_info("@ivan_optika, Оптика Люкс")[2] == "Оптика Люкс"
    assert parse_person_info("https://t.me/ivan_optika: Оптика")[:2] == ("ivan_optika", None)
    assert parse_person_info("/who_ab12cd34 Линзы Плюс") == (None, "u:ab12cd34", "Линзы Плюс")
    assert parse_person_info("@ivan") == ("ivan", None, "")
    assert parse_person_info("Иван Петров, Оптика Люкс") is None


def test_person_fields():
    assert parse_fields("Оптика Люкс, владелец, знакомы по выставке, общались про линзы") == (
        "Оптика Люкс", "владелец", "знакомы по выставке, общались про линзы")
    assert parse_fields("роль: оптометрист") == (None, "оптометрист", None)
    assert parse_fields("Заметка: знакомы, давно") == (None, None, "знакомы, давно")
    assert parse_fields("Оптика Люкс, , звонить после обеда") == (
        "Оптика Люкс", None, "звонить после обеда")
    assert parse_fields("Оптика Люкс\n\nвладелец\nзаметка раз\nзаметка два") == (
        "Оптика Люкс", "владелец", "заметка раз\nзаметка два")
    assert parse_fields("") == (None, None, None)


def test_report_time_input():
    from chat_parser.bot.live import parse_time

    assert parse_time("21:30") == (21, 30) and parse_time("21.30") == (21, 30)
    assert parse_time("9") == (9, 0) and parse_time("в 9:05") == (9, 5)
    assert parse_time("22 ч") == (22, 0)
    assert parse_time("24:00") is None and parse_time("21:60") is None
    assert parse_time("вечером") is None


def test_next_time_with_minutes():
    from datetime import datetime

    from chat_parser import clock

    at = datetime(2026, 9, 24, 15, 0, tzinfo=clock.tz())
    assert clock.next_at(21, 30, at) == datetime(2026, 9, 24, 21, 30, tzinfo=clock.tz())
    assert clock.next_at(9, 0, at) == datetime(2026, 9, 25, 9, 0, tzinfo=clock.tz())
    assert clock.next_at(15, 0, at).day == 25  # строго после
    assert clock.hhmm(9, 5) == "9:05"
