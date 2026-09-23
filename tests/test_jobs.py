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


# ---------- защита, остановка, оценки ----------

import asyncio  # noqa: E402

import pytest  # noqa: E402

from chat_parser.bot import jobs  # noqa: E402
from chat_parser.bot.format import (  # noqa: E402
    discussions, extract_choices, fmt_duration, fmt_estimate, fmt_extract_result, fmt_stopped,
)


@pytest.mark.asyncio
async def test_second_heavy_job_is_refused():
    async with jobs.exclusive("разбор"):
        assert jobs.is_running()
        with pytest.raises(jobs.Busy) as err:
            async with jobs.exclusive("пересчёт болей"):
                pass
        assert err.value.what == "разбор"
    assert not jobs.is_running()


@pytest.mark.asyncio
async def test_slot_is_released_after_error():
    with pytest.raises(RuntimeError):
        async with jobs.exclusive("x"):
            raise RuntimeError("упало")
    assert not jobs.is_running()


@pytest.mark.asyncio
async def test_stop_button_cancels_only_its_job():
    started = asyncio.Event()

    async def long_job():
        async with jobs.exclusive("разбор", "abc12345"):
            started.set()
            await asyncio.sleep(60)

    waiter = asyncio.create_task(jobs.run_job(long_job()))
    await started.wait()
    assert jobs.cancel("старая-кнопка") is None  # чужой id не останавливает
    assert jobs.cancel("abc12345") == "разбор"
    with pytest.raises(jobs.Cancelled):
        await waiter
    assert not jobs.is_running()  # слот освободился


@pytest.mark.asyncio
async def test_stop_does_not_kill_the_waiter():
    """Остановка — это Cancelled у ожидающего, а не отмена его самого:
    так кнопка не убьёт ни обработчик, ни ночной планировщик."""
    started = asyncio.Event()

    async def long_job():
        async with jobs.exclusive("полный цикл", "job1"):
            started.set()
            await asyncio.sleep(60)

    async def scheduler_like():
        try:
            await jobs.run_job(long_job())
        except jobs.Cancelled:
            return "пережил остановку"

    sched = asyncio.create_task(scheduler_like())
    await started.wait()
    jobs.cancel("job1")
    assert await sched == "пережил остановку"


@pytest.mark.asyncio
async def test_shutdown_still_cancels_everything():
    started = asyncio.Event()

    async def long_job():
        async with jobs.exclusive("разбор"):
            started.set()
            await asyncio.sleep(60)

    waiter = asyncio.create_task(jobs.run_job(long_job()))
    await started.wait()
    waiter.cancel()  # выключение бота
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert not jobs.is_running()


def test_plural_forms():
    assert [discussions(n) for n in (1, 3, 5, 11, 21, 22, 1000)] == [
        "1 обсуждение", "3 обсуждения", "5 обсуждений", "11 обсуждений",
        "21 обсуждение", "22 обсуждения", "1 000 обсуждений",
    ]


def test_extract_choices_never_offer_more_than_queue():
    assert extract_choices(3) == [("Все 3", "all")]
    assert [v for _, v in extract_choices(150)] == ["20", "100", "all"]
    assert extract_choices(0) == []


def test_duration():
    assert fmt_duration(20) == "меньше минуты"
    assert fmt_duration(7 * 60) == "~7 мин"
    assert fmt_duration(80 * 60) == "~1 ч 20 мин"
    assert fmt_duration(119 * 60) == "~2 ч"


def test_estimate_is_time_not_tokens():
    assert "токен" not in fmt_estimate(50, 12.0)
    assert "~10 мин" in fmt_estimate(50, 12.0)
    assert "после первого разбора" in fmt_estimate(50, None)
    assert "Разбирать нечего" in fmt_estimate(0, 12.0)
    assert "меньше минуты" in fmt_estimate(5, 0.0)  # быстро — не значит «нет данных»


def test_extract_result_has_no_technical_numbers():
    stats = {"threads": 19, "signals": 43, "dropped": 1, "empty": 6, "failed": 1,
             "drop_rate": 0.023, "seconds": 192, "pending_left": 842,
             "tokens": {"prompt": 60_000, "completion": 40_000}}
    text = fmt_extract_result(stats)
    assert "токен" not in text and "отбраков" not in text
    assert "Разобрано: 20 обсуждений" in text and "<b>43</b>" in text
    assert "/retry" in text and "842" in text


def test_stopped_message_says_what_is_kept():
    text = fmt_stopped("разбор", {"done": 7, "signals": 12})
    assert "7 обсуждений" in text and "сохранено" in text
