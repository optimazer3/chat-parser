from datetime import datetime, timezone

from chat_parser.ingest.tdesktop import (
    flatten_text,
    normalize_chat_id,
    parse_date,
    parse_from_id,
    parse_messages,
)
from chat_parser.pii import author_hash

SALT = "test-salt"

EXPORT = {
    "name": "Оптики — обмен опытом",
    "type": "public_supergroup",
    "id": 1234567890,
    "messages": [
        {"id": 1, "type": "service", "action": "join_group_by_link", "date": "2026-02-01T09:00:00"},
        {
            "id": 2,
            "type": "message",
            "date": "2026-02-03T11:24:00",
            "date_unixtime": "1770118here",  # битое значение -> падаем на date
            "from": "Иван",
            "from_id": "user555000111",
            "text": "пупиллометр один на два салона, возим туда-сюда",
        },
        {
            "id": 3,
            "type": "message",
            "date_unixtime": "1770118000",
            "from_id": "user777",
            "reply_to_message_id": 2,
            "text": [
                "пишите на ",
                {"type": "email", "text": "info@optika.ru"},
                ", телефон +7 912 345-67-89",
            ],
            "reactions": [{"type": "emoji", "count": 2}, {"type": "emoji", "count": 3}],
        },
        {"id": 4, "type": "message", "date_unixtime": "1770118100", "from_id": "user777",
         "text": "", "media_type": "sticker"},
    ],
}


def test_flatten_text_handles_both_shapes():
    assert flatten_text("просто строка") == "просто строка"
    assert flatten_text(["а ", {"type": "link", "text": "б"}, " в"]) == "а б в"
    assert flatten_text(None) == ""


def test_chat_id_matches_what_telethon_would_give():
    """Супергруппа у Telethon -100…; иначе тот же чат задвоится при переходе на MTProto."""
    assert normalize_chat_id(1234567890, "public_supergroup") == -1001234567890
    assert normalize_chat_id(-1001234567890, "public_supergroup") == -1001234567890
    assert normalize_chat_id(987654, "private_group") == -987654
    assert normalize_chat_id(42, "personal_chat") == 42


def test_parse_from_id_strips_prefix():
    assert parse_from_id("user555000111") == 555000111
    assert parse_from_id("channel1234") == 1234
    assert parse_from_id(None) is None


def test_parse_date_prefers_unixtime_and_falls_back():
    assert parse_date({"date_unixtime": "1770118000"}) == datetime.fromtimestamp(
        1770118000, tz=timezone.utc
    )
    naive = parse_date({"date": "2026-02-03T11:24:00"})
    assert naive == datetime(2026, 2, 3, 11, 24, tzinfo=timezone.utc)
    assert parse_date({}) is None


def test_parse_messages_skips_service_and_keeps_content():
    rows, skipped = parse_messages(EXPORT, -1001234567890, SALT)
    assert skipped == 1  # служебное join_group_by_link
    assert [r[1] for r in rows] == [2, 3, 4]


def test_author_hash_matches_mtproto_path():
    """Импорт и выгрузка через MTProto должны давать один и тот же псевдоним."""
    rows, _ = parse_messages(EXPORT, -1001234567890, SALT)
    imported = {r[1]: r[3] for r in rows}
    assert imported[2] == author_hash(555000111, SALT)
    assert imported[3] == author_hash(777, SALT)


def test_contacts_are_masked_on_import():
    rows, _ = parse_messages(EXPORT, -1001234567890, SALT)
    text = next(r[6] for r in rows if r[1] == 3)
    assert "<EMAIL>" in text and "<PHONE>" in text
    assert "optika.ru" not in text and "345-67-89" not in text


def test_reply_and_reactions_are_carried_over():
    rows, _ = parse_messages(EXPORT, -1001234567890, SALT)
    row = next(r for r in rows if r[1] == 3)
    assert row[7] == 2      # reply_to
    assert row[10] == 5     # 2 + 3 реакции


def test_media_type_detected():
    rows, _ = parse_messages(EXPORT, -1001234567890, SALT)
    assert next(r[9] for r in rows if r[1] == 4) == "sticker"
