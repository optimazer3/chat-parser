"""Кластеризация сигналов и скоринг.

При 5-10 чатах сигналов будут тысячи, а не миллионы: весь список summary
влезает в контекст Opus 5 целиком. Поэтому группируем LLM'ом — это проще
и точнее, чем подбирать пороги косинусной близости. Эмбеддинги + HDBSCAN
понадобятся, когда сигналов станет больше ~5000.

Кластеризуем ОТДЕЛЬНО по каждой аудитории: боли владельца оптики и покупателя
очков не пересекаются, смешивать их в одном кластере бессмысленно.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import anthropic
import asyncpg

from ..config import settings
from ..analyze.prompts import SYSTEM_CARD, SYSTEM_CLUSTER
from ..analyze.schema import Card, Clustering

MIN_CONFIDENCE = 0.5
HALF_LIFE_DAYS = 90.0


def score(n_authors: int, n_chats: int, mean_intensity: float, wtp_share: float,
          last_seen: datetime) -> float:
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


async def _cluster_audience(
    client: anthropic.AsyncAnthropic, pool: asyncpg.Pool, audience: str
) -> int:
    rows = await pool.fetch(
        """
        select id, type, summary, intensity
          from signals
         where audience = $1 and confidence >= $2
         order by id
        """,
        audience,
        MIN_CONFIDENCE,
    )
    if len(rows) < 4:
        return 0

    listing = "\n".join(f"{r['id']}\t{r['type']}\t{r['summary']}" for r in rows)
    resp = await client.messages.parse(
        model=settings.model_synth,
        max_tokens=32000,
        system=[{"type": "text", "text": SYSTEM_CLUSTER, "cache_control": {"type": "ephemeral"}}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Аудитория: {audience}\n"
                    f"Сигналы (id, тип, формулировка):\n{listing}"
                ),
            }
        ],
        output_format=Clustering,
    )
    if resp.stop_reason == "refusal":
        print(f"  ! кластеризация {audience}: refusal")
        return 0

    valid_ids = {r["id"] for r in rows}
    created = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("update signals set cluster_id = null where audience = $1", audience)
        await conn.execute("delete from clusters where audience = $1", audience)
        for draft in resp.parsed_output.clusters:
            ids = [i for i in draft.signal_ids if i in valid_ids]
            if len(ids) < 2:
                continue
            agg = await conn.fetchrow(
                """
                select count(*)                                 as n_signals,
                       count(distinct author_label)             as n_authors,
                       count(distinct chat_id)                  as n_chats,
                       avg(intensity)::float                    as mean_intensity,
                       avg((type = 'willingness_to_pay')::int)::float as wtp_share,
                       min(ts) as first_seen, max(ts) as last_seen
                  from signals where id = any($1::bigint[])
                """,
                ids,
            )
            cid = await conn.fetchval(
                """
                insert into clusters (audience, label, statement, score, n_signals,
                                      n_authors, n_chats, first_seen, last_seen)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9) returning id
                """,
                audience,
                draft.label,
                draft.statement,
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
                "update signals set cluster_id = $1 where id = any($2::bigint[])", cid, ids
            )
            created += 1
    return created


async def run(pool: asyncpg.Pool) -> dict:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    audiences = [
        r["audience"]
        for r in await pool.fetch(
            "select audience, count(*) c from signals group by audience having count(*) >= 4"
        )
    ]
    out = {}
    for a in audiences:
        out[a] = await _cluster_audience(client, pool, a)
    return out


async def make_cards(pool: asyncpg.Pool, top: int = 15) -> int:
    """Развёрнутая карточка для топовых кластеров."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    clusters = await pool.fetch(
        "select id, audience, label, statement from clusters order by score desc limit $1", top
    )
    done = 0
    for c in clusters:
        signals = await pool.fetch(
            """
            select type, summary, evidence_quote, context, intensity, entities
              from signals where cluster_id = $1 order by intensity desc
            """,
            c["id"],
        )
        payload = "\n".join(
            f"- [{s['type']}, острота {s['intensity']}] {s['summary']}\n"
            f"  цитата: «{s['evidence_quote']}»\n  контекст: {s['context']}"
            for s in signals
        )
        resp = await client.messages.parse(
            model=settings.model_synth,
            max_tokens=16000,
            system=[{"type": "text", "text": SYSTEM_CARD, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Аудитория: {c['audience']}\nКластер: {c['label']}\n"
                        f"Формулировка: {c['statement']}\n\nСигналы:\n{payload}"
                    ),
                }
            ],
            output_format=Card,
        )
        if resp.stop_reason == "refusal":
            continue
        await pool.execute(
            "update clusters set card = $2, updated_at = now() where id = $1",
            c["id"],
            json.dumps(resp.parsed_output.model_dump(), ensure_ascii=False),
        )
        done += 1
    return done
