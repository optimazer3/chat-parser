"""Пул соединений к Postgres (Supabase)."""

from __future__ import annotations

from pathlib import Path

import asyncpg

from .config import settings

EXPECTED_TABLES = [
    "chats", "messages", "cursors", "threads", "signals", "clusters", "runs", "llm_usage",
    "chat_requests",
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


def safe_dsn() -> str:
    """Строка подключения без пароля — для вывода в консоль."""
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", settings.database_url)
