"""Пул соединений к Postgres (Supabase)."""

from __future__ import annotations

import asyncpg

from .config import settings

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
