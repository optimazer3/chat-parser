"""Привязка новых сигналов к уже известным болям.

Полный пересчёт болей (llm_cluster.run) дорог и меняет их номера, поэтому
для ежедневной работы — дёшево и аддитивно: новые сигналы раскладываются по
существующим болям одним запросом на аудиторию, а то, что никуда не подошло,
группируется в новые боли. Так отчёт отличает «новую боль» от «опять то же»
и видит всплески.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import asyncpg

from .. import usage
from ..analyze.prompts import SYSTEM_ASSIGN
from ..analyze.schema import Assignment
from ..config import settings
from ..llm import LLMError, build_llm
from .llm_cluster import MIN_CONFIDENCE, _cluster_batch, add_clusters, refresh_stats

BATCH = 60


async def assign_new(
    pool: asyncpg.Pool, since: datetime, fresh: datetime | None = None,
    usage_source: str = "live",
) -> dict[str, Any]:
    """Разложить сигналы, созданные с since и ещё не привязанные, по болям.
    fresh — только из разговоров не старше этого момента: архив, разобранный
    вручную, раскладывается ручным пересчётом болей."""
    fresh = fresh or datetime.min.replace(tzinfo=since.tzinfo)
    llm = build_llm(settings.synth_model)
    result: dict[str, Any] = {"assigned": 0, "new_pains": [], "errors": []}
    touched: set[int] = set()
    started, finished = time.monotonic(), False
    try:
        audiences = [
            r["audience"]
            for r in await pool.fetch(
                """
                select distinct audience from signals
                 where cluster_id is null and confidence >= $1 and created_at >= $2
                   and ts >= $3
                """,
                MIN_CONFIDENCE, since, fresh,
            )
        ]
        for audience in audiences:
            try:
                await _assign_audience(pool, llm, audience, since, fresh, result, touched)
            except LLMError as e:
                result["errors"].append(f"{audience}: {e}")
        if touched:
            await refresh_stats(pool, sorted(touched))
        finished = True
    finally:
        await usage.record(pool, "assign", llm, seconds=time.monotonic() - started,
                           cancelled=not finished, source=usage_source)
    return result


async def _assign_audience(pool, llm, audience: str, since: datetime, fresh: datetime,
                           result: dict[str, Any], touched: set[int]) -> None:
    signals = list(await pool.fetch(
        """
        select id, type, summary from signals
         where audience = $1 and cluster_id is null and confidence >= $2 and created_at >= $3
           and ts >= $4
         order by id
        """,
        audience, MIN_CONFIDENCE, since, fresh,
    ))
    pains = list(await pool.fetch(
        "select id, label, statement from clusters where audience = $1 order by score desc",
        audience,
    ))

    if pains:
        pain_ids = {p["id"] for p in pains}
        pain_list = "\n".join(f"{p['id']}\t{p['label']}\t{p['statement']}" for p in pains)
        for i in range(0, len(signals), BATCH):
            chunk = signals[i : i + BATCH]
            own = {s["id"] for s in chunk}
            answer = await llm.structured(
                SYSTEM_ASSIGN,
                f"Аудитория: {audience}\n\nИзвестные боли (id, название, формулировка):\n"
                f"{pain_list}\n\nНовые сигналы (id, тип, формулировка):\n"
                + "\n".join(f"{s['id']}\t{s['type']}\t{s['summary']}" for s in chunk),
                Assignment,
                max_tokens=8000,
            )
            for item in answer.items:
                if item.signal_id in own and item.pain_id in pain_ids:
                    await pool.execute(
                        "update signals set cluster_id = $1 where id = $2 and cluster_id is null",
                        item.pain_id, item.signal_id,
                    )
                    touched.add(item.pain_id)
                    result["assigned"] += 1

    leftover = list(await pool.fetch(
        """
        select id, type, summary, intensity from signals
         where audience = $1 and cluster_id is null and confidence >= $2 and created_at >= $3
           and ts >= $4
         order by id
        """,
        audience, MIN_CONFIDENCE, since, fresh,
    ))
    if len(leftover) >= 2:
        drafts = await _cluster_batch(llm, audience, leftover)
        result["new_pains"] += await add_clusters(pool, audience, drafts)
