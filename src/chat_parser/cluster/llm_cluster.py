"""Кластеризация сигналов и скоринг.

Группируем не эмбеддингами, а моделью: на 5-10 чатах сигналов тысячи, и
формулировки у них короткие. Но контекст обычной модели через шлюз — это
десятки тысяч токенов, а не миллион, поэтому кластеризация двухступенчатая:

  1) сигналы режутся на батчи и кластеризуются независимо -> черновики;
  2) черновики (их сильно меньше) сводятся вторым проходом, который
     схлопывает дубликаты между батчами.

Кластеризуем ОТДЕЛЬНО по каждой аудитории: боли владельца оптики и
покупателя очков не пересекаются, смешивать их бессмысленно.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

from ..analyze.prompts import SYSTEM_CARD, SYSTEM_CLUSTER, SYSTEM_MERGE
from ..analyze.schema import Card, Clustering, Merging
from ..config import settings
from ..llm import LLM, LLMError, build_llm

MIN_CONFIDENCE = 0.5
MIN_CLUSTER_SIGNALS = 2
HALF_LIFE_DAYS = 90.0


@dataclass
class Draft:
    label: str
    statement: str
    signal_ids: list[int]


def score(
    n_authors: int,
    n_chats: int,
    mean_intensity: float,
    wtp_share: float,
    last_seen: datetime,
) -> float:
    """Считаем по уникальным АВТОРАМ и чатам, а не по числу сигналов.

    Иначе топ займут один болтливый участник и один самый активный чат.
    """
    age_days = (datetime.now(timezone.utc) - last_seen).days
    recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
    return round(
        2.0 * math.log1p(n_authors)
        + 1.5 * math.log1p(n_chats)
        + 1.0 * mean_intensity
        + 2.0 * recency
        + 2.0 * wtp_share,
        3,
    )


async def _cluster_batch(llm: LLM, audience: str, rows: list[asyncpg.Record]) -> list[Draft]:
    listing = "\n".join(f"{r['id']}\t{r['type']}\t{r['summary']}" for r in rows)
    valid = {r["id"] for r in rows}
    result = await llm.structured(
        SYSTEM_CLUSTER,
        f"Аудитория: {audience}\nСигналы (id, тип, формулировка):\n{listing}",
        Clustering,
        max_tokens=16000,
    )
    drafts = []
    for c in result.clusters:
        ids = sorted({i for i in c.signal_ids if i in valid})
        if len(ids) >= MIN_CLUSTER_SIGNALS:
            drafts.append(Draft(c.label, c.statement, ids))
    return drafts


async def _merge_drafts(llm: LLM, audience: str, drafts: list[Draft]) -> list[Draft]:
    """Схлопывает дубликаты, возникшие из-за независимой обработки батчей."""
    listing = "\n".join(
        f"{i}\t{d.label}\t{d.statement}\t({len(d.signal_ids)} сигн.)"
        for i, d in enumerate(drafts)
    )
    result = await llm.structured(
        SYSTEM_MERGE,
        f"Аудитория: {audience}\nЧерновые кластеры (draft_id, label, statement):\n{listing}",
        Merging,
        max_tokens=16000,
    )

    merged: list[Draft] = []
    used: set[int] = set()
    for g in result.groups:
        ids = [i for i in g.draft_ids if 0 <= i < len(drafts) and i not in used]
        if not ids:
            continue
        used.update(ids)
        signal_ids = sorted({s for i in ids for s in drafts[i].signal_ids})
        merged.append(Draft(g.label, g.statement, signal_ids))

    # Черновики, которые модель потеряла, не выбрасываем.
    merged += [d for i, d in enumerate(drafts) if i not in used]
    return merged


async def _persist(
    pool: asyncpg.Pool, audience: str, drafts: list[Draft]
) -> int:
    created = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("update signals set cluster_id = null where audience = $1", audience)
        await conn.execute("delete from clusters where audience = $1", audience)
        for d in drafts:
            if len(d.signal_ids) < MIN_CLUSTER_SIGNALS:
                continue
            agg = await conn.fetchrow(
                """
                select count(*) as n_signals,
                       count(distinct author_label) as n_authors,
                       count(distinct chat_id) as n_chats,
                       avg(intensity)::float as mean_intensity,
                       avg((type = 'willingness_to_pay')::int)::float as wtp_share,
                       min(ts) as first_seen, max(ts) as last_seen
                  from signals where id = any($1::bigint[])
                """,
                d.signal_ids,
            )
            if not agg or not agg["last_seen"]:
                continue
            cid = await conn.fetchval(
                """
                insert into clusters (audience, label, statement, score, n_signals,
                                      n_authors, n_chats, first_seen, last_seen)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9) returning id
                """,
                audience,
                d.label[:200],
                d.statement,
                score(
                    agg["n_authors"],
                    agg["n_chats"],
                    agg["mean_intensity"] or 0.0,
                    agg["wtp_share"] or 0.0,
                    agg["last_seen"],
                ),
                agg["n_signals"],
                agg["n_authors"],
                agg["n_chats"],
                agg["first_seen"],
                agg["last_seen"],
            )
            await conn.execute(
                "update signals set cluster_id = $1 where id = any($2::bigint[])",
                cid,
                d.signal_ids,
            )
            created += 1
    return created


async def _cluster_audience(llm: LLM, pool: asyncpg.Pool, audience: str) -> int:
    rows = list(
        await pool.fetch(
            """
            select id, type, summary, intensity
              from signals
             where audience = $1 and confidence >= $2
             order by id
            """,
            audience,
            MIN_CONFIDENCE,
        )
    )
    if len(rows) < 4:
        return 0

    size = max(20, settings.cluster_batch)
    batches = [rows[i : i + size] for i in range(0, len(rows), size)]
    drafts: list[Draft] = []
    for batch in batches:
        drafts += await _cluster_batch(llm, audience, batch)

    if not drafts:
        return 0
    if len(batches) > 1 and len(drafts) > 1:
        drafts = await _merge_drafts(llm, audience, drafts)

    return await _persist(pool, audience, drafts)


async def run(pool: asyncpg.Pool) -> dict:
    llm = build_llm(settings.synth_model)
    audiences = [
        r["audience"]
        for r in await pool.fetch(
            "select audience from signals group by audience having count(*) >= 4"
        )
    ]
    out: dict[str, int | str] = {}
    for a in audiences:
        try:
            out[a] = await _cluster_audience(llm, pool, a)
        except LLMError as e:
            out[a] = f"ошибка: {e}"
    return out


async def make_cards(
    pool: asyncpg.Pool,
    top: int = 15,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> int:
    """Развёрнутая карточка для топовых кластеров."""
    llm = build_llm(settings.synth_model)
    clusters = await pool.fetch(
        "select id, audience, label, statement from clusters order by score desc limit $1", top
    )
    done = 0
    for i, c in enumerate(clusters, 1):
        if on_progress is not None:
            await on_progress(i, len(clusters))
        signals = await pool.fetch(
            """
            select type, summary, evidence_quote, context, intensity
              from signals where cluster_id = $1 order by intensity desc limit 40
            """,
            c["id"],
        )
        payload = "\n".join(
            f"- [{s['type']}, острота {s['intensity']}] {s['summary']}\n"
            f"  цитата: «{s['evidence_quote']}»\n  контекст: {s['context']}"
            for s in signals
        )
        try:
            card = await llm.structured(
                SYSTEM_CARD,
                f"Аудитория: {c['audience']}\nКластер: {c['label']}\n"
                f"Формулировка: {c['statement']}\n\nСигналы:\n{payload}",
                Card,
                max_tokens=8000,
            )
        except LLMError as e:
            print(f"  ! карточка {c['id']}: {e}")
            continue
        await pool.execute(
            "update clusters set card = $2, updated_at = now() where id = $1",
            c["id"],
            json.dumps(card.model_dump(), ensure_ascii=False),
        )
        done += 1
    return done
