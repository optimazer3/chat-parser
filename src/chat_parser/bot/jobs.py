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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .. import db, usage
from ..analyze import extract as extract_mod
from ..cluster import llm_cluster
from ..config import settings
from ..ingest import collector, tdesktop
from ..ingest.client import connect_client
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
    операцию, а не того, кто её ждёт: обработчик сообщения или фоновую
    задачу бота. Остановка превращается в Cancelled; отмена самого
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
                job["progress"] = "загрузка сообщений"
                await progress("📥 Забираю новые сообщения…")
                client = await connect_client()
                try:
                    stats["requests"] = await _connect_requests(pool, client)
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


# --------------------------------------------------------- добавление чатов


async def save_chat_request(link: str) -> str:
    """Запомнить чат до подключения. Возвращает статус: new | exists | connected."""
    pool = await db.get_pool()
    row = await pool.fetchrow("select status from chat_requests where link = $1", link)
    if row is not None:
        return "connected" if row["status"] == "done" else "exists"
    await pool.execute("insert into chat_requests (link) values ($1)", link)
    return "new"


async def pending_requests() -> list[dict[str, Any]]:
    pool = await db.get_pool()
    return [
        dict(r)
        for r in await pool.fetch(
            "select id, link, status, note from chat_requests "
            "where status <> 'done' order by added_at"
        )
    ]


async def refresh_names(chat_id: int) -> int:
    """Подтянуть имена всех участников чата из Telegram. Нужно для тех, кто
    писал до того, как бот начал запоминать имена."""
    from ..people import upsert_authors
    from ..pii import author_hash, author_label

    async with exclusive("имена участников"):
        pool = await db.get_pool()
        client = await connect_client()
        try:
            entity = await client.get_entity(chat_id)
            items = []
            async for user in client.iter_participants(entity, limit=10000):
                h = author_hash(user.id, settings.author_salt)
                name = " ".join(x for x in (user.first_name, user.last_name) if x) or None
                items.append((h, author_label(h), name, user.username))
        finally:
            await client.disconnect()
        async with pool.acquire() as conn:
            await upsert_authors(conn, items)
        return len(items)


async def delete_request(request_id: int) -> str | None:
    pool = await db.get_pool()
    return await pool.fetchval(
        "delete from chat_requests where id = $1 returning link", request_id
    )


async def delete_chat(chat_id: int) -> dict[str, Any] | None:
    """Перестать следить за чатом и удалить его данные. Боли, где после этого
    не осталось сигналов, убираются, остальные пересчитываются."""
    async with exclusive("удаление чата"):
        pool = await db.get_pool()
        info = await pool.fetchrow(
            """
            select title,
                   (select count(*) from messages where chat_id = $1) messages,
                   (select count(*) from signals where chat_id = $1) signals
              from chats where id = $1
            """,
            chat_id,
        )
        if info is None:
            return None
        touched = [r["cluster_id"] for r in await pool.fetch(
            "select distinct cluster_id from signals where chat_id = $1 and cluster_id is not null",
            chat_id,
        )]
        async with pool.acquire() as conn, conn.transaction():
            # чтобы бот не подключил его обратно при запуске
            await conn.execute("delete from chat_requests where chat_id = $1", chat_id)
            # сообщения, курсор, обсуждения и сигналы уходят каскадом
            await conn.execute("delete from chats where id = $1", chat_id)
        removed = await llm_cluster.refresh_stats(pool, touched) if touched else 0
        return {**dict(info), "pains_removed": removed}


async def _mark_request(pool, link: str, status: str, chat_id: int | None = None,
                        note: str | None = None) -> None:
    await pool.execute(
        """
        insert into chat_requests (link, status, chat_id, note) values ($1,$2,$3,$4)
        on conflict (link) do update
           set status = excluded.status, chat_id = coalesce(excluded.chat_id, chat_requests.chat_id),
               note = excluded.note, updated_at = now()
        """,
        link, status, chat_id, note,
    )


async def connect_chat(link: str, join: bool) -> int:
    """Подключить чат по ссылке. NeedsJoin/JoinPending пробрасываются наверх."""
    async with exclusive("подключение чата"):
        pool = await db.get_pool()
        client = await connect_client()
        try:
            chat_id = await collector.register_chat(client, pool, link, join=join)
        except collector.JoinPending as e:
            await _mark_request(pool, link, "join_pending", note=str(e))
            raise
        finally:
            await client.disconnect()
        await _mark_request(pool, link, "done", chat_id)
        return chat_id


async def _connect_requests(pool, client) -> dict[str, list]:
    """Подключить сохранённые чаты. Вступление разрешено: человек сам прислал
    эту ссылку кнопкой «Добавить чат». Не больше join_per_run за раз и с паузой
    между вступлениями — частые вступления злят антиспам Telegram."""
    rows = await pool.fetch(
        "select link from chat_requests where status in ('pending', 'join_pending') "
        "order by added_at limit $1",
        settings.join_per_run,
    )
    result: dict[str, list] = {"connected": [], "waiting": [], "failed": []}
    for i, r in enumerate(rows):
        if i:
            await asyncio.sleep(settings.join_pause)
        link = r["link"]
        try:
            chat_id = await collector.register_chat(client, pool, link, join=True)
        except collector.JoinPending:
            await _mark_request(pool, link, "join_pending", note="ждёт одобрения админа")
            result["waiting"].append(link)
            continue
        except Exception as e:  # noqa: BLE001 — один чат не валит остальные
            await _mark_request(pool, link, "failed", note=f"{type(e).__name__}: {e}")
            result["failed"].append((link, str(e)))
            continue
        await _mark_request(pool, link, "done", chat_id)
        title = await pool.fetchval("select title from chats where id = $1", chat_id)
        result["connected"].append(title or link)
    return result


async def connect_saved_chats() -> dict[str, list]:
    """Отдельно от полного цикла — при запуске бота, когда ключи появились."""
    async with exclusive("подключение сохранённых чатов"):
        pool = await db.get_pool()
        client = await connect_client()
        try:
            return await _connect_requests(pool, client)
        finally:
            await client.disconnect()


async def chats_without_history() -> list[int]:
    pool = await db.get_pool()
    return [
        r["id"]
        for r in await pool.fetch(
            """
            select c.id from chats c left join cursors cu on cu.chat_id = c.id
             where c.is_active and not coalesce(cu.backfill_done, false)
             order by c.id
            """
        )
    ]


async def run_history(
    chat_ids: list[int], on_progress: Callable[[int], Awaitable[None]], job_id: str | None = None
) -> dict[str, Any]:
    """Загрузить историю чатов и сразу собрать обсуждения."""
    async with exclusive("загрузка истории", job_id) as job:
        pool = await db.get_pool()
        client = await connect_client()
        saved = 0
        try:
            for chat_id in chat_ids:
                base = saved

                async def cb(n: int, base: int = base) -> None:
                    job["progress"] = f"загружено {n + base} сообщений"
                    await on_progress(n + base)

                res = await collector.sync_chat_full(client, pool, chat_id, on_progress=cb)
                saved += res.get("saved", 0)
        finally:
            await client.disconnect()
        threads = 0
        for chat_id in chat_ids:
            threads += (await threads_mod.build_for_chat(pool, chat_id)).get("threads", 0)
        return {"saved": saved, "threads": threads, "chats": len(chat_ids)}
