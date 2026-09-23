"""Загрузка истории на живом Postgres с настоящими объектами сообщений Telethon.

Нужна тестовая база: TEST_DATABASE_URL=... pytest (таблицы пересоздаются!).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from telethon.tl.types import Message, PeerChannel, PeerUser

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL не задан")

CHAT = -1001234567890
T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def msg(i: int) -> Message:
    return Message(id=i, peer_id=PeerChannel(1234567890), date=T0 + timedelta(minutes=i),
                   message=f"сообщение номер {i}", from_id=PeerUser(1000 + i % 7))


class FakeClient:
    """Отдаёт сообщения так же, как Telegram: назад от offset_id или вперёд от min_id."""

    def __init__(self, n: int):
        self.messages = [msg(i) for i in range(1, n + 1)]

    async def get_entity(self, _):
        return object()

    def iter_messages(self, entity, offset_id=0, limit=None, min_id=None, reverse=False):
        async def gen():
            if reverse:
                items = [m for m in self.messages if m.id > (min_id or 0)]
            else:
                items = [m for m in reversed(self.messages) if not offset_id or m.id < offset_id]
            for m in items[:limit] if limit else items:
                yield m
        return gen()


@pytest.fixture
async def pool(monkeypatch):
    from chat_parser import db
    from chat_parser.config import settings
    from chat_parser.ingest import collector

    monkeypatch.setattr(settings, "database_url", TEST_DB)
    monkeypatch.setattr(settings, "ingest_pause", 0)
    monkeypatch.setattr(collector.random, "random", lambda: 0)
    monkeypatch.setattr(db, "_pool", None)
    p = await db.get_pool()
    await p.execute("drop table if exists runs, clusters, signals, threads, cursors, "
                    "messages, chats, llm_usage, chat_requests cascade")
    await db.apply_schema()
    await p.execute("insert into chats (id, title) values ($1, 'Оптики')", CHAT)
    yield p
    await db.close_pool()


async def test_new_chat_gets_history_then_only_new(pool):
    from chat_parser.ingest import collector

    client = FakeClient(250)
    seen = []

    async def progress(n):
        seen.append(n)

    res = await collector.sync_chat_full(client, pool, CHAT, on_progress=progress)
    assert res["saved"] == 250
    assert seen and seen[-1] == 250
    assert await pool.fetchval("select backfill_done from cursors where chat_id=$1", CHAT)

    client.messages.append(msg(251))
    res = await collector.sync_chat_full(client, pool, CHAT)
    assert res["saved"] == 1  # только новое
    assert await pool.fetchval("select count(*) from messages") == 251


async def test_stopped_history_resumes_without_duplicates(pool):
    from chat_parser.ingest import collector

    client = FakeClient(250)

    async def stop_after_first_batch(n):
        if n >= 100:
            raise asyncio.CancelledError  # нажали ⏹

    with pytest.raises(asyncio.CancelledError):
        await collector.sync_chat_full(client, pool, CHAT, on_progress=stop_after_first_batch)
    assert await pool.fetchval("select count(*) from messages") == 100
    assert not await pool.fetchval("select backfill_done from cursors where chat_id=$1", CHAT)

    res = await collector.sync_chat_full(client, pool, CHAT)
    assert res["saved"] == 150
    assert await pool.fetchval("select count(*) from messages") == 250
    assert await pool.fetchval("select backfill_done from cursors where chat_id=$1", CHAT)
