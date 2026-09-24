"""Вечерний разбор чатов и итоги дня.

Раз в день, в выбранное время (кнопка ⏰ в боте; по умолчанию REPORT_HOUR
по местному времени), аккаунт-сборщик забирает
переписку за последние сутки (для контекста — ещё сутки до них), собирает
обсуждения, разбирает те, что шли сегодня, раскладывает сигналы по болям и
присылает один отчёт. Автоматический разбор идёт в дневной бюджет
LIVE_DAILY_BUDGET. Архив, залитый из файлов, не трогает — он разбирается вручную.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import clock, db, usage
from ..analyze import extract as extract_mod
from ..analyze.prompts import SYSTEM_DAY
from ..analyze.schema import DayDigest
from ..cluster import llm_cluster
from ..cluster.assign import assign_new
from ..config import settings
from ..ingest import collector
from ..ingest.client import connect_client
from ..llm import build_llm
from ..normalize import threads as threads_mod
from ..report import daily
from . import jobs

log = logging.getLogger("chat_parser.live")

ENABLED_KEY = "live_enabled"
BUDGET_HIT_KEY = "live_budget_hit"
LAST_RUN_KEY = "live_last_run"
ERRORS_KEY = "live_errors"
REPORT_DATE_KEY = "daily_report_date"   # за какой день отчёт уже отправлен
REPORT_AT_KEY = "daily_report_at"       # когда закрыт период прошлого отчёта
REPORT_TIME_KEY = "report_time"         # «21:30», выбранное в боте
NEXT_REPORT_KEY = "report_next_at"      # когда присылать следующий
DAY = timedelta(hours=24)
CONTEXT = timedelta(hours=48)  # сутки отчёта + сутки до них для контекста


async def is_enabled() -> bool:
    return await db.get_setting(ENABLED_KEY, "1") == "1"


async def set_enabled(on: bool) -> None:
    await db.set_setting(ENABLED_KEY, "1" if on else "0")


def budget_enabled() -> bool:
    return settings.live_daily_budget > 0 and bool(settings.llm_price_in or settings.llm_price_out)


async def spent_today() -> float:
    return await usage.spent_since(await db.get_pool(), clock.day_start(), source="live")


async def budget_left() -> float | None:
    """Сколько осталось на автоматический разбор сегодня; None — лимита нет."""
    if not budget_enabled():
        return None
    return settings.live_daily_budget - await spent_today()


async def budget_hit_today() -> datetime | None:
    raw = await db.get_setting(BUDGET_HIT_KEY)
    if not raw:
        return None
    at = datetime.fromisoformat(raw)
    return at if at >= clock.day_start() else None


async def _period_start(now: datetime) -> datetime:
    """С прошлого отчёта (после смены времени между отчётами бывает больше
    суток), но не дальше двух суток назад. Первый отчёт — за сутки."""
    raw = await db.get_setting(REPORT_AT_KEY)
    last = datetime.fromisoformat(raw) if raw else None
    return max(last, now - CONTEXT) if last else now - DAY


# ------------------------------------------------------------ расписание

# Время поменяли в боте — цикл отчёта просыпается и пересчитывает ожидание.
schedule_changed = asyncio.Event()

TIME_RE = re.compile(r"(?:в\s*)?(\d{1,2})(?:\s*[:.\-ч ]\s*(\d{2}))?(?:\s*(?:ч|час|часа|часов))?")


def parse_time(text: str) -> tuple[int, int] | None:
    """«21:30», «21.30», «9», «в 9:05» -> (час, минута). Иначе None."""
    m = TIME_RE.fullmatch(text.strip().lower())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


async def report_time() -> tuple[int, int]:
    raw = await db.get_setting(REPORT_TIME_KEY)
    if raw:
        hour, minute = raw.split(":")
        return int(hour), int(minute)
    return settings.report_hour % 24, 0


async def _sent_today() -> bool:
    return await db.get_setting(REPORT_DATE_KEY) == clock.now().date().isoformat()


async def next_report_at() -> datetime:
    raw = await db.get_setting(NEXT_REPORT_KEY)
    if raw:
        return datetime.fromisoformat(raw)
    # Расписания ещё нет: сегодняшний отчёт, если его не было, — даже если
    # время уже прошло (бот был выключен), тогда он придёт сразу.
    hour, minute = await report_time()
    today = clock.at_time(hour, minute)
    return today + DAY if await _sent_today() else today


async def report_due(at: datetime | None = None) -> bool:
    """Время отчёта наступило. Бот был выключен — отчёт досылается при запуске."""
    return (at or clock.now()) >= await next_report_at()


async def _advance(pending: datetime) -> None:
    """Отчёт за pending отправлен (или отменён) — следующий по расписанию,
    не раньше следующего дня: даже если время успели поменять на более позднее."""
    hour, minute = await report_time()
    pending = pending.astimezone(clock.tz())
    nxt = clock.next_at(hour, minute, max(clock.now(), pending))
    if nxt.date() <= pending.date():
        nxt += DAY
    await db.set_setting(NEXT_REPORT_KEY, nxt.isoformat())


async def skip_pending() -> None:
    """Вечерний отчёт остановили кнопкой ⏹ — сегодня больше не пытаемся."""
    await _advance(await next_report_at())


async def set_report_time(hour: int, minute: int) -> tuple[datetime, bool]:
    """Новое время. Возвращает (когда следующий отчёт, пропадает ли сегодняшний).
    Сегодняшний пропадает, если его ещё не было, а новое время уже прошло."""
    sent = await _sent_today()
    today = clock.at_time(hour, minute)
    passed = today <= clock.now()
    nxt = today + DAY if passed or sent else today
    await db.set_setting(REPORT_TIME_KEY, f"{hour:02d}:{minute:02d}")
    await db.set_setting(NEXT_REPORT_KEY, nxt.isoformat())
    schedule_changed.set()
    return nxt, passed and not sent


# ------------------------------------------------------------ вечерний проход


async def collect_day(job: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Забрать переписку, собрать обсуждения и разобрать свежие."""
    pool = await db.get_pool()
    stats: dict[str, Any] = {"saved": 0, "threads": 0, "extracted": 0, "signals": 0,
                             "budget_hit": False, "paused": False, "errors": []}

    chats = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.title, cu.newest_id from chats c left join cursors cu on cu.chat_id = c.id
         where c.is_active order by c.id
        """
    )]
    changed: list[int] = []
    if chats and settings.telegram_ready:
        client = await connect_client()
        try:
            for i, chat in enumerate(chats, 1):
                job["progress"] = f"забираю переписку: чат {i} из {len(chats)}"
                try:
                    if chat["newest_id"] is None:
                        # историю этого чата не загружали — берём сутки и сутки контекста
                        res = await collector.sync_chat(
                            client, pool, chat["id"], "recent", since=now - CONTEXT
                        )
                    else:
                        res = await collector.sync_chat(client, pool, chat["id"], "incremental")
                except Exception as e:  # noqa: BLE001 — один чат не валит остальные
                    stats["errors"].append(
                        f"не удалось прочитать «{chat['title']}»: {e}. Если аккаунт-сборщик "
                        "не состоит в этом чате — добавь его кнопкой ➕ Добавить чат"
                    )
                    continue
                if res.get("error") == "no_access":
                    stats["errors"].append(f"нет доступа к «{chat['title']}» — чат отключён")
                if res.get("saved"):
                    changed.append(chat["id"])
                    stats["saved"] += res["saved"]
        finally:
            await client.disconnect()
    await db.set_setting(ERRORS_KEY, json.dumps(stats["errors"], ensure_ascii=False))

    # Обсуждения по двум суткам: разговор, начатый вчера и продолженный сегодня,
    # модель увидит целиком.
    job["progress"] = "собираю обсуждения"
    for chat_id in changed:
        stats["threads"] += (
            await threads_mod.build_for_chat(pool, chat_id, now - CONTEXT)
        ).get("threads", 0)
    await db.set_setting(LAST_RUN_KEY, now.isoformat())

    if not await is_enabled():
        stats["paused"] = True
        return stats

    batch = max(1, settings.extract_concurrency * 2)
    while True:
        left = await budget_left()
        if left is not None and left <= 0:
            stats["budget_hit"] = True
            if await budget_hit_today() is None:
                await db.set_setting(BUDGET_HIT_KEY, datetime.now(timezone.utc).isoformat())
            break
        done_before = stats["extracted"]

        async def progress(done: int, total: int, st: dict, base: int = done_before) -> None:
            job["progress"] = f"разбираю обсуждения: {base + done}"

        # Всё свежее, что ещё ждёт: и обсуждения, которые вчера шли в момент
        # отчёта, и то, на что вчера не хватило лимита. Старее — архив.
        res = await extract_mod.run(
            pool, batch, on_progress=progress,
            ended_after=now - CONTEXT,
            ended_before=now - timedelta(minutes=settings.live_quiet_minutes),
            usage_source="live",
        )
        n = res["threads"] + res["failed"]
        stats["extracted"] += n
        stats["signals"] += res["signals"]
        if n < batch:
            break
    return stats


async def day_highlights(since: datetime, until: datetime, fresh: datetime) -> list[str]:
    """3-5 выводов дня от нейросети. Мало сигналов — пропускаем."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select s.type, s.audience, s.intensity, s.summary, c.label pain
          from signals s left join clusters c on c.id = s.cluster_id
         where s.created_at >= $1 and s.created_at < $2 and s.ts >= $3
           and s.confidence >= 0.5
         order by s.intensity desc, s.id limit 150
        """,
        since, until, fresh,
    )
    if len(rows) < 3:
        return []
    listing = "\n".join(
        f"- [{r['type']}, {r['audience']}, острота {r['intensity']}]"
        + (f" (боль: {r['pain']})" if r["pain"] else "") + f" {r['summary']}"
        for r in rows
    )
    llm = build_llm(settings.synth_model)
    started, finished = time.monotonic(), False
    try:
        digest = await llm.structured(SYSTEM_DAY, f"Сигналы за день:\n{listing}", DayDigest,
                                      max_tokens=8000)
        finished = True
        return [h.strip() for h in digest.highlights if h.strip()][:5]
    finally:
        await usage.record(pool, "digest", llm, seconds=time.monotonic() - started,
                           cancelled=not finished, source="live")


# ------------------------------------------------------------ дневной отчёт


async def build_report(mark_sent: bool = True, catch_up: bool = False) -> str:
    """Проход и отчёт.

    mark_sent=True — отчёт по расписанию: следующий будет в следующее время.
    catch_up=True — сегодняшние итоги, которые пропали из-за смены времени:
    считаются отправленными, расписание не трогают.
    mark_sent=False — «показать сейчас», отчёт по расписанию всё равно придёт.
    """
    async with jobs.exclusive("итоги дня") as job:
        pool = await db.get_pool()
        # какой отчёт по расписанию закрываем — до того, как время успеют поменять
        pending = await next_report_at() if mark_sent and not catch_up else None
        now = datetime.now(timezone.utc)
        since = await _period_start(now)
        fresh = now - CONTEXT

        run = await collect_day(job, now)
        highlights: list[str] = []
        if not run["paused"]:
            # Раскладка по болям, описания новых болей и выводы дня нужны самому
            # отчёту, поэтому идут и при исчерпанном лимите: это единицы запросов.
            job["progress"] = "раскладываю сигналы по болям"
            assigned = await assign_new(pool, since, fresh, usage_source="live")
            if assigned["new_pains"]:
                job["progress"] = "описываю новые боли"
                await llm_cluster.make_cards(
                    pool, only_ids=assigned["new_pains"], usage_source="live"
                )
        # Период отчёта закрывается после разбора: сигналы, найденные сейчас,
        # входят в этот отчёт, а следующий начнётся с этой отметки.
        until = datetime.now(timezone.utc)
        if not run["paused"]:
            job["progress"] = "пишу выводы дня"
            try:
                highlights = await day_highlights(since, until, fresh)
            except Exception as e:  # noqa: BLE001 — отчёт важнее выводов
                log.warning("выводы дня не получились: %s", e)

        data = await daily.collect(pool, since, until, fresh)
        data["highlights"] = highlights
        backlog = await pool.fetchval(
            "select count(*) from threads where status = 'pending' and ended_at < $1", fresh
        )
        hit = await budget_hit_today()
        notes = {
            "paused": run["paused"],
            "budget_hit_at": hit.astimezone(clock.tz()) if hit else None,
            "errors": run["errors"],
            "backlog": backlog,
        }
        if mark_sent:
            day = clock.now()
            if pending is not None:
                # отчёт за запланированный день: после полуночи досылается вчерашний
                day = min(day, pending.astimezone(clock.tz()))
                await _advance(pending)
            await db.set_setting(REPORT_DATE_KEY, day.date().isoformat())
            await db.set_setting(REPORT_AT_KEY, until.isoformat())
        return daily.render(data, notes)
