"""Выгрузка истории и инкрементальная досинхронизация.

Два режима:
  backfill    — идём назад по истории от oldest_id до начала чата;
  incremental — забираем всё новее newest_id.

Оба резюмируемые: курсор коммитится после каждого батча, так что падение
посреди чата на 200k сообщений не означает «начать заново».
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import asyncpg
from telethon import TelegramClient, errors, utils
from telethon.tl.types import Message

from ..config import settings
from ..pii import author_hash, author_label, mask_text

BATCH = 100


def _media_type(msg: Message) -> str | None:
    return type(msg.media).__name__ if msg.media else None


def _reactions(msg: Message) -> int:
    results = getattr(getattr(msg, "reactions", None), "results", None)
    return sum(r.count for r in results) if results else 0


def _fwd_from(msg: Message) -> str | None:
    fwd = getattr(msg, "fwd_from", None)
    if not fwd:
        return None
    return str(getattr(fwd, "from_name", None) or getattr(fwd, "from_id", None) or "unknown")


def _row(msg: Message, chat_id: int) -> tuple | None:
    if not isinstance(msg, Message):
        return None  # служебные события (вход/выход/закрепления) не нужны
    h = author_hash(msg.sender_id, settings.author_salt)
    return (
        chat_id,
        msg.id,
        msg.date,
        h,
        author_label(h),
        bool(getattr(msg.sender, "bot", False)),
        mask_text(msg.message),
        msg.reply_to_msg_id,
        _fwd_from(msg),
        _media_type(msg),
        _reactions(msg),
        msg.edit_date,
    )


async def _save(conn: asyncpg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    await conn.executemany(
        """
        insert into messages (chat_id, message_id, ts, author_hash, author_label, is_bot,
                              text, reply_to, fwd_from, media_type, reactions, edited_at)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        on conflict (chat_id, message_id) do nothing
        """,
        rows,
    )


async def register_chat(client: TelegramClient, pool: asyncpg.Pool, ref: str) -> int:
    """Резолвит @username / ссылку / id и заводит чат в БД."""
    entity = await client.get_entity(ref)
    chat_id = utils.get_peer_id(entity)
    await pool.execute(
        """
        insert into chats (id, username, title, kind, members_count)
        values ($1,$2,$3,$4,$5)
        on conflict (id) do update
           set username = excluded.username,
               title = excluded.title,
               members_count = excluded.members_count
        """,
        chat_id,
        getattr(entity, "username", None),
        getattr(entity, "title", None) or getattr(entity, "first_name", None),
        type(entity).__name__.lower(),
        getattr(entity, "participants_count", None),
    )
    await pool.execute(
        "insert into cursors (chat_id) values ($1) on conflict (chat_id) do nothing", chat_id
    )
    return chat_id


async def sync_chat(
    client: TelegramClient,
    pool: asyncpg.Pool,
    chat_id: int,
    mode: str,
    limit: int | None = None,
) -> dict:
    """Один проход по чату. Возвращает статистику."""
    cur = await pool.fetchrow("select * from cursors where chat_id = $1", chat_id)
    if cur and cur["retry_after"] and cur["retry_after"] > datetime.now(timezone.utc):
        return {"chat_id": chat_id, "skipped": "flood_wait", "until": cur["retry_after"]}

    entity = await client.get_entity(chat_id)
    oldest, newest = (cur["oldest_id"], cur["newest_id"]) if cur else (None, None)

    if mode == "backfill":
        if cur and cur["backfill_done"]:
            return {"chat_id": chat_id, "skipped": "backfill_done"}
        it = client.iter_messages(entity, offset_id=oldest or 0, limit=limit)
    elif mode == "incremental":
        if newest is None:
            return {"chat_id": chat_id, "skipped": "no_cursor_run_backfill_first"}
        it = client.iter_messages(entity, min_id=newest, reverse=True, limit=limit)
    else:
        raise ValueError(f"unknown mode: {mode}")

    rows: list[tuple] = []
    saved = 0
    seen_min: int | None = None
    seen_max: int | None = None
    exhausted = True

    async def flush() -> None:
        nonlocal rows, saved, oldest, newest
        if not rows:
            return
        async with pool.acquire() as conn, conn.transaction():
            await _save(conn, rows)
            new_oldest = min(oldest, seen_min) if oldest else seen_min
            new_newest = max(newest, seen_max) if newest else seen_max
            await conn.execute(
                """
                update cursors set oldest_id = $2, newest_id = $3, last_run = now(),
                                   retry_after = null
                 where chat_id = $1
                """,
                chat_id,
                new_oldest,
                new_newest,
            )
            oldest, newest = new_oldest, new_newest
        saved += len(rows)
        rows = []

    try:
        async for msg in it:
            row = _row(msg, chat_id)
            if row is None:
                continue
            rows.append(row)
            seen_min = msg.id if seen_min is None else min(seen_min, msg.id)
            seen_max = msg.id if seen_max is None else max(seen_max, msg.id)
            if len(rows) >= BATCH:
                await flush()
                # Троттлинг: один аккаунт, без параллельных потоков.
                await asyncio.sleep(settings.ingest_pause + random.random() * 0.5)
    except errors.FloodWaitError as e:
        exhausted = False
        await flush()
        until = datetime.now(timezone.utc) + timedelta(seconds=e.seconds + 30)
        await pool.execute(
            "update cursors set retry_after = $2, note = $3 where chat_id = $1",
            chat_id,
            until,
            f"FloodWait {e.seconds}s",
        )
        return {"chat_id": chat_id, "saved": saved, "flood_wait_until": until}
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
        await flush()
        await pool.execute(
            "update chats set is_active = false where id = $1", chat_id
        )
        await pool.execute(
            "update cursors set note = $2 where chat_id = $1", chat_id, f"no access: {e!s}"
        )
        return {"chat_id": chat_id, "saved": saved, "error": "no_access"}

    await flush()
    # Полный проход backfill без limit означает, что дошли до начала чата.
    if mode == "backfill" and exhausted and limit is None:
        await pool.execute(
            "update cursors set backfill_done = true where chat_id = $1", chat_id
        )
    return {"chat_id": chat_id, "mode": mode, "saved": saved}


async def sync_all(
    client: TelegramClient, pool: asyncpg.Pool, mode: str, limit: int | None = None
) -> list[dict]:
    ids = [
        r["id"]
        for r in await pool.fetch("select id from chats where is_active order by id")
    ]
    out = []
    for chat_id in ids:
        out.append(await sync_chat(client, pool, chat_id, mode, limit))
        await asyncio.sleep(settings.ingest_pause)
    return out
