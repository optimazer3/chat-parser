from datetime import datetime, timezone

from chat_parser.bot.format import chunks
from chat_parser.bot.jobs import seconds_until


def test_seconds_until_later_today():
    now = datetime(2026, 3, 1, 4, 0, tzinfo=timezone.utc)
    assert seconds_until(6, now) == 2 * 3600


def test_seconds_until_rolls_over_midnight_and_month_end():
    now = datetime(2026, 3, 31, 7, 0, tzinfo=timezone.utc)
    assert seconds_until(6, now) == 23 * 3600


def test_chunks_respect_telegram_limit():
    text = "\n".join(f"строка номер {i}" for i in range(500))
    parts = chunks(text, limit=200)
    assert all(len(p) <= 200 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
