"""Рендер треда в текст для модели.

@упоминания заменяются на плейсхолдер: в БД они нужны для склейки диалогов,
но в LLM отправлять чужие ники незачем.
"""

from __future__ import annotations

import re

import asyncpg

MENTION_RE = re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{3,31}")


def render(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        text = MENTION_RE.sub("<@nick>", r["text"] or "").replace("\n", " ").strip()
        ts = r["ts"].strftime("%Y-%m-%d %H:%M")
        reply = f" ->m:{r['reply_to']}" if r["reply_to"] else ""
        lines.append(f"[m:{r['message_id']} | {r['author_label']} | {ts}{reply}] {text}")
    return "\n".join(lines)


async def load_thread_text(pool: asyncpg.Pool, chat_id: int, message_ids: list[int]) -> str:
    rows = await pool.fetch(
        """
        select message_id, ts, author_label, text, reply_to
          from messages
         where chat_id = $1 and message_id = any($2::bigint[])
         order by message_id
        """,
        chat_id,
        message_ids,
    )
    return render([dict(r) for r in rows])
