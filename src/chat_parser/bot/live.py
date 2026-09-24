"""Вечерний разбор чатов и итоги дня.

Раз в день, в REPORT_HOUR по местному времени, аккаунт-сборщик забирает
переписку за последние сутки (для контекста — ещё сутки до них), собирает
обсуждения, разбирает те, что шли сегодня, раскладывает сигналы по болям и
присылает один отчёт. Автоматический разбор идёт в дневной бюджет
LIVE_DAILY_BUDGET. Архив, залитый из файлов, не трогает — он разбирается вручную.
"""

from __future__ import annotations

import json
import logging
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
REPORT_DATE_KEY = "daily_report_date"
REPORT_AT_KEY = "daily_report_at"
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
    """С прошлого отчёта, но не дальше суток назад."""
    raw = await db.get_setting(REPORT_AT_KEY)
    last = datetime.fromisoformat(raw) if raw else None
    return max(last, now - DAY) if last else now - DAY


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


async def report_due(at: datetime | None = None) -> bool:
    """Сегодняшний отчёт ещё не отправлен, а его время уже наступило.
    Так отчёт досылается, если в REPORT_HOUR бот был выключен."""
    local = (at or clock.now()).astimezone(clock.tz())
    if local.hour < settings.report_hour:
        return False
    return await db.get_setting(REPORT_DATE_KEY) != local.date().isoformat()


async def build_report(mark_sent: bool = True) -> str:
    """Вечерний проход и отчёт. mark_sent=False — «показать сейчас»:
    вечерний отчёт всё равно придёт."""
    async with jobs.exclusive("итоги дня") as job:
        pool = await db.get_pool()
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
            await db.set_setting(REPORT_DATE_KEY, clock.now().date().isoformat())
            await db.set_setting(REPORT_AT_KEY, until.isoformat())
        return daily.render(data, notes)
