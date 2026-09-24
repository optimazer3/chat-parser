"""Участники чатов: имена из Telegram, компания и роль от пользователя,
подсказки нейросети по словам самих людей."""

from __future__ import annotations

import re
from typing import Any

import asyncpg

LABEL_RE = re.compile(r"^u:[0-9a-f]{8}$")


def who_command(label: str) -> str:
    """u:ab12cd34 -> /who_ab12cd34 — нажимаемая команда карточки участника."""
    return "/who_" + label.removeprefix("u:")


async def upsert_authors(conn: asyncpg.Connection, items: list[tuple]) -> None:
    """items: (author_hash, author_label, name, username). Компанию, роль и
    заметку не трогает — их вносит человек."""
    if not items:
        return
    await conn.executemany(
        """
        insert into authors (author_hash, author_label, name, username)
        values ($1, $2, $3, $4)
        on conflict (author_hash) do update
           set name = coalesce(excluded.name, authors.name),
               username = coalesce(excluded.username, authors.username),
               updated_at = now()
        """,
        items,
    )


def split_company_role(text: str) -> tuple[str | None, str | None]:
    """«Оптика Люкс, владелец» -> ("Оптика Люкс", "владелец"). «-» — стереть."""
    text = text.strip()
    if text in ("-", "—", "стереть"):
        return None, None
    if "," in text:
        company, role = text.split(",", 1)
        return company.strip() or None, role.strip() or None
    return text or None, None


async def set_company_role(pool: asyncpg.Pool, label: str, company: str | None,
                           role: str | None) -> bool:
    res = await pool.execute(
        "update authors set company = $2, role = $3, company_hint = null, role_hint = null, "
        "hint_quote = null, updated_at = now() where author_label = $1",
        label, company, role,
    )
    return not res.endswith(" 0")


async def set_note(pool: asyncpg.Pool, label: str, note: str | None) -> bool:
    res = await pool.execute(
        "update authors set note = $2, updated_at = now() where author_label = $1", label, note
    )
    return not res.endswith(" 0")


async def accept_hint(pool: asyncpg.Pool, label: str) -> bool:
    res = await pool.execute(
        """
        update authors set company = coalesce(company_hint, company),
                           role = coalesce(role_hint, role),
                           company_hint = null, role_hint = null, hint_quote = null,
                           updated_at = now()
         where author_label = $1 and (company_hint is not null or role_hint is not null)
        """,
        label,
    )
    return not res.endswith(" 0")


async def save_hint(pool: asyncpg.Pool, chat_id: int, label: str, company: str | None,
                    role: str | None, quote: str, message_id: int | None) -> None:
    """Подсказку сохраняем, только пока пользователь сам ничего не внёс."""
    await pool.execute(
        """
        update authors
           set company_hint = coalesce($3, company_hint), role_hint = coalesce($4, role_hint),
               hint_quote = $5, hint_chat_id = $2, hint_message_id = $6, updated_at = now()
         where author_label = $1 and company is null and role is null
        """,
        label, chat_id, company, role, quote, message_id,
    )


async def card(pool: asyncpg.Pool, label: str) -> dict[str, Any] | None:
    row = await pool.fetchrow("select * from authors where author_label = $1", label)
    if row is None:
        return None
    out = dict(row)
    out["chats"] = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.title, count(*) n from messages m join chats c on c.id = m.chat_id
         where m.author_label = $1 group by c.id, c.title order by n desc
        """,
        label,
    )]
    out["signals"] = await pool.fetchval(
        "select count(*) from signals where author_label = $1", label
    )
    out["pains"] = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.label, count(*) n from signals s join clusters c on c.id = s.cluster_id
         where s.author_label = $1 group by c.id, c.label order by n desc limit 5
        """,
        label,
    )]
    return out


async def listing(pool: asyncpg.Pool, chat_id: int | None = None,
                  limit: int = 30) -> list[dict[str, Any]]:
    """Самые активные участники (в чате или везде)."""
    return [dict(r) for r in await pool.fetch(
        """
        select a.author_label, a.name, a.username, a.company, a.role,
               (a.company_hint is not null or a.role_hint is not null) has_hint,
               count(m.*) msgs
          from authors a join messages m on m.author_label = a.author_label
         where ($1::bigint is null or m.chat_id = $1)
         group by a.author_hash
         order by msgs desc, a.author_label
         limit $2
        """,
        chat_id, limit,
    )]


def display(name: str | None, label: str, company: str | None = None,
            role: str | None = None) -> str:
    """«Иван Петров · Оптика Люкс, владелец» — подпись под цитатой."""
    who = name or "участник " + label.removeprefix("u:")
    extra = ", ".join(x for x in (company, role) if x)
    return f"{who} · {extra}" if extra else who
