from chat_parser.analyze.schema import Extraction, PersonHint
from chat_parser.analyze.validate import validate_people
from chat_parser.bot.format import fmt_person, fmt_quote
from chat_parser.people import display, split_company_role, who_command

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
