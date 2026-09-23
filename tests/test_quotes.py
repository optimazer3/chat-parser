from chat_parser.bot.format import fmt_card
from chat_parser.links import message_link
from chat_parser.quotes import pick_quotes

SIGNALS = [  # самые острые первыми
    {"id": 1, "evidence_quote": "жду оправы по 6 недель, клиенты уходят"},
    {"id": 2, "evidence_quote": "пупиллометр один на два салона"},
    {"id": 3, "evidence_quote": "жду оправы по 6 недель, клиенты уходят"},  # дубль текста
    {"id": 4, "evidence_quote": "второй брать за 40к жаба душит"},
]


def test_links_by_chat_kind():
    assert message_link(-1001234567890, None, 55) == "https://t.me/c/1234567890/55"
    assert message_link(-1001234567890, "optics_chat", 55) == "https://t.me/optics_chat/55"
    assert message_link(-987654, None, 55) is None       # обычная группа — ссылок нет
    assert message_link(-1001234567890, None, None) is None


def test_new_cards_use_model_choice_in_order():
    card = {"evidence_ids": [4, 2, 999]}  # 999 — чужой номер, пропускаем
    assert [q["id"] for q in pick_quotes(card, SIGNALS)] == [4, 2]


def test_old_cards_with_text_quotes_get_matched_to_signals():
    card = {"evidence": ["«пупиллометр один на два салона»", "чего-то нет в сигналах"]}
    assert [q["id"] for q in pick_quotes(card, SIGNALS)] == [2]


def test_no_card_means_sharpest_without_duplicates():
    assert [q["id"] for q in pick_quotes(None, SIGNALS, limit=3)] == [1, 2, 4]


def test_card_text_has_links_and_plain_wording():
    cluster = {"id": 7, "label": "Долгие поставки", "audience": "owner",
               "statement": "жду оправы месяцами", "n_authors": 3, "n_chats": 1,
               "n_signals": 5, "score": 6.1,
               "card": {"who": "владельцы", "when": "при заказе", "current_workarounds": [],
                        "evidence_ids": [1], "product_hypotheses": [],
                        "open_questions": ["как часто"]}}
    quotes = [{"evidence_quote": "клиенты <уходят>", "link": "https://t.me/c/1/2"}]
    text = fmt_card(cluster, quotes)
    assert '<a href="https://t.me/c/1/2">↗ сообщение в чате</a>' in text
    assert "клиенты &lt;уходят&gt;" in text          # цитата экранирована
    assert "<b>Выяснить:</b>" in text and "интервью" not in text
    assert "Говорят 3 человека в 1 чате, 5 сигналов." in text
