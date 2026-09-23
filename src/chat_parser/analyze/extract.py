"""LLM-извлечение сигналов из тредов.

Главная защита от галлюцинаций — проверка цитаты: evidence_quote обязана
дословно встречаться в исходном тексте треда. Не встречается — сигнал
выбрасывается. Доля отбраковки логируется: если она стабильно выше ~5%,
проблема в промпте, а не в модели.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

from ..config import settings
from ..llm import LLM, LLMError, TokenBudgetExhausted, build_llm
from ..normalize.chunker import load_thread_text
from .prompts import SYSTEM_EXTRACT
from .schema import Extraction
from .validate import validate


async def extract_one(llm: LLM, text: str) -> Extraction:
    try:
        return await llm.structured(
            SYSTEM_EXTRACT, text, Extraction, max_tokens=settings.extract_max_tokens
        )
    except TokenBudgetExhausted:
        # Редкий тяжёлый тред: модель рассуждает дольше обычного. Поднимать лимит
        # для всех тредов дорого, поэтому повторяем только этот.
        return await llm.structured(
            SYSTEM_EXTRACT, text, Extraction, max_tokens=settings.extract_max_tokens_retry
        )


async def reset_for_redo(pool: asyncpg.Pool) -> int:
    """Вернуть уже обработанные треды в очередь — после правки промпта.

    Сигналы треда перезаписываются целиком при повторной обработке,
    так что дублей не будет.
    """
    result = await pool.execute(
        "update threads set status = 'pending' where status in ('extracted', 'failed')"
    )
    return int(result.split()[-1])


async def reset_failed(pool: asyncpg.Pool) -> int:
    result = await pool.execute("update threads set status = 'pending' where status = 'failed'")
    return int(result.split()[-1])


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def summary_lines(stats: dict) -> list[str]:
    """Человекочитаемая сводка прогона с оценкой на остаток очереди."""
    mins, secs = divmod(int(stats.get("seconds", 0)), 60)
    tok = stats.get("tokens") or {}
    processed = stats["threads"] + stats["failed"]
    spent = tok.get("prompt", 0) + tok.get("completion", 0)
    lines = [
        f"Готово за {mins} мин {secs} с",
        f"  тредов обработано: {stats['threads']} "
        f"(без сигналов: {stats['empty']}, ошибок: {stats['failed']})",
        f"  сигналов: {stats['signals']}, отбраковано цитат: {stats['dropped']} "
        f"({stats['drop_rate']:.1%})",
    ]
    if spent:
        reasoning = tok.get("reasoning", 0)
        lines.append(
            f"  токенов: запрос {_fmt(tok.get('prompt', 0))}, "
            f"ответ {_fmt(tok.get('completion', 0))}"
            + (f" (из них рассуждения {_fmt(reasoning)})" if reasoning else "")
        )
    left = stats.get("pending_left", 0)
    if spent and processed and left:
        per_thread = spent // processed
        lines.append(
            f"  в среднем ~{_fmt(per_thread)} токенов на тред; в очереди ещё {_fmt(left)} "
            f"тредов -> примерно {_fmt(per_thread * left)} токенов на всё"
        )
    elif not left:
        lines.append("  очередь пуста")
    if stats["failed"]:
        lines.append("  упавшие треды можно повторить: chat-parser extract --retry-failed")
    return lines


def _log(line: str) -> None:
    print(line, flush=True)


async def run(pool: asyncpg.Pool, limit: int | None = None, verbose: bool = False) -> dict:
    llm = build_llm()
    started = time.monotonic()
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
    total = len(rows)
    done = 0
    if verbose:
        _log(f"Тредов к обработке: {total} (параллельно {settings.extract_concurrency})")

    def progress(root_id: int, note: str) -> None:
        nonlocal done
        done += 1
        if verbose:
            _log(f"  [{done}/{total}] тред {root_id}: {note}")

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
                progress(root_id, "пустой тред, пропущен")
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
                progress(root_id, f"ОШИБКА {type(e).__name__}: {e}")
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
            note = f"{len(signals)} сигн." if signals else "сигналов нет"
            if dropped:
                note += f", отбраковано {dropped}"
            progress(root_id, note)

    await asyncio.gather(*(handle(r) for r in rows))
    judged = stats["signals"] + stats["dropped"]
    stats["drop_rate"] = round(stats["dropped"] / judged, 3) if judged else 0.0
    stats["seconds"] = round(time.monotonic() - started)
    stats["tokens"] = dict(llm.usage)
    stats["pending_left"] = await pool.fetchval(
        "select count(*) from threads where status = 'pending'"
    )
    return stats
