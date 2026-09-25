"""Доставка итогов дня: PDF в Telegram с короткой подписью и на почту.

PDF рендерится один раз и уходит и в Telegram, и в письмо. Не собрался PDF —
отчёт всё равно доходит: в Telegram текстом, на почту — письмом без вложения.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .. import mailer
from ..config import settings
from ..report.daily import DailyReport
from . import format as fmt

log = logging.getLogger("chat_parser.delivery")


async def _bot_username(bot: Bot) -> str | None:
    try:
        return (await bot.me()).username
    except Exception:  # noqa: BLE001 — без ссылок на бота PDF всё равно нужен
        return None


async def to_telegram(bot: Bot, chat_ids: Iterable[int], report: DailyReport) -> bytes | None:
    pdf = await asyncio.to_thread(report.pdf, await _bot_username(bot))
    for chat_id in chat_ids:
        try:
            if pdf:
                await bot.send_document(
                    chat_id, BufferedInputFile(pdf, report.filename), caption=report.caption
                )
            else:
                for part in fmt.chunks(report.text):
                    await bot.send_message(chat_id, part)
        except Exception as e:  # noqa: BLE001 — один получатель не мешает остальным
            log.warning("итоги дня не доставлены %s: %s", chat_id, e)
    return pdf


async def to_email(report: DailyReport, pdf: bytes | None) -> str | None:
    """None — письмо ушло (или почта не настроена); иначе — что пошло не так."""
    if not settings.email_ready:
        return None
    text = report.plain()
    if pdf is None:
        text = text.replace("Полный отчёт — во вложении (PDF).",
                            "PDF собрать не получилось — полный отчёт в Telegram.")
    try:
        await mailer.send(report.subject, text, pdf, report.filename)
    except Exception as e:  # noqa: BLE001
        log.warning("итоги дня не ушли на почту: %s", e)
        return str(e)
    return None


async def deliver(bot: Bot, chat_ids: Iterable[int], report: DailyReport,
                  email: bool = True) -> bool:
    """Возвращает True, если письмо ушло."""
    chat_ids = list(chat_ids)
    pdf = await to_telegram(bot, chat_ids, report)
    if not email or not settings.email_ready:
        return False
    error = await to_email(report, pdf)
    if error:
        for chat_id in chat_ids:
            try:
                await bot.send_message(
                    chat_id, f"⚠️ Итоги дня не ушли на почту: {fmt.esc(error)}"
                )
            except Exception as e:  # noqa: BLE001
                log.warning("не доставлено %s: %s", chat_id, e)
    return error is None
