import pytest

from chat_parser.bot.format import messages_word
from chat_parser.links import BadChatRef, chat_link, normalize_chat_ref


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@Optika_Pro", "https://t.me/optika_pro"),
        ("t.me/Optika_Pro", "https://t.me/optika_pro"),
        ("https://t.me/optika_pro/", "https://t.me/optika_pro"),
        ("  https://telegram.me/optika_pro  ", "https://t.me/optika_pro"),
        ("https://t.me/optika_pro/1234", "https://t.me/optika_pro"),  # ссылка на сообщение
        ("https://t.me/+Wfx5U9BAtSRiODVi", "https://t.me/+Wfx5U9BAtSRiODVi"),  # регистр хэша важен
        ("https://t.me/joinchat/Wfx5U9BAtSRiODVi", "https://t.me/+Wfx5U9BAtSRiODVi"),
    ],
)
def test_same_chat_same_link(raw, expected):
    assert normalize_chat_ref(raw) == expected


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("https://t.me/c/1234567890/55", "приватном чате"),
        ("привет", "не похоже на ссылку"),
        ("https://t.me/ab", "не разобрал"),
    ],
)
def test_bad_refs_explain_what_to_send(raw, fragment):
    with pytest.raises(BadChatRef, match=fragment):
        normalize_chat_ref(raw)


def test_chat_link_preference():
    assert chat_link(-1001234567890, "optika", 99, None) == "https://t.me/optika"
    assert chat_link(-1001234567890, None, 99, None) == "https://t.me/c/1234567890/99"
    assert chat_link(-555, None, 99, "https://t.me/+abcdefghij") == "https://t.me/+abcdefghij"
    assert chat_link(-555, None, None, None) is None


def test_messages_word():
    assert [messages_word(n) for n in (1, 3, 5, 21, 1500)] == [
        "1 сообщение", "3 сообщения", "5 сообщений", "21 сообщение", "1 500 сообщений",
    ]


def test_time_shown_in_user_timezone(monkeypatch):
    from datetime import datetime, timezone

    from chat_parser.bot.format import when
    from chat_parser.config import settings

    ts = datetime(2026, 9, 23, 15, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(settings, "display_utc_offset", 3)
    assert when(ts) == "23.09 в 18:01"
    assert when(ts, with_year=True) == "23.09.2026 в 18:01"
    monkeypatch.setattr(settings, "display_utc_offset", 7)
    assert when(datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)) == "24.09 в 03:00"
