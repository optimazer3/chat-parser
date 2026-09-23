"""Ссылки на сообщения в Telegram."""

from __future__ import annotations


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
