"""Фоновый прогон пайплайна из бота.

Один прогон за раз: параллельные выгрузки одним аккаунтом Telegram — прямой
путь к флуд-бану, а параллельные extract задвоят сигналы.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import db
from ..analyze import extract as extract_mod
from ..cluster import llm_cluster
from ..ingest import collector
from ..config import settings
from ..ingest.client import build_client
from ..normalize import threads as threads_mod
from ..report import build_md

REPORT_PATH = Path("out/report.md")

Progress = Callable[[str], Awaitable[None]]

_running = False


class Busy(RuntimeError):
    """Прогон уже идёт."""


def is_running() -> bool:
    return _running


async def last_pipeline_at() -> datetime | None:
    pool = await db.get_pool()
    return await pool.fetchval(
        "select max(finished_at) from runs where kind = 'pipeline' and error is null"
    )


async def run_pipeline(progress: Progress) -> dict[str, Any]:
    global _running
    if _running:
        raise Busy
    _running = True
    started = datetime.now(timezone.utc)
    stats: dict[str, Any] = {}
    try:
        pool = await db.get_pool()

        if settings.telegram_ready:
            await progress("📥 Забираю новые сообщения…")
            client = build_client()
            await client.start()
            try:
                stats["ingest"] = await collector.sync_all(client, pool, "incremental")
            finally:
                await client.disconnect()
            saved = sum(r.get("saved", 0) for r in stats["ingest"])
            await progress(f"🧵 Новых сообщений: {saved}. Собираю диалоги…")
        else:
            # Без TG_API_ID работаем по тому, что залито вручную.
            stats["ingest"] = "пропущено: TG_API_ID/TG_API_HASH не заданы"
            await progress("🧵 Выгрузка выключена, работаю по залитым данным…")
        stats["threads"] = await threads_mod.build_all(pool)

        pending = await pool.fetchval("select count(*) from threads where status = 'pending'")
        await progress(f"🧠 Тредов на анализ: {pending}. Извлекаю сигналы…")
        stats["extract"] = await extract_mod.run(pool)

        await progress(
            f"🧩 Сигналов: {stats['extract'].get('signals', 0)}. Группирую в боли…"
        )
        stats["cluster"] = await llm_cluster.run(pool)
        stats["cards"] = await llm_cluster.make_cards(pool)

        await progress("📄 Собираю отчёт…")
        await build_md.build(pool, REPORT_PATH)

        stats["started_at"] = started
        await db.log_run("pipeline", stats)
        return stats
    except Exception as e:
        await db.log_run("pipeline", stats, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        _running = False


def seconds_until(hour_utc: int, now: datetime | None = None) -> float:
    """Секунды до ближайшего наступления указанного часа UTC."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def since_counts(since: datetime | None) -> tuple[int, int]:
    """(новых сообщений, новых сигналов) с момента since."""
    pool = await db.get_pool()
    if since is None:
        row = await pool.fetchrow(
            "select (select count(*) from messages) m, (select count(*) from signals) s"
        )
    else:
        row = await pool.fetchrow(
            "select (select count(*) from messages where ts > $1) m,"
            " (select count(*) from signals where created_at > $1) s",
            since,
        )
    return row["m"], row["s"]


async def wait_for_free(timeout: float = 0.0) -> bool:
    """Ждёт освобождения слота. Используется планировщиком, чтобы не терять прогон."""
    waited = 0.0
    while _running and waited < timeout:
        await asyncio.sleep(5)
        waited += 5
    return not _running
