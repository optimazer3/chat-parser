"""Тяжёлые операции, которые бот запускает в фоне.

Одна операция за раз: параллельные выгрузки одним аккаунтом Telegram ведут
к флуд-бану, параллельный разбор задваивает работу и деньги, а импорт посреди
разбора может вернуть в очередь тред, который как раз обрабатывается.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from .. import db, usage
from ..analyze import extract as extract_mod
from ..cluster import llm_cluster
from ..config import settings
from ..ingest import collector, tdesktop
from ..ingest.client import build_client
from ..normalize import threads as threads_mod
from ..report import build_md

REPORT_PATH = Path("out/report.md")

T = TypeVar("T")

Progress = Callable[[str], Awaitable[None]]
ExtractProgress = Callable[[int, int, dict], Awaitable[None]]

_current: dict[str, Any] | None = None


class Busy(RuntimeError):
    """Уже идёт другая тяжёлая операция."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


class Cancelled(RuntimeError):
    """Операцию остановили кнопкой."""


def new_job_id() -> str:
    return uuid.uuid4().hex[:8]


@asynccontextmanager
async def exclusive(name: str, job_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """Проверка и захват без await между ними — гонки в одном event loop нет.

    Запоминаем задачу, в которой идёт операция, чтобы её можно было
    остановить кнопкой (cancel).
    """
    global _current
    if _current is not None:
        raise Busy(_current["name"])
    _current = {
        "name": name,
        "id": job_id or new_job_id(),
        "task": asyncio.current_task(),
        "progress": "",
        "done": 0,
        "signals": 0,
        "since": datetime.now(timezone.utc),
    }
    try:
        yield _current
    finally:
        _current = None


def current() -> dict[str, Any] | None:
    return _current


def is_running() -> bool:
    return _current is not None


def cancel(job_id: str) -> str | None:
    """Остановить операцию с этим id. None — такой операции уже нет."""
    job = _current
    if job is None or job["id"] != job_id or job["task"] is None:
        return None
    job["task"].cancel()
    return job["name"]


async def run_job(coro: Awaitable[T]) -> T:
    """Запустить операцию отдельной задачей и дождаться её.

    Отдельная задача нужна, чтобы кнопка «Остановить» отменяла именно
    операцию, а не того, кто её ждёт: обработчик сообщения или ночной
    планировщик. Остановка превращается в Cancelled; отмена самого
    ожидающего (выключение бота) пробрасывается как есть.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await task
    except asyncio.CancelledError:
        me = asyncio.current_task()
        if task.cancelled() and not (me is not None and me.cancelling()):
            raise Cancelled from None
        task.cancel()
        raise


async def _noop(*_: Any) -> None:
    return None


# ------------------------------------------------------------- оценки и счётчики


async def queue_size() -> int:
    pool = await db.get_pool()
    return await pool.fetchval("select count(*) from threads where status = 'pending'")


async def seconds_per_thread() -> float | None:
    """Среднее время разбора одного обсуждения по последним разборам."""
    return await usage.seconds_per_thread(await db.get_pool())


async def last_pipeline_at() -> datetime | None:
    pool = await db.get_pool()
    return await pool.fetchval(
        "select max(finished_at) from runs where kind = 'pipeline' and error is null"
    )


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


# ------------------------------------------------------------------ операции


async def import_export(path: Path) -> dict[str, Any]:
    """Импорт экспорта Telegram Desktop и сразу — сборка тредов этого чата."""
    async with exclusive("импорт истории"):
        pool = await db.get_pool()
        stats = await tdesktop.import_file(pool, path)
        stats["threads"] = await threads_mod.build_for_chat(pool, stats["chat_id"])
        return stats


async def reset_queue(kind: str) -> int:
    """kind: 'redo' — вернуть разобранные и упавшие; 'failed' — только упавшие."""
    async with exclusive("возврат в очередь"):
        pool = await db.get_pool()
        if kind == "redo":
            return await extract_mod.reset_for_redo(pool)
        return await extract_mod.reset_failed(pool)


async def run_extract(
    on_progress: ExtractProgress, limit: int | None, job_id: str | None = None
) -> dict[str, Any]:
    async with exclusive("разбор", job_id) as job:

        async def cb(done: int, total: int, stats: dict) -> None:
            job["progress"] = f"{done} из {total}"
            job["done"], job["signals"] = done, stats["signals"]
            await on_progress(done, total, stats)

        pool = await db.get_pool()
        stats = await extract_mod.run(pool, limit, on_progress=cb)
        await db.log_run("extract", stats)
        return stats


async def run_cluster(progress: Progress, job_id: str | None = None) -> dict[str, Any]:
    async with exclusive("пересчёт болей", job_id) as job:
        pool = await db.get_pool()
        job["progress"] = "группировка сигналов"
        await progress("🧩 Группирую сигналы в боли…")
        clusters = await llm_cluster.run(pool)
        n = sum(v for v in clusters.values() if isinstance(v, int))

        async def cards_cb(i: int, total: int) -> None:
            job["progress"] = f"описание болей {i} из {total}"
            await progress(f"📝 Болей: {n}. Описываю каждую: {i} из {total}…")

        cards = await llm_cluster.make_cards(pool, on_progress=cards_cb)
        await build_md.build(pool, REPORT_PATH)
        stats = {"cluster": clusters, "cards": cards, "clusters_total": n}
        await db.log_run("cluster", stats)
        return stats


async def run_pipeline(
    progress: Progress, extract_limit: int | None = None, job_id: str | None = None
) -> dict[str, Any]:
    """Полный цикл: выгрузка -> обсуждения -> разбор -> боли -> отчёт."""
    async with exclusive("полный цикл", job_id) as job:
        started = datetime.now(timezone.utc)
        stats: dict[str, Any] = {}
        try:
            pool = await db.get_pool()

            if settings.telegram_ready:
                job["progress"] = "выгрузка"
                await progress("📥 Забираю новые сообщения…")
                client = build_client()
                await client.start()
                try:
                    stats["ingest"] = await collector.sync_all(client, pool, "incremental")
                finally:
                    await client.disconnect()
            else:
                stats["ingest"] = "пропущено: TG_API_ID/TG_API_HASH не заданы"

            job["progress"] = "сборка обсуждений"
            await progress("🧵 Собираю обсуждения…")
            stats["threads"] = await threads_mod.build_all(pool)

            async def ex_cb(done: int, total: int, st: dict) -> None:
                job["progress"] = f"разбор {done} из {total}"
                job["done"], job["signals"] = done, st["signals"]
                await progress(
                    f"🧠 Разбираю обсуждения: {done} из {total} · сигналов {st['signals']}"
                )

            stats["extract"] = await extract_mod.run(pool, extract_limit, on_progress=ex_cb)

            job["progress"] = "группировка сигналов"
            await progress(f"🧩 Сигналов: {stats['extract']['signals']}. Группирую в боли…")
            stats["cluster"] = await llm_cluster.run(pool)

            async def cards_cb(i: int, total: int) -> None:
                job["progress"] = f"описание болей {i} из {total}"
                await progress(f"📝 Описываю боли: {i} из {total}…")

            stats["cards"] = await llm_cluster.make_cards(pool, on_progress=cards_cb)

            await progress("📄 Собираю отчёт…")
            await build_md.build(pool, REPORT_PATH)

            stats["started_at"] = started
            await db.log_run("pipeline", stats)
            return stats
        except Exception as e:
            await db.log_run("pipeline", stats, error=f"{type(e).__name__}: {e}")
            raise


def seconds_until(hour_utc: int, now: datetime | None = None) -> float:
    """Секунды до ближайшего наступления указанного часа UTC."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
