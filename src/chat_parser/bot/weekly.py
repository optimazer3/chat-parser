"""Расписание и сбор еженедельного отчёта по всем подключённым каналам."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from telethon import TelegramClient, errors

from .. import clock, db, usage
from ..analyze.posts import channel_themes
from ..config import settings
from ..ingest import channel_posts, collector
from ..ingest.client import connect_client
from ..llm import build_llm
from ..report.channel_weekly import Channel, ChannelWeeklyReport
from . import jobs

log = logging.getLogger("chat_parser.weekly")

NEXT_KEY = "weekly_next_at"
SENT_KEY = "weekly_sent_at"
FULL_READ_KEY = "weekly_full_read:{chat}:{topic}"  # полгода темы уже прочитаны
HALF_YEAR = timedelta(days=183)


async def read_since(chat_id: int, topic_id: int, now: datetime) -> datetime:
    """С какого момента читать тему: в первый раз — полгода, потом — последний
    месяц. Реакции на посты старше месяца почти не меняются."""
    if await db.get_setting(FULL_READ_KEY.format(chat=chat_id, topic=topic_id)):
        return now - timedelta(days=settings.weekly_refresh_days)
    return now - HALF_YEAR



# ------------------------------------------------------------ расписание


def slot_after(at: datetime) -> datetime:
    """Ближайшие WEEKLY_WEEKDAY, WEEKLY_HOUR:00 по местному времени строго после at."""
    local = at.astimezone(clock.tz())
    target = local.replace(hour=settings.weekly_hour % 24, minute=0, second=0, microsecond=0)
    target += timedelta(days=(settings.weekly_weekday - local.weekday()) % 7)
    if target <= local:
        target += timedelta(days=7)
    return target


async def next_at() -> datetime:
    raw = await db.get_setting(NEXT_KEY)
    if raw:
        return datetime.fromisoformat(raw)
    # Первый запуск: ближайший слот, без отчёта «задним числом».
    nxt = slot_after(clock.now())
    await db.set_setting(NEXT_KEY, nxt.isoformat())
    return nxt


async def due(at: datetime | None = None) -> bool:
    """Пора. Бот был выключен в понедельник утром — отчёт уйдёт после запуска."""
    return (at or clock.now()) >= await next_at()


async def advance(pending: datetime) -> None:
    """Отчёт за pending отправлен (или остановлен) — следующий через неделю."""
    if await next_at() == pending:
        after = max(clock.now(), pending.astimezone(clock.tz()))
        await db.set_setting(NEXT_KEY, slot_after(after).isoformat())


# ------------------------------------------------------------ какая группа


def _norm(text: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold().replace("ё", "е")).strip()


async def find_chat(pool: asyncpg.Pool) -> dict[str, Any] | None:
    """Группа из WEEKLY_CHAT: по названию, @username или ссылке."""
    spec = settings.weekly_chat.strip()
    if not spec:
        return None
    rows = [dict(r) for r in await pool.fetch(
        "select id, title, username, link from chats order by id")]
    handle = spec.rsplit("/", 1)[-1].lstrip("@").casefold()
    for r in rows:  # точно: ник или ссылка
        if (r["username"] and r["username"].casefold() == handle) or (r["link"] or "") == spec:
            return r
    wanted = _norm(spec)
    exact = [r for r in rows if _norm(r["title"]) == wanted]
    if exact:
        return exact[0]
    partial = [r for r in rows if wanted and wanted in _norm(r["title"])]
    return partial[0] if partial else None


async def mark_chats(pool: asyncpg.Pool) -> dict[str, Any] | None:
    """Группа еженедельного отчёта не входит в ежедневный, остальные — входят."""
    chat = await find_chat(pool)
    await pool.execute("update chats set in_daily = (id is distinct from $1::bigint)",
                       chat["id"] if chat else None)
    return chat



async def _collect_channel(
    pool: asyncpg.Pool, client: TelegramClient | None, row: asyncpg.Record,
    since: datetime, until: datetime,
) -> Channel:
    title = row["title"] or row["username"] or f"Канал {row['id']}"
    channel = Channel(row["id"], title, row["username"])
    if not row["is_active"]:
        channel.status = "Сбор отключён."
    elif client is None:
        channel.status = "Счётчики Telegram недоступны."
    else:
        try:
            entity = await collector.entity_for(client, channel.id, channel.username, row["link"])
            if not getattr(entity, "broadcast", False):
                channel.status = "Группа. Метрики публикаций доступны для каналов."
                return channel
            channel.title = getattr(entity, "title", None) or channel.title
            channel.username = getattr(entity, "username", None)
            channel.posts = await channel_posts.read(client, entity, since, until)
            return channel
        except errors.FloodWaitError:
            log.warning("Telegram отложил сбор публикаций канала %s", channel.id)
            channel.status = "Сбор отложен Telegram."
        except Exception:
            log.exception("не удалось прочитать публикации канала %s", channel.id)
            channel.status = "Не удалось загрузить публикации из Telegram."

    if row["kind"] in {"public_channel", "private_channel"}:
        imported = await pool.fetch(
            """
            select message_id, ts, text, reactions from messages
             where chat_id = $1 and ts >= $2 and ts < $3 and not is_bot
             order by message_id
            """,
            channel.id, since, until,
        )
        if imported:
            channel.status = "Данные из импорта. Счётчик комментариев недоступен."
            channel.posts = [
                channel_posts.Post(post["message_id"], post["ts"], post["text"] or "",
                                   post["reactions"], None) for post in imported
            ]
    return channel


async def build(job_id: str | None = None, *, until: datetime | None = None
                ) -> ChannelWeeklyReport:
    """Собрать отклик на публикации за семь суток."""
    async with jobs.exclusive("еженедельный отчёт", job_id) as job:
        until = until or clock.now()
        since = until - timedelta(days=7)
        pool = await db.get_pool()
        rows = await pool.fetch(
            "select id, title, username, link, kind, is_active from chats "
            "order by lower(coalesce(title, username, '')), id"
        )
        client = None
        if settings.telegram_ready and any(row["is_active"] for row in rows):
            try:
                client = await connect_client()
            except Exception:
                log.exception("не удалось подключить аккаунт-сборщик")

        channels = []
        try:
            for index, row in enumerate(rows, 1):
                job["progress"] = f"читаю каналы: {index} из {len(rows)}"
                channels.append(await _collect_channel(pool, client, row, since, until))
        finally:
            if client is not None:
                await client.disconnect()

        if settings.llm_api_key and settings.llm_model:
            eligible = [
                channel for channel in channels
                if any(p.engagement for p in channel.posts) and any(p.text for p in channel.posts)
            ]
            if eligible:
                llm = build_llm()
                started, finished = time.monotonic(), False
                try:
                    for index, channel in enumerate(eligible, 1):
                        job["progress"] = f"определяю темы: {index} из {len(eligible)}"
                        channel.labels = await channel_themes(
                            llm, {post.id: post.text for post in channel.posts},
                        )
                    finished = True
                finally:
                    await usage.record(pool, "post_themes", llm, seconds=time.monotonic() - started,
                                       cancelled=not finished, source="weekly")

        return ChannelWeeklyReport(since, until, channels)
