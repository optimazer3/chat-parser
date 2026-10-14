"""Еженедельный отчёт «Отклик на темы» по теме группы-форума.

Раз в неделю (WEEKLY_WEEKDAY, WEEKLY_HOUR по местному времени) аккаунт-сборщик
забирает сообщения темы: в первый раз за полгода, потом — за последний месяц
(WEEKLY_REFRESH_DAYS): новые посты и свежие реакции. Нейросеть разбирает новые
сообщения (пост или реплика, тема), и на
почту уходит отчёт: какие темы откликаются за неделю, месяц и полгода, сначала
у автора в фокусе. Сама группа в ежедневный отчёт не входит.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .. import clock, db, usage
from ..analyze import posts as posts_mod
from ..analyze.prompts import SYSTEM_WEEKLY
from ..analyze.schema import WeeklyDigest
from ..config import settings
from ..ingest import topics as topics_mod
from ..ingest.client import connect_client
from ..llm import build_llm
from ..report import weekly as report_mod
from ..report.weekly import WeeklyReport
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


def enabled() -> bool:
    return bool(settings.weekly_chat.strip())


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
    return enabled() and (at or clock.now()) >= await next_at()


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


async def focus_authors(pool: asyncpg.Pool, chat_id: int,
                        topic_id: int) -> tuple[set[str], list[str]]:
    """Кто из писавших в теме — автор в фокусе (WEEKLY_FOCUS)."""
    rows = await pool.fetch(
        """
        select distinct a.author_hash, a.name, a.username
          from topic_messages t join authors a on a.author_hash = t.author_hash
         where t.chat_id = $1 and t.topic_id = $2
        """,
        chat_id, topic_id,
    )
    picked = [r for r in rows
              if report_mod.focus_matches(settings.weekly_focus, r["name"], r["username"])]
    names = sorted({r["name"] or "@" + (r["username"] or "") for r in picked})
    return {r["author_hash"] for r in picked}, names


# ------------------------------------------------------------ выводы


def _listing(data: dict[str, Any]) -> str:
    """Цифры и суть постов для модели. Имён нет: «автор в фокусе» и «другие»."""
    out = []
    for p in data["periods"]:
        out.append(f"## За {p['title']}: постов {p['posts']}, реакций {p['reactions']}, "
                   f"комментариев {p['comments']}")
        for who, themes, posts in (
            ("Фокусный автор", p["focus_themes"], p["focus_posts"]),
            ("Другие участники", p["other_themes"], p["other_posts"]),
        ):
            out.append(f"{who} — темы (реакций в среднем на пост / всего / постов):")
            out += [f"- {t['theme']}: {t['avg']:.0f} / {t['reactions']} / {t['posts']}"
                    for t in themes[:12]] or ["- (нет постов)"]
            if posts:
                out.append(f"{who} — лучшие посты:")
                out += [f"- [реакций {x['reactions']}, комментариев {x['comments']}] "
                        f"{x['theme']} — {x['summary'] or ''}" for x in posts[:8]]
        out.append("")
    return "\n".join(out)


async def insights(data: dict[str, Any]) -> dict[str, Any]:
    if not any(p["posts"] for p in data["periods"]):
        return {}
    pool = await db.get_pool()
    llm = build_llm(settings.synth_model)
    started, finished = time.monotonic(), False
    try:
        res = await llm.structured(SYSTEM_WEEKLY, _listing(data), WeeklyDigest, max_tokens=8000)
        finished = True
    finally:
        await usage.record(pool, "weekly", llm, seconds=time.monotonic() - started,
                           cancelled=not finished, source="weekly")

    def items(xs: list[str], n: int) -> list[str]:
        return [x.strip() for x in xs if x.strip()][:n]

    return {"summary": res.summary.strip(), "focus": items(res.focus, 6),
            "others": items(res.others, 4), "ideas": items(res.ideas, 5)}


# ------------------------------------------------------------ отчёт


async def build(job_id: str | None = None, mark_sent: bool = True) -> WeeklyReport:
    """Собрать отчёт. mark_sent=False — «прислать сейчас»: расписание не трогает."""
    async with jobs.exclusive("еженедельный отчёт", job_id) as job:
        pool = await db.get_pool()
        pending = await next_at() if mark_sent else None
        now = datetime.now(timezone.utc)
        report = await _build(pool, job, now)
        if pending is not None:
            await advance(pending)
            await db.set_setting(SENT_KEY, now.isoformat())
        return report


async def _build(pool: asyncpg.Pool, job: dict[str, Any], now: datetime) -> WeeklyReport:
    empty = {"chat_title": settings.weekly_chat, "topic_title": settings.weekly_topic,
             "now": now, "periods": [], "focus_names": []}
    chat = await mark_chats(pool)
    if chat is None:
        return WeeklyReport(empty, {"problem": (
            f"Группа «{settings.weekly_chat}» не найдена среди подключённых чатов. Добавь её "
            "в боте кнопкой ➕ Добавить чат или поправь WEEKLY_CHAT в .env.")})
    empty["chat_title"] = chat["title"]
    if not settings.telegram_ready:
        return WeeklyReport(empty, {"problem": "Нужны ключи Telegram API: без них тему не прочитать."})

    warnings: list[str] = []
    client = await connect_client()
    try:
        job["progress"] = "ищу тему в группе"
        entity = await topics_mod.entity_for(client, chat["id"], chat["username"])
        topics = await topics_mod.forum_topics(client, entity)
        found = topics_mod.match_topic(settings.weekly_topic, topics)
        if found is None:
            err = str(topics_mod.TopicNotFound(settings.weekly_topic, [t for _, t in topics]))
            return WeeklyReport(empty, {"problem": err[:1].upper() + err[1:]
                                        + ". Поправь WEEKLY_TOPIC в .env."})
        topic_id, topic_title = found

        async def read_progress(n: int) -> None:
            job["progress"] = f"читаю тему: {n} сообщений"

        job["progress"] = "читаю тему"
        since = await read_since(chat["id"], topic_id, now)
        try:
            await topics_mod.sync_topic(client, pool, chat["id"], topic_id, since,
                                        on_progress=read_progress)
            if since <= now - HALF_YEAR:  # полгода прочитаны целиком — дальше только месяц
                await db.set_setting(FULL_READ_KEY.format(chat=chat["id"], topic=topic_id),
                                     now.isoformat())
        except Exception as e:  # noqa: BLE001 — отчёт по уже собранному лучше, чем никакого
            log.warning("тема не прочитана: %s", e)
            warnings.append(f"Тему не удалось дочитать ({e}) — реакции могут быть не "
                            "самыми свежими.")
    finally:
        await client.disconnect()

    async def classify_progress(done: int, total: int) -> None:
        job["progress"] = f"разбираю сообщения: {done} из {total}"

    job["progress"] = "разбираю сообщения"
    res = await posts_mod.classify(pool, chat["id"], topic_id, now - HALF_YEAR,
                                   on_progress=classify_progress)
    if res["failed"]:
        warnings.append(f"Нейросеть не разобрала {res['failed']} сообщений — попробую в "
                        "следующий раз.")

    focus, names = await focus_authors(pool, chat["id"], topic_id)
    if not focus:
        warnings.append(f"Автор «{settings.weekly_focus}» не найден среди писавших в теме "
                        "за полгода. Проверь WEEKLY_FOCUS в .env.")
    job["progress"] = "считаю отклик"
    data = await report_mod.collect(pool, chat, topic_id, topic_title, focus, names, now)
    job["progress"] = "пишу выводы"
    try:
        data.update(await insights(data))
    except Exception as e:  # noqa: BLE001 — цифры важнее выводов
        log.warning("выводы недели не получились: %s", e)
        warnings.append("Выводы нейросети не получились — в отчёте только цифры.")
    return WeeklyReport(data, {"warnings": warnings})
