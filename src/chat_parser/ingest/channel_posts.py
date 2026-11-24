"""Счётчики публикаций из истории канала Telegram."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.types import Message

from ..pii import mask_text
from .topics import reaction_counts


@dataclass(frozen=True)
class Post:
    id: int
    ts: datetime
    text: str
    reactions: int
    comments: int | None
    views: int | None = None
    forwards: int | None = None
    detail: dict[str, int] | None = None

    @property
    def likes(self) -> int | None:
        return self.detail.get("👍", 0) if self.detail is not None else None

    @property
    def engagement(self) -> int:
        return self.reactions + (self.comments or 0)


async def read(client: TelegramClient, entity, since: datetime, until: datetime) -> list[Post]:
    """Обновляет счётчики всех публикаций за заданный период."""
    posts = []
    albums: dict[int, Post] = {}
    async for msg in client.iter_messages(entity, offset_date=until):
        if msg.date < since:
            break
        if not isinstance(msg, Message) or not msg.post or msg.date >= until:
            continue
        replies = msg.replies
        detail = reaction_counts(msg)
        post = Post(msg.id, msg.date, mask_text(msg.message), sum(detail.values()),
                    replies.replies if replies and replies.comments else None,
                    msg.views, msg.forwards, detail)
        if msg.grouped_id is None:
            posts.append(post)
            continue
        prev = albums.get(msg.grouped_id)
        if prev is not None:
            # Один альбом — одна публикация. Ссылка ведёт на часть с подписью,
            # а повторённые Telegram счётчики не суммируются.
            lead = post if post.text or not prev.text else prev
            known_comments = [n for n in (post.comments, prev.comments) if n is not None]
            post = replace(lead, reactions=max(post.reactions, prev.reactions),
                           comments=max(known_comments, default=None),
                           views=max((v for v in (post.views, prev.views) if v is not None),
                                     default=None),
                           forwards=max((v for v in (post.forwards, prev.forwards) if v is not None),
                                        default=None),
                           detail=post.detail if post.reactions >= prev.reactions else prev.detail)
        albums[msg.grouped_id] = post
    return posts + list(albums.values())
