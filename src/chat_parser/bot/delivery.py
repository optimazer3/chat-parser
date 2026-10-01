"""Доставка итогов дня — на почту: обзор в письме, подробный PDF во вложении.

В Telegram отчёт не отправляется. Исключение — запасной путь, чтобы отчёт не
потерялся: почта не настроена или письмо не ушло. Тогда в Telegram приходят
предупреждение и PDF.
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


async def to_email(report: DailyReport, pdf: bytes | None) -> str | None:
    """None — письмо ушло; иначе — что пошло не так."""
    text = report.plain()
    if pdf is None:
        text = text.replace("Полный отчёт — во вложении (PDF).",
                            "PDF собрать не получилось — в письме только обзор.")
    try:
        await mailer.send(report.subject, text, pdf, report.filename, html=report.html())
    except Exception as e:  # noqa: BLE001
        log.warning("итоги дня не ушли на почту: %s", e)
        return str(e)
    return None


async def _fallback(bot: Bot, chat_ids: list[int], report: DailyReport, pdf: bytes | None,
                    problem: str) -> None:
    """Почта не сработала — отчёт приходит сюда, чтобы не потерялся."""
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, problem)
            if pdf:
                await bot.send_document(
                    chat_id, BufferedInputFile(pdf, report.filename), caption=report.caption
                )
            else:
                for part in fmt.chunks(report.text):
                    await bot.send_message(chat_id, part)
        except Exception as e:  # noqa: BLE001 — один получатель не мешает остальным
            log.warning("итоги дня не доставлены %s: %s", chat_id, e)


async def deliver(bot: Bot, chat_ids: Iterable[int], report: DailyReport) -> bool:
    """Отправить итоги на почту. True — письмо ушло."""
    chat_ids = list(chat_ids)
    pdf = await asyncio.to_thread(report.pdf, await _bot_username(bot))
    if not settings.email_ready:
        await _fallback(bot, chat_ids, report, pdf,
                        "⚠️ Почта не настроена, поэтому итоги дня присылаю сюда. Чтобы они "
                        "приходили на почту, заполни в .env REPORT_EMAIL_TO, SMTP_USER и "
                        "SMTP_PASSWORD и перезапусти бота.")
        return False
    error = await to_email(report, pdf)
    if error is None:
        return True
    await _fallback(bot, chat_ids, report, pdf,
                    f"⚠️ Итоги дня не ушли на почту: {fmt.esc(error)}\nПрисылаю их сюда.")
    return False
