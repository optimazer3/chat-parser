from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich import print

from . import db
from .analyze import extract as extract_mod
from .cluster import llm_cluster
from .ingest import collector
from .ingest.client import build_client
from .normalize import threads as threads_mod
from .report import build_md

app = typer.Typer(no_args_is_help=True, help="Мониторинг чатов рынка оптики")


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await db.close_pool()

    return asyncio.run(wrapper())


@app.command("add-chat")
def add_chat(refs: list[str]) -> None:
    """Завести чаты: @username, ссылка t.me или id."""

    async def go():
        pool = await db.get_pool()
        client = build_client()
        await client.start()
        for ref in refs:
            try:
                cid = await collector.register_chat(client, pool, ref)
                print(f"[green]+[/green] {ref} -> {cid}")
            except Exception as e:  # noqa: BLE001 — показать причину и идти дальше
                print(f"[red]![/red] {ref}: {e}")
        await client.disconnect()

    _run(go())


@app.command()
def ingest(
    mode: str = typer.Option("incremental", help="backfill | incremental"),
    limit: int | None = typer.Option(None, help="Ограничить число сообщений на чат"),
) -> None:
    """Выгрузить историю (backfill) или добрать новое (incremental)."""

    async def go():
        pool = await db.get_pool()
        client = build_client()
        await client.start()
        res = await collector.sync_all(client, pool, mode, limit)
        await client.disconnect()
        for r in res:
            print(r)
        await db.log_run("ingest", {"mode": mode, "chats": res})

    _run(go())


@app.command()
def threads() -> None:
    """Собрать сообщения в треды (диалоги)."""

    async def go():
        pool = await db.get_pool()
        res = await threads_mod.build_all(pool)
        for r in res:
            print(r)
        await db.log_run("threads", {"chats": res})

    _run(go())


@app.command()
def extract(limit: int | None = typer.Option(None, help="Сколько тредов обработать")) -> None:
    """Извлечь сигналы из тредов через Claude."""

    async def go():
        pool = await db.get_pool()
        stats = await extract_mod.run(pool, limit)
        print(stats)
        if stats.get("drop_rate", 0) > 0.05:
            print("[yellow]Отбраковка цитат выше 5% — стоит править промпт.[/yellow]")
        await db.log_run("extract", stats)

    _run(go())


@app.command()
def cluster(cards: bool = typer.Option(True, help="Сгенерировать карточки для топа")) -> None:
    """Сгруппировать сигналы в боли и посчитать веса."""

    async def go():
        pool = await db.get_pool()
        stats = await llm_cluster.run(pool)
        print(stats)
        if cards:
            n = await llm_cluster.make_cards(pool)
            print(f"карточек: {n}")
        await db.log_run("cluster", stats)

    _run(go())


@app.command()
def report(
    out: Path = typer.Option(Path("out/report.md")),
    top: int = typer.Option(30),
) -> None:
    """Собрать markdown-отчёт."""

    async def go():
        pool = await db.get_pool()
        path = await build_md.build(pool, out, top)
        print(f"[green]{path}[/green]")

    _run(go())


@app.command()
def status() -> None:
    """Состояние пайплайна."""

    async def go():
        pool = await db.get_pool()
        rows = await pool.fetch(
            """
            select c.title, c.username, cu.backfill_done, cu.newest_id, cu.last_run,
                   cu.retry_after,
                   (select count(*) from messages m where m.chat_id = c.id) msgs
              from chats c left join cursors cu on cu.chat_id = c.id
             order by c.id
            """
        )
        for r in rows:
            print(dict(r))
        pend = await pool.fetchval("select count(*) from threads where status='pending'")
        print(f"тредов в очереди на анализ: {pend}")

    _run(go())


@app.command()
def pipeline() -> None:
    """Полный цикл мониторинга: ingest -> threads -> extract -> cluster -> report."""

    async def go():
        pool = await db.get_pool()
        client = build_client()
        await client.start()
        ing = await collector.sync_all(client, pool, "incremental")
        await client.disconnect()
        thr = await threads_mod.build_all(pool)
        ext = await extract_mod.run(pool)
        clu = await llm_cluster.run(pool)
        await llm_cluster.make_cards(pool)
        path = await build_md.build(pool, Path("out/report.md"))
        stats = {"ingest": ing, "threads": thr, "extract": ext, "cluster": clu}
        print(json.dumps(stats, ensure_ascii=False, default=str, indent=2))
        print(f"[green]{path}[/green]")
        await db.log_run("pipeline", stats)

    _run(go())


if __name__ == "__main__":
    app()
