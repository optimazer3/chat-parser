"""Учёт расхода модели.

Пользователю бота цифры токенов не показываются — они видны только по
скрытой команде /usage и в консоли. Запись идёт по одной строке на
операцию, в том числе остановленную: токены, потраченные до остановки,
тоже стоят денег.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger("chat_parser.usage")

PERIODS = (("за 24 часа", "1 day"), ("за 7 дней", "7 days"), ("за 30 дней", "30 days"))


async def record(
    pool: asyncpg.Pool,
    stage: str,
    llm: Any,
    *,
    threads: int = 0,
    seconds: float = 0,
    cancelled: bool = False,
) -> None:
    """Никогда не бросает: сбой учёта не должен ронять разбор."""
    u = getattr(llm, "usage", None) or {}
    if not u.get("calls"):
        return
    try:
        await pool.execute(
            """
            insert into llm_usage (stage, model, calls, prompt_tokens, completion_tokens,
                                   reasoning_tokens, threads, seconds, cancelled)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            stage,
            getattr(llm, "model", None),
            u.get("calls", 0),
            u.get("prompt", 0),
            u.get("completion", 0),
            u.get("reasoning", 0),
            threads,
            int(seconds),
            cancelled,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("не записан расход модели (%s): %s — выполни chat-parser init-db", stage, e)


async def seconds_per_thread(pool: asyncpg.Pool) -> float | None:
    """Среднее время разбора одного обсуждения по последним разборам."""
    try:
        value = await pool.fetchval(
            """
            select sum(seconds)::float / nullif(sum(threads), 0)
              from (select seconds, threads from llm_usage
                     where stage = 'extract' and threads > 0
                     order by id desc limit 10) t
            """
        )
    except asyncpg.UndefinedTableError:
        return None
    # 0.0 — это «очень быстро», а не «нет данных»: None только когда разборов не было.
    return value


async def report(pool: asyncpg.Pool) -> dict[str, Any]:
    totals = []
    for title, interval in (*PERIODS, ("всего", None)):
        where = f"where ts > now() - interval '{interval}'" if interval else ""
        row = await pool.fetchrow(
            f"""
            -- sum(bigint) в Postgres — numeric (в Python Decimal), отсюда ::bigint
            select count(*) runs, coalesce(sum(calls), 0)::bigint calls,
                   coalesce(sum(prompt_tokens), 0)::bigint prompt,
                   coalesce(sum(completion_tokens), 0)::bigint completion,
                   coalesce(sum(reasoning_tokens), 0)::bigint reasoning
              from llm_usage {where}
            """
        )
        totals.append((title, dict(row)))

    by_stage = {
        r["stage"]: dict(r)
        for r in await pool.fetch(
            """
            select stage, count(*) runs,
                   coalesce(sum(prompt_tokens + completion_tokens), 0)::bigint tokens,
                   coalesce(sum(threads), 0)::bigint threads,
                   coalesce(sum(seconds), 0)::bigint seconds
              from llm_usage where ts > now() - interval '30 days'
             group by stage
            """
        )
    }
    active_days = await pool.fetchval(
        """
        select count(distinct date_trunc('day', ts)) from llm_usage
         where ts > now() - interval '30 days'
        """
    )
    models = [
        r["model"]
        for r in await pool.fetch(
            "select distinct model from llm_usage where ts > now() - interval '30 days' "
            "and model is not null order by 1"
        )
    ]
    return {"totals": totals, "by_stage": by_stage, "active_days": active_days or 0,
            "models": models}
