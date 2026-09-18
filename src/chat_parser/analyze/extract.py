"""LLM-извлечение сигналов из тредов.

Главная защита от галлюцинаций — проверка цитаты: evidence_quote обязана
дословно встречаться в исходном тексте треда. Не встречается — сигнал
выбрасывается. Доля отбраковки логируется: если она стабильно выше ~5%,
проблема в промпте, а не в модели.
"""

from __future__ import annotations

import asyncio

import asyncpg

from ..config import settings
from ..llm import LLM, LLMError, build_llm
from ..normalize.chunker import load_thread_text
from .prompts import SYSTEM_EXTRACT
from .schema import Extraction
from .validate import validate


async def extract_one(llm: LLM, text: str) -> Extraction:
    return await llm.structured(SYSTEM_EXTRACT, text, Extraction, max_tokens=8000)


async def run(pool: asyncpg.Pool, limit: int | None = None) -> dict:
    llm = build_llm()
    rows = await pool.fetch(
        """
        select chat_id, root_id, message_ids, started_at
          from threads
         where status = 'pending'
         order by started_at desc
         limit $1
        """,
        limit or 1_000_000,
    )
    sem = asyncio.Semaphore(settings.extract_concurrency)
    stats = {"threads": 0, "signals": 0, "dropped": 0, "empty": 0, "failed": 0}

    async def handle(row: asyncpg.Record) -> None:
        chat_id, root_id = row["chat_id"], row["root_id"]
        ids = list(row["message_ids"])
        async with sem:
            text = await load_thread_text(pool, chat_id, ids)
            if not text.strip():
                await pool.execute(
                    "update threads set status='skipped' where chat_id=$1 and root_id=$2",
                    chat_id,
                    root_id,
                )
                return
            try:
                extraction = await extract_one(llm, text)
            except (LLMError, Exception) as e:  # noqa: BLE001 — один тред не валит прогон
                stats["failed"] += 1
                await pool.execute(
                    "update threads set status='failed' where chat_id=$1 and root_id=$2",
                    chat_id,
                    root_id,
                )
                print(f"  ! {chat_id}/{root_id}: {type(e).__name__}: {e}")
                return

            signals, dropped = validate(extraction, text, set(ids))
            stats["dropped"] += dropped
            stats["threads"] += 1
            if not signals:
                stats["empty"] += 1

            async with pool.acquire() as conn, conn.transaction():
                # Перезаписываем сигналы треда целиком: тред мог дорасти.
                await conn.execute(
                    "delete from signals where chat_id=$1 and root_id=$2", chat_id, root_id
                )
                if signals:
                    await conn.executemany(
                        """
                        insert into signals (chat_id, root_id, type, audience, summary,
                            evidence_quote, message_ids, author_label, intensity,
                            confidence, entities, context, ts)
                        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        """,
                        [
                            (
                                chat_id,
                                root_id,
                                s.type,
                                s.audience,
                                s.summary,
                                s.evidence_quote,
                                s.message_ids,
                                s.author_label,
                                s.intensity,
                                s.confidence,
                                s.entities,
                                s.context,
                                row["started_at"],
                            )
                            for s in signals
                        ],
                    )
                await conn.execute(
                    "update threads set status='extracted' where chat_id=$1 and root_id=$2",
                    chat_id,
                    root_id,
                )
            stats["signals"] += len(signals)

    await asyncio.gather(*(handle(r) for r in rows))
    total = stats["signals"] + stats["dropped"]
    stats["drop_rate"] = round(stats["dropped"] / total, 3) if total else 0.0
    return stats
