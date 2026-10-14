"""Пул соединений к Postgres (Supabase)."""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg

from .config import settings

EXPECTED_TABLES = [
    "chats", "messages", "cursors", "threads", "signals", "clusters", "runs", "llm_usage",
    "chat_requests", "bot_settings", "authors", "topic_messages",
]

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=5,
            # Supavisor в transaction-режиме (порт 6543) не поддерживает
            # серверные prepared statements. На session pooler безвредно.
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def log_run(kind: str, stats: dict, error: str | None = None) -> None:
    import json

    pool = await get_pool()
    await pool.execute(
        "insert into runs (kind, finished_at, stats, error) values ($1, now(), $2, $3)",
        kind,
        json.dumps(stats, ensure_ascii=False, default=str),
        error,
    )


def schema_path() -> Path:
    """db/schema.sql — рядом с пакетом (wheel) или в корне репозитория (editable)."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parent / "schema.sql",
        here.parents[2] / "db" / "schema.sql",
        Path("db/schema.sql"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("не найден db/schema.sql")


async def apply_schema() -> list[str]:
    """Накатывает схему. Скрипт идемпотентный, повторный запуск безопасен."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(schema_path().read_text(encoding="utf-8"))
    return await missing_tables()


async def missing_tables() -> list[str]:
    pool = await get_pool()
    rows = await pool.fetch(
        "select tablename from pg_tables where schemaname = 'public'"
    )
    present = {r["tablename"] for r in rows}
    return [t for t in EXPECTED_TABLES if t not in present]


def explain_error(error: BaseException | str) -> str | None:
    """Что делать при частых ошибках подключения к Supabase; None — подсказки нет."""
    text = str(error)
    low = text.lower()
    if "tenant/user" in low or "tenant or user not found" in low:
        ref = re.search(r"postgres\.([a-z0-9]+)", text)
        project = f" «{ref.group(1)}»" if ref else ""
        return (
            f"Supabase не нашёл проект{project}. Чаще всего проект на паузе: на бесплатном "
            "тарифе Supabase ставит его на паузу, если неделю к нему никто не обращался.\n"
            "1. Открой https://supabase.com/dashboard → свой проект. Если там «Paused» — нажми "
            "«Restore project» и подожди 2–5 минут, пока он запустится.\n"
            "2. Если проект активен — в DATABASE_URL неверный адрес пулера: в Supabase нажми "
            "Connect → Session pooler, скопируй строку целиком и вставь в .env, подставив свой "
            "пароль (регион и начало адреса, aws-0 или aws-1, должны совпасть)."
        )
    if "password authentication failed" in low:
        return ("Неверный пароль базы в DATABASE_URL. В Supabase: Project Settings → Database → "
                "Reset database password, потом обнови пароль в .env.")
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
        return ("Не получилось найти сервер базы. Проверь интернет. Если в DATABASE_URL адрес "
                "db.….supabase.co — замени строку на Session pooler (Connect → Session pooler).")
    if "max client connections" in low or "too many connections" in low:
        return ("У базы закончились подключения. Проверь, не запущена ли вторая копия бота "
                "(ещё одно окно start-bot.bat), и закрой лишнюю.")
    if isinstance(error, TimeoutError) or "timed out" in low or "таймаут" in low:
        return ("Сервер базы не отвечает. Проверь интернет и что проект в Supabase не на паузе "
                "(https://supabase.com/dashboard).")
    return None


def safe_dsn() -> str:
    """Строка подключения без пароля — для вывода в консоль."""
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", settings.database_url)


async def get_setting(key: str, default: str | None = None) -> str | None:
    pool = await get_pool()
    value = await pool.fetchval("select value from bot_settings where key = $1", key)
    return default if value is None else value


async def set_setting(key: str, value: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        insert into bot_settings (key, value) values ($1, $2)
        on conflict (key) do update set value = excluded.value, updated_at = now()
        """,
        key, value,
    )
