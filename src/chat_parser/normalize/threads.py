"""Восстановление диалогов из плоского потока сообщений.

В групповом чате осмысленная реплика разорвана на 5 сообщений и перемешана
с двумя параллельными разговорами. Без склейки в треды LLM получает кашу.

Правила:
  1. Есть reply_to на известное сообщение -> жёсткое ребро.
  2. Иначе сообщение цепляется к активному треду (последнее сообщение не
     старше thread_gap_minutes). Если активных несколько — к тому, где автор
     уже участвовал, иначе к самому свежему.
  3. Тред длиннее thread_max_messages закрывается, дальше начинается новый.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import asyncpg

from ..config import settings


@dataclass
class Thread:
    root_id: int
    ids: list[int] = field(default_factory=list)
    authors: set[str] = field(default_factory=set)
    started_at: datetime | None = None
    last_ts: datetime | None = None
    closed: bool = False


def assign_threads(rows: list[dict]) -> list[Thread]:
    """rows: message_id, ts, author_label, text, reply_to — отсортированы по message_id."""
    gap = timedelta(minutes=settings.thread_gap_minutes)
    thread_of: dict[int, int] = {}
    threads: dict[int, Thread] = {}

    for m in rows:
        mid, ts, author = m["message_id"], m["ts"], m["author_label"]
        root: int | None = None

        if m["reply_to"] and m["reply_to"] in thread_of:
            root = thread_of[m["reply_to"]]
            if threads[root].closed:
                root = None
        else:
            active = [
                t
                for t in threads.values()
                if not t.closed and t.last_ts is not None and ts - t.last_ts <= gap
            ]
            if active:
                mine = [t for t in active if author and author in t.authors]
                root = max(mine or active, key=lambda t: t.last_ts).root_id

        if root is None:
            root = mid
            threads[root] = Thread(root_id=root, started_at=ts)

        t = threads[root]
        t.ids.append(mid)
        t.last_ts = ts
        if t.started_at is None:
            t.started_at = ts
        if author:
            t.authors.add(author)
        thread_of[mid] = root

        if len(t.ids) >= settings.thread_max_messages:
            t.closed = True

    return sorted(threads.values(), key=lambda t: t.root_id)


def is_worth_analyzing(thread: Thread, texts: dict[int, str]) -> bool:
    """Отсеивает мусор: «+», «спс», стикеры. Одиночное сообщение проходит,
    только если оно содержательное — там тоже бывают боли."""
    words = sum(len((texts.get(i) or "").split()) for i in thread.ids)
    if len(thread.ids) == 1:
        return words >= settings.min_words_standalone
    return words >= 12


def thread_hash(ids: list[int], texts: dict[int, str]) -> str:
    payload = "|".join(f"{i}:{texts.get(i, '')}" for i in ids)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def build_for_chat(
    pool: asyncpg.Pool, chat_id: int, since: datetime | None = None
) -> dict:
    """since — собрать только обсуждения из сообщений не старше этого момента.

    Нужно вечернему разбору: пересобирать всю историю чата каждый вечер
    незачем. Обсуждение, начавшееся до окна, получит в окне новый корень, но
    уже разобранная его часть не переразбирается — хэш старой записи тот же.
    """
    rows = [
        dict(r)
        for r in await pool.fetch(
            """
            select message_id, ts, author_label, text, reply_to
              from messages
             where chat_id = $1 and not is_bot and text <> ''
               and ($2::timestamptz is null or ts >= $2)
             order by message_id
            """,
            chat_id,
            since,
        )
    ]
    if not rows:
        return {"chat_id": chat_id, "threads": 0}

    texts = {r["message_id"]: r["text"] for r in rows}
    threads = assign_threads(rows)

    payload = []
    skipped = 0
    for t in threads:
        if not is_worth_analyzing(t, texts):
            skipped += 1
            continue
        payload.append(
            (
                chat_id,
                t.root_id,
                t.started_at,
                t.last_ts,
                t.ids,
                len(t.authors),
                thread_hash(t.ids, texts),
            )
        )

    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            insert into threads (chat_id, root_id, started_at, ended_at, message_ids,
                                 participants, text_hash)
            values ($1,$2,$3,$4,$5,$6,$7)
            on conflict (chat_id, root_id) do update
               set ended_at = excluded.ended_at,
                   message_ids = excluded.message_ids,
                   participants = excluded.participants,
                   text_hash = excluded.text_hash,
                   built_at = now(),
                   -- тред дорос новыми сообщениями -> переанализировать
                   status = case when threads.text_hash <> excluded.text_hash
                                 then 'pending' else threads.status end
            """,
            payload,
        )
    return {"chat_id": chat_id, "threads": len(payload), "skipped": skipped}


async def build_all(pool: asyncpg.Pool) -> list[dict]:
    ids = [r["id"] for r in await pool.fetch("select id from chats order by id")]
    return [await build_for_chat(pool, cid) for cid in ids]
