"""Расписание и сбор еженедельного отчёта по всем подключённым каналам."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .. import clock, db
from ..analyze import extract
from ..config import settings
from ..ingest import collector
from ..ingest.client import connect_client
from ..normalize import threads
from ..report import channel_weekly
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



async def _sync_channels(
    pool: asyncpg.Pool, channels: list[asyncpg.Record], since: datetime, job: dict[str, Any],
) -> dict[int, str]:
    if not channels or not settings.telegram_ready:
        return {}

    try:
        client = await connect_client()
    except Exception:
        log.exception("не удалось подключить аккаунт-сборщик")
        return {channel["id"]: "сбор недоступен" for channel in channels}

    issues = {}
    try:
        for index, channel in enumerate(channels, 1):
            chat_id = channel["id"]
            job["progress"] = f"загружаю каналы: {index} из {len(channels)}"
            try:
                if channel["newest_id"] is None:
                    result = await collector.sync_chat(client, pool, chat_id, "recent", since=since)
                else:
                    result = await collector.sync_chat(client, pool, chat_id, "incremental")
                if result.get("error") == "no_access":
                    issues[chat_id] = "нет доступа к каналу"
                elif result.get("flood_wait_until") or result.get("skipped") == "flood_wait":
                    issues[chat_id] = "сбор отложен Telegram"
            except Exception:
                log.exception("не удалось обновить канал %s", chat_id)
                issues[chat_id] = "ошибка загрузки"
    finally:
        await client.disconnect()
    return issues


async def build(job_id: str | None = None, *, until: datetime | None = None
                ) -> channel_weekly.ChannelWeeklyReport:
    """Собрать PDF-данные за семь суток. Планировщик передаёт конец своего выпуска."""
    async with jobs.exclusive("еженедельный отчёт", job_id) as job:
        until = until or clock.now()
        since = until - timedelta(days=7)
        pool = await db.get_pool()
        chats = await pool.fetch(
            """
            select c.id, c.is_active, cu.newest_id from chats c
              left join cursors cu on cu.chat_id = c.id order by c.id
            """
        )
        issues = await _sync_channels(pool, [chat for chat in chats if chat["is_active"]],
                                      since, job)
        job["progress"] = "собираю обсуждения"
        for chat in chats:
            await threads.build_for_chat(pool, chat["id"], since, until=until)

        analysis_note = ""
        if settings.llm_api_key and settings.llm_model and chats:
            job["progress"] = "извлекаю сигналы"

            async def progress(done: int, total: int, _stats: dict) -> None:
                job["progress"] = f"разбираю обсуждения: {done} из {total}"

            stats = await extract.run(pool, on_progress=progress, ended_after=since,
                                      ended_before=until, usage_source="weekly")
            if stats["failed"]:
                analysis_note = f"Не удалось разобрать обсуждений: {stats['failed']}."
        elif chats and not (settings.llm_api_key and settings.llm_model):
            analysis_note = "Автоматический разбор сообщений не настроен."

        job["progress"] = "готовлю PDF"
        report = await channel_weekly.collect(pool, since, until)
        report.analysis_note = analysis_note
        for channel in report.channels:
            channel["status"] = issues.get(channel["id"]) or (
                "" if channel["is_active"] else "сбор отключён"
            )
        return report
