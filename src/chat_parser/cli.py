from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import typer
from rich import print

from . import db
from .config import settings
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


@app.command("init-db")
def init_db() -> None:
    """Накатить схему в Supabase. Идемпотентно, повторный запуск безопасен."""

    async def go():
        print(f"БД: [dim]{db.safe_dsn()}[/dim]")
        missing = await db.apply_schema()
        if missing:
            print(f"[red]Не создались таблицы: {', '.join(missing)}[/red]")
            raise typer.Exit(1)
        print(f"[green]Схема применена[/green] ({len(db.EXPECTED_TABLES)} таблиц)")

    _run(go())


DOCTOR_TIMEOUT = 25
TG_CONNECT_TIMEOUT = 15


async def _probe(coro, seconds: int = DOCTOR_TIMEOUT):
    """(результат, None) либо (None, текст ошибки). Не виснет никогда."""
    try:
        return await asyncio.wait_for(coro, seconds), None
    except TimeoutError:
        return None, f"таймаут {seconds}с — хост недоступен или сеть режет соединение"
    except Exception as e:  # noqa: BLE001 — доктор обязан дойти до конца
        return None, f"{type(e).__name__}: {e}"


async def _probe_db():
    pool = await db.get_pool()
    ver = await pool.fetchval("select version()")
    missing = await db.missing_tables()
    counts = None
    if not missing:
        counts = await pool.fetchrow(
            "select (select count(*) from chats) chats,"
            " (select count(*) from messages) msgs,"
            " (select count(*) from threads) thr,"
            " (select count(*) from signals) sig"
        )
    return ver, missing, counts


async def _probe_telegram():
    # Свой таймаут на connect: если отменять его снаружи, Telethon оставляет
    # висящие фоновые задачи и засоряет вывод трейсбеками.
    client = build_client()
    try:
        try:
            await asyncio.wait_for(client.connect(), TG_CONNECT_TIMEOUT)
        except TimeoutError as e:
            raise RuntimeError(
                f"не удалось подключиться к Telegram за {TG_CONNECT_TIMEOUT}с — "
                "проверь сеть, VPN или прокси"
            ) from e
        if not await client.is_user_authorized():
            return None
        return await client.get_me()
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _probe_anthropic():
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=1)
    return await client.models.retrieve(settings.model_extract)


@app.command()
def doctor() -> None:
    """Проверить окружение целиком. Вывод можно скопировать целиком в чат."""

    async def go():
        # Telethon шумит в лог при обрыве соединения — доктору это не нужно.
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        logging.getLogger("telethon").setLevel(logging.CRITICAL)
        problems: list[str] = []

        print("[bold]1. Конфигурация[/bold]")
        if settings.author_salt in ("", "change-me"):
            print('  [red]FAIL[/red] AUTHOR_SALT не задан — сгенерируй:')
            print('        python -c "import secrets; print(secrets.token_hex(16))"')
            problems.append("AUTHOR_SALT")
        else:
            print("  [green]OK[/green]   AUTHOR_SALT задан")
        if not settings.anthropic_api_key:
            print("  [red]FAIL[/red] ANTHROPIC_API_KEY пуст")
            problems.append("ANTHROPIC_API_KEY")
        print(f"  [dim]модели: {settings.model_extract} / {settings.model_synth}[/dim]")

        print("\n[bold]2. База данных[/bold]")
        print(f"  [dim]{db.safe_dsn()}[/dim]")
        res, err = await _probe(_probe_db())
        if err:
            print(f"  [red]FAIL[/red] {err}")
            print("  [dim]подсказка: в Supabase бери Session pooler (порт 5432),")
            print("  [dim]прямое подключение у новых проектов только по IPv6[/dim]")
            problems.append("БД")
        else:
            ver, missing, counts = res
            print(f"  [green]OK[/green]   {ver.split(',')[0]}")
            if missing:
                print(f"  [red]FAIL[/red] нет таблиц: {', '.join(missing)}"
                      " — запусти chat-parser init-db")
                problems.append("схема")
            else:
                print(f"  [green]OK[/green]   схема на месте · чатов {counts['chats']},"
                      f" сообщений {counts['msgs']}, тредов {counts['thr']},"
                      f" сигналов {counts['sig']}")

        print("\n[bold]3. Telegram[/bold]")
        me, err = await _probe(_probe_telegram())
        if err:
            print(f"  [red]FAIL[/red] {err}")
            problems.append("Telegram")
        elif me is None:
            print("  [red]FAIL[/red] сессия не авторизована — python scripts/login.py")
            problems.append("Telegram")
        else:
            print(f"  [green]OK[/green]   вошли как {me.first_name} "
                  f"(@{me.username}), id {me.id}")

        print("\n[bold]4. Anthropic[/bold]")
        model, err = await _probe(_probe_anthropic())
        if err:
            print(f"  [red]FAIL[/red] {err}")
            problems.append("Anthropic")
        else:
            print(f"  [green]OK[/green]   {model.display_name} доступна")

        print()
        if problems:
            print(f"[red bold]Не готово: {', '.join(problems)}.[/red bold] См. выше.")
            raise typer.Exit(1)
        print("[green bold]Всё готово.[/green bold] "
              "Дальше: chat-parser add-chat <ссылка> --join")

    _run(go())


@app.command("add-chat")
def add_chat(
    refs: list[str],
    join: bool = typer.Option(
        False, "--join", help="Вступить в приватный чат по инвайт-ссылке"
    ),
) -> None:
    """Завести чаты: @username, ссылка t.me (в т.ч. приватная t.me/+hash) или id."""

    async def go():
        pool = await db.get_pool()
        client = build_client()
        await client.start()
        for ref in refs:
            try:
                cid = await collector.register_chat(client, pool, ref, join=join)
                title = await pool.fetchval("select title from chats where id = $1", cid)
                print(f"[green]+[/green] {ref} -> {cid} «{title}»")
            except collector.NeedsJoin as e:
                print(f"[yellow]?[/yellow] {e}")
            except collector.JoinPending as e:
                print(f"[yellow]~[/yellow] {e}")
            except Exception as e:  # noqa: BLE001 — показать причину и идти дальше
                print(f"[red]![/red] {ref}: {type(e).__name__}: {e}")
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
