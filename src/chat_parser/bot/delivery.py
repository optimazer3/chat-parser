"""Доставка недельного PDF на почту или в личный чат администратора."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Literal

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .. import mailer
from ..config import settings
from ..report.channel_weekly import ChannelWeeklyReport

log = logging.getLogger("chat_parser.delivery")


async def deliver(
    bot: Bot, chat_ids: Iterable[int], report: ChannelWeeklyReport,
) -> Literal["email", "telegram"] | None:
    pdf = await asyncio.to_thread(report.pdf)
    if settings.email_ready:
        try:
            await mailer.send(report.subject, report.email_text(), pdf, report.filename)
        except Exception:
            log.exception("еженедельный отчёт не ушёл на почту")
        else:
            return "email"

    delivered = False
    for chat_id in chat_ids:
        try:
            await bot.send_document(chat_id, BufferedInputFile(pdf, report.filename))
            delivered = True
        except Exception:
            log.exception("еженедельный отчёт не доставлен в Telegram: %s", chat_id)
    return "telegram" if delivered else None
