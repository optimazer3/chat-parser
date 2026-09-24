"""Импорт истории из экспорта Telegram Desktop.

Путь без API-ключей и без риска для аккаунта: в Telegram Desktop открыть чат,
⋮ -> «Экспорт истории чата», формат JSON. На выходе result.json, который
скармливается сюда.

Формат экспорта отличается от того, что отдаёт MTProto, поэтому поля
приводятся к общему виду: id чата к тому, каким его видит Telethon
(-100… у супергрупп), автор — к тому же солёному хэшу, текст — к плоской
строке. Благодаря этому позже можно добавить те же чаты через MTProto,
и данные сойдутся, а не задвоятся.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from ..config import settings
from ..pii import author_hash, author_label, mask_text
from ..people import upsert_authors
from .collector import save_messages

CHUNK = 1000
SUPERGROUP_TYPES = ("supergroup", "channel")


def flatten_text(value: Any) -> str:
    """text бывает строкой, а бывает списком из строк и объектов-сущностей."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(value)


def normalize_chat_id(raw_id: Any, chat_type: str | None) -> int:
    """Приводит id к виду, в котором его хранит Telethon (utils.get_peer_id).

    В экспорте супергруппа может быть и положительным id, и уже с префиксом
    -100 — в зависимости от версии Telegram Desktop.
    """
    value = int(raw_id)
    if value < 0:
        return value
    kind = (chat_type or "").lower()
    if any(t in kind for t in SUPERGROUP_TYPES):
        return int(f"-100{value}")
    if "group" in kind:  # обычная группа
        return -value
    return value  # личная переписка


def parse_from_id(value: Any) -> int | None:
    """'user123456789' / 'channel1234' -> 123456789 / 1234."""
    if value is None:
        return None
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def parse_date(msg: dict[str, Any]) -> datetime | None:
    """date_unixtime надёжнее date: date — локальное время того, кто выгружал."""
    unix = msg.get("date_unixtime")
    if unix is not None:
        try:
            return datetime.fromtimestamp(int(unix), tz=timezone.utc)
        except (ValueError, OSError):
            pass
    raw = msg.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _media_type(msg: dict[str, Any]) -> str | None:
    if msg.get("media_type"):
        return str(msg["media_type"])
    for key in ("photo", "file", "sticker_emoji", "poll", "location_information"):
        if key in msg:
            return key
    return None


def _reactions(msg: dict[str, Any]) -> int:
    items = msg.get("reactions")
    if not isinstance(items, list):
        return 0
    return sum(int(r.get("count", 0)) for r in items if isinstance(r, dict))


def parse_messages(data: dict[str, Any], chat_id: int, salt: str) -> tuple[list[tuple], int]:
    """(строки для вставки, сколько пропущено служебных/пустых)."""
    rows: list[tuple] = []
    skipped = 0
    for msg in data.get("messages", []):
        if not isinstance(msg, dict) or msg.get("type") != "message":
            skipped += 1  # вступления, закрепления, смены аватарки
            continue
        ts = parse_date(msg)
        if ts is None or msg.get("id") is None:
            skipped += 1
            continue
        h = author_hash(parse_from_id(msg.get("from_id")), salt)
        rows.append(
            (
                chat_id,
                int(msg["id"]),
                ts,
                h,
                author_label(h),
                False,
                mask_text(flatten_text(msg.get("text"))),
                msg.get("reply_to_message_id"),
                msg.get("forwarded_from"),
                _media_type(msg),
                _reactions(msg),
                None,
            )
        )
    return rows, skipped


def export_authors(data: dict[str, Any], salt: str) -> list[tuple]:
    """(hash, label, имя, username) по полям from/from_id экспорта. Username в
    экспорте нет — только отображаемое имя."""
    seen: dict[str, tuple] = {}
    for msg in data.get("messages", []):
        if not isinstance(msg, dict) or msg.get("type") != "message":
            continue
        h = author_hash(parse_from_id(msg.get("from_id")), salt)
        name = msg.get("from")
        if h and isinstance(name, str) and name.strip():
            seen[h] = (h, author_label(h), name.strip(), None)
    return list(seen.values())


async def import_file(
    pool: asyncpg.Pool,
    path: Path,
    chat_id_override: int | None = None,
    title_override: str | None = None,
) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "messages" not in data:
        raise ValueError(
            "это не похоже на экспорт чата: в файле нет поля 'messages'. "
            "Нужен result.json из «Экспорт истории чата» в формате JSON"
        )

    chat_type = data.get("type")
    chat_id = chat_id_override or normalize_chat_id(data.get("id", 0), chat_type)
    title = title_override or data.get("name") or f"chat {chat_id}"

    rows, skipped = parse_messages(data, chat_id, settings.author_salt)
    if not rows:
        return {"chat_id": chat_id, "title": title, "saved": 0, "skipped": skipped}

    ids = [r[1] for r in rows]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            insert into chats (id, title, kind) values ($1,$2,$3)
            on conflict (id) do update set title = excluded.title
            """,
            chat_id,
            title,
            str(chat_type or "unknown"),
        )
        before = await conn.fetchval(
            "select count(*) from messages where chat_id = $1", chat_id
        )
        for i in range(0, len(rows), CHUNK):
            await save_messages(conn, rows[i : i + CHUNK])
        # имена авторов — для карточек участников (в модель не уходят)
        await upsert_authors(conn, export_authors(data, settings.author_salt))
        after = await conn.fetchval(
            "select count(*) from messages where chat_id = $1", chat_id
        )
        await conn.execute(
            """
            insert into cursors (chat_id, oldest_id, newest_id, backfill_done, note, last_run)
            values ($1,$2,$3,true,$4,now())
            on conflict (chat_id) do update set
                oldest_id = least(coalesce(cursors.oldest_id, excluded.oldest_id),
                                  excluded.oldest_id),
                newest_id = greatest(coalesce(cursors.newest_id, excluded.newest_id),
                                     excluded.newest_id),
                backfill_done = cursors.backfill_done or excluded.backfill_done,
                note = excluded.note,
                last_run = now()
            """,
            chat_id,
            min(ids),
            max(ids),
            f"импорт из экспорта Telegram Desktop {datetime.now(timezone.utc):%Y-%m-%d}",
        )

    return {
        "chat_id": chat_id,
        "title": title,
        "in_file": len(rows),
        "saved": after - before,
        "duplicates": len(rows) - (after - before),
        "skipped": skipped,
    }
