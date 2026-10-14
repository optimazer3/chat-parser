"""Темы групп-форумов: найти тему по названию и забрать её сообщения с откликом.

Реакции, просмотры и пересылки обновляются при каждом сборе: у поста недельной
давности их заметно больше, чем в день публикации.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from typing import Any

import asyncpg
from telethon import TelegramClient, functions
from telethon.tl.types import ForumTopic, Message

from ..config import settings
from ..people import upsert_authors
from ..pii import author_hash, author_label, mask_text
from .collector import entity_for, sender_names

BATCH = 200


class TopicNotFound(LookupError):
    def __init__(self, wanted: str, available: list[str]):
        self.wanted, self.available = wanted, available
        names = ", ".join(f"«{t}»" for t in available[:15]) or "тем нет — это не группа с темами"
        super().__init__(f"тема «{wanted}» не найдена. В группе есть: {names}")


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold().replace("ё", "е")).strip()


def match_topic(wanted: str, topics: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Тема по названию: точно, вхождением или похожим написанием («Управления
    ресурсами» найдёт «Управление ресурсами»)."""
    w = _norm(wanted)
    if not w:
        return None
    best, score = None, 0.0
    for topic_id, title in topics:
        t = _norm(title)
        if t == w:
            return topic_id, title
        s = 0.95 if (w in t or t in w) else difflib.SequenceMatcher(None, w, t).ratio()
        if s > score:
            best, score = (topic_id, title), s
    return best if score >= 0.75 else None


async def forum_topics(client: TelegramClient, entity: Any) -> list[tuple[int, str]]:
    """(id, название) тем группы. Новые версии Telethon — messages.*, старые — channels.*."""
    request = getattr(functions.messages, "GetForumTopicsRequest", None)
    if request is None:  # Telethon до 1.40
        request = functions.channels.GetForumTopicsRequest
    res = await client(request(entity, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    return [(t.id, t.title) for t in res.topics if isinstance(t, ForumTopic)]


def reaction_counts(msg: Any) -> dict[str, int]:
    """{"👍": 12, "🔥": 3}; свои эмодзи и платные звёзды — отдельными ключами."""
    out: dict[str, int] = {}
    for r in getattr(getattr(msg, "reactions", None), "results", None) or []:
        key = getattr(r.reaction, "emoticon", None)
        if not key:
            key = "⭐" if type(r.reaction).__name__ == "ReactionPaid" else "свой эмодзи"
        out[key] = out.get(key, 0) + int(r.count)
    return out


def reply_target(msg: Any, topic_id: int) -> int | None:
    """На какое сообщение темы это ответ. Сообщение «в тему», а не в ответ, —
    None: у него reply_to указывает на саму тему."""
    head = getattr(msg, "reply_to", None)
    target = getattr(head, "reply_to_msg_id", None) if head is not None else None
    return None if target in (None, topic_id) else target


async def save(pool: asyncpg.Pool, rows: list[tuple], authors: dict[str, tuple]) -> None:
    if not rows:
        return
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            insert into topic_messages (chat_id, topic_id, message_id, ts, author_hash,
                author_label, text, reply_to, reactions, reactions_detail, views, forwards)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12)
            on conflict (chat_id, message_id) do update
               set reactions = excluded.reactions,
                   reactions_detail = excluded.reactions_detail,
                   views = excluded.views, forwards = excluded.forwards,
                   -- текст поправили — разберём заново
                   is_post = case when topic_messages.text is distinct from excluded.text
                                  then null else topic_messages.is_post end,
                   text = excluded.text,
                   updated_at = now()
            """,
            rows,
        )
        await upsert_authors(conn, list(authors.values()))


async def sync_topic(client: TelegramClient, pool: asyncpg.Pool, chat_id: int, topic_id: int,
                     since: datetime, on_progress=None) -> dict[str, int]:
    """Сообщения темы с since (от новых к старым) — новые и обновлённые реакции."""
    entity = await entity_for(client, chat_id)
    rows: list[tuple] = []
    authors: dict[str, tuple] = {}
    seen = 0
    async for msg in client.iter_messages(entity, reply_to=topic_id):
        if msg.date < since:
            break
        if not isinstance(msg, Message) or msg.id == topic_id:
            continue  # служебные: создание темы, закрепления
        h = author_hash(msg.sender_id, settings.author_salt)
        detail = reaction_counts(msg)
        rows.append((
            chat_id, topic_id, msg.id, msg.date, h, author_label(h), mask_text(msg.message or ""),
            reply_target(msg, topic_id), sum(detail.values()),
            json.dumps(detail, ensure_ascii=False) if detail else None,
            getattr(msg, "views", None), getattr(msg, "forwards", None),
        ))
        name, username = sender_names(msg)
        if h:
            authors[h] = (h, author_label(h), name, username)
        seen += 1
        if len(rows) >= BATCH:
            await save(pool, rows, authors)
            rows, authors = [], {}
            if on_progress is not None:
                await on_progress(seen)
    await save(pool, rows, authors)
    return {"messages": seen}
