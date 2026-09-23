"""Ссылки на чаты и сообщения в Telegram."""

from __future__ import annotations

import re

REF_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?P<path>[^?#\s]+)$"
    r"|^@(?P<user>[A-Za-z][A-Za-z0-9_]{3,31})$"
)
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class BadChatRef(ValueError):
    """Не ссылка на чат — с объяснением, что прислать."""


def normalize_chat_ref(text: str) -> str:
    """Единый вид ссылки: @Optika, t.me/optika и https://t.me/optika/ — один чат.

    Регистр у username не важен, у хэша приглашения — важен.
    """
    m = REF_RE.match(text.strip())
    if not m:
        raise BadChatRef("это не похоже на ссылку на чат: жду t.me/имя, @имя или t.me/+приглашение")
    if m.group("user"):
        return f"https://t.me/{m.group('user').lower()}"
    parts = m.group("path").strip("/").split("/")
    head = parts[0]
    if head == "c":
        raise BadChatRef(
            "это ссылка на сообщение в приватном чате — по ней чат не добавить. "
            "Пришли ссылку-приглашение (t.me/+…) или имя публичного чата"
        )
    if head.startswith("+") and len(head) > 8:
        return f"https://t.me/{head}"
    if head == "joinchat" and len(parts) > 1:
        return f"https://t.me/+{parts[1]}"
    if USERNAME_RE.fullmatch(head):
        return f"https://t.me/{head.lower()}"  # t.me/имя/123 — ссылка на сообщение, берём чат
    raise BadChatRef("не разобрал ссылку: жду t.me/имя, @имя или t.me/+приглашение")


def message_link(chat_id: int, username: str | None, message_id: int | None) -> str | None:
    """Ссылка на сообщение или None, если у чата таких ссылок не бывает.

    Публичный чат — t.me/<username>/<id>. Супергруппа без username —
    t.me/c/<id без -100>/<id>: открывается у участников чата. У обычных
    (не «супер») групп и личных переписок ссылок на сообщения нет.
    """
    if not message_id:
        return None
    if username:
        return f"https://t.me/{username}/{message_id}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


def chat_link(
    chat_id: int, username: str | None, last_message_id: int | None, stored: str | None
) -> str | None:
    """Ссылка, чтобы открыть чат.

    Публичный — t.me/<username>. Супергруппа — t.me/c/…/<последнее сообщение>:
    у участника открывает чат на свежих сообщениях. Иначе — ссылка, по которой
    чат добавляли (например, приглашение).
    """
    if username:
        return f"https://t.me/{username}"
    link = message_link(chat_id, None, last_message_id)
    return link or stored or None


# SQL-выражение: id сообщения, где цитата сигнала стоит на самом деле.
# Нужно для сигналов, разобранных до того, как validate научился ставить это
# сообщение первым: бесплатно чинит их ссылки без повторного разбора.
# Сравнение без учёта регистра и лишних пробелов; не нашли — первое сообщение.
QUOTE_MESSAGE_SQL = """
coalesce(
    (select m.message_id from messages m
      where m.chat_id = s.chat_id and m.message_id = any(s.message_ids)
        and position(
              lower(regexp_replace(s.evidence_quote, '\\s+', ' ', 'g'))
              in lower(regexp_replace(m.text, '\\s+', ' ', 'g'))
            ) > 0
      order by m.message_id limit 1),
    s.message_ids[1]
)"""
