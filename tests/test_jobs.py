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


# ---------- защита и оценки ----------

import pytest  # noqa: E402

from chat_parser.bot import jobs  # noqa: E402
from chat_parser.bot.format import extract_choices, fmt_estimate, threads_word  # noqa: E402


@pytest.mark.asyncio
async def test_second_heavy_job_is_refused():
    async with jobs.exclusive("разбор сигналов"):
        assert jobs.is_running()
        with pytest.raises(jobs.Busy) as err:
            async with jobs.exclusive("группировка болей"):
                pass
        assert err.value.what == "разбор сигналов"
    assert not jobs.is_running()


@pytest.mark.asyncio
async def test_slot_is_released_after_error():
    with pytest.raises(RuntimeError):
        async with jobs.exclusive("x"):
            raise RuntimeError("упало")
    assert not jobs.is_running()


def test_per_thread_from_extract_and_pipeline_stats():
    ext = {"threads": 19, "failed": 1, "tokens": {"prompt": 60_000, "completion": 40_000}}
    assert jobs.per_thread_from_stats("extract", ext) == 5_000
    assert jobs.per_thread_from_stats("pipeline", {"extract": ext}) == 5_000
    assert jobs.per_thread_from_stats("extract", {"threads": 0, "failed": 0}) is None
    assert jobs.per_thread_from_stats("pipeline", {"extract": "пропущено"}) is None


def test_plural_forms():
    assert [threads_word(n) for n in (1, 3, 5, 11, 21, 22, 1000)] == [
        "1 тред", "3 треда", "5 тредов", "11 тредов", "21 тред", "22 треда", "1 000 тредов",
    ]


def test_extract_choices_never_offer_more_than_queue():
    assert extract_choices(3) == [("Все 3", "all")]
    assert [v for _, v in extract_choices(150)] == ["20", "100", "all"]
    assert extract_choices(0) == []


def test_estimate_text():
    assert "Оценки расхода пока нет" in fmt_estimate(50, None)
    assert "≈ <b>250 000</b> токенов" in fmt_estimate(50, 5_000)
    assert "Очередь пуста" in fmt_estimate(0, 5_000)
