"""Посты темы: модель отличает пост от реплики и называет его тему.

Каждое сообщение разбирается один раз (пока не поменяли текст). Короткие —
точно не посты: их помечаем без модели.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import asyncpg

from .. import usage
from ..config import settings
from ..llm import build_llm
from .prompts import SYSTEM_POSTS
from .schema import PostBatch

log = logging.getLogger("chat_parser.posts")

BATCH = 12
MAX_CHARS = 1500  # длинный пост целиком не нужен, тема видна по началу


async def classify(pool: asyncpg.Pool, chat_id: int, topic_id: int, since: datetime,
                   usage_source: str = "weekly", on_progress=None) -> dict[str, Any]:
    await pool.execute(
        """
        update topic_messages set is_post = false, analyzed_at = now()
         where chat_id = $1 and topic_id = $2 and is_post is null
           and length(coalesce(text, '')) < $3
        """,
        chat_id, topic_id, settings.weekly_post_min_chars,
    )
    rows = list(await pool.fetch(
        """
        select message_id, text, reply_to from topic_messages
         where chat_id = $1 and topic_id = $2 and is_post is null and ts >= $3
         order by ts
        """,
        chat_id, topic_id, since,
    ))
    themes = [r["theme"] for r in await pool.fetch(
        """
        select theme, count(*) n from topic_messages
         where chat_id = $1 and topic_id = $2 and is_post and theme <> ''
         group by theme order by n desc, theme limit 80
        """,
        chat_id, topic_id,
    )]
    stats = {"checked": 0, "posts": 0, "failed": 0}
    if not rows:
        return stats

    llm = build_llm()
    started, finished = time.monotonic(), False
    try:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            listing = "\n\n".join(
                f"[id {r['message_id']}{' · это ответ на другое сообщение' if r['reply_to'] else ''}]"
                f"\n{(r['text'] or '')[:MAX_CHARS]}"
                for r in chunk
            )
            known = "\n".join(f"- {t}" for t in themes) or "(пока нет)"
            try:
                res = await llm.structured(
                    SYSTEM_POSTS, f"Уже встречавшиеся темы:\n{known}\n\nСообщения:\n{listing}",
                    PostBatch, max_tokens=8000,
                )
            except Exception as e:  # noqa: BLE001 — остальные пачки разбираем; эту — в следующий раз
                log.warning("пачка постов не разобрана: %s", e)
                stats["failed"] += len(chunk)
                continue
            verdicts = {v.id: v for v in res.items}
            for r in chunk:
                v = verdicts.get(r["message_id"])
                if v is None:
                    continue  # модель пропустила — разберём в следующий раз
                theme = v.theme.strip() if v.is_post else ""
                await pool.execute(
                    """
                    update topic_messages set is_post = $3, theme = $4, summary = $5,
                                              analyzed_at = now()
                     where chat_id = $1 and message_id = $2
                    """,
                    chat_id, r["message_id"], v.is_post and bool(theme), theme or None,
                    v.summary.strip() if v.is_post else None,
                )
                stats["checked"] += 1
                if v.is_post and theme:
                    stats["posts"] += 1
                    if theme not in themes:
                        themes.append(theme)
            if on_progress is not None:
                await on_progress(min(i + BATCH, len(rows)), len(rows))
        finished = True
    finally:
        await usage.record(pool, "posts", llm, threads=stats["checked"],
                           seconds=time.monotonic() - started, cancelled=not finished,
                           source=usage_source)
    return stats
