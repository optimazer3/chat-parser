"""Запуск бота: polling + суточный планировщик в одном процессе.

Бот — это панель управления (токен от @BotFather). Историю чатов он читать
не может и не должен: этим занимается аккаунт-сборщик через MTProto. Две
разные сущности, два разных набора ключей.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .. import db
from ..config import settings
from . import format as fmt
from . import jobs
from .auth import AdminOnly
from .handlers import router

log = logging.getLogger("chat_parser.bot")

COMMANDS = [
    BotCommand(command="status", description="что собрано и что в очереди"),
    BotCommand(command="top", description="топ болей"),
    BotCommand(command="pain", description="карточка боли по номеру"),
    BotCommand(command="signals", description="последние сигналы"),
    BotCommand(command="run", description="прогнать цикл сейчас"),
    BotCommand(command="report", description="отчёт файлом"),
    BotCommand(command="chats", description="список чатов"),
    BotCommand(command="addchat", description="добавить чат"),
    BotCommand(command="models", description="модели на шлюзе"),
    BotCommand(command="help", description="справка"),
]


async def _broadcast(bot: Bot, text: str) -> None:
    for admin in settings.admin_ids:
        try:
            for part in fmt.chunks(text):
                await bot.send_message(admin, part)
        except Exception as e:  # noqa: BLE001 — один недоступный админ не валит рассылку
            log.warning("не доставлено %s: %s", admin, e)


async def scheduler(bot: Bot) -> None:
    """Суточный прогон и дайджест в заданный час UTC."""
    while True:
        await asyncio.sleep(jobs.seconds_until(settings.daily_run_hour_utc))
        since = await jobs.last_pipeline_at()
        try:
            stats = await jobs.run_pipeline(lambda _: asyncio.sleep(0))
        except jobs.Busy:
            log.info("плановый прогон пропущен: занято")
            continue
        except Exception as e:  # noqa: BLE001
            await _broadcast(bot, f"❌ Плановый прогон упал: {type(e).__name__}: {fmt.esc(e)}")
            continue

        new_msgs, new_signals = await jobs.since_counts(since)
        pool = await db.get_pool()
        top = await pool.fetch(
            "select id, label, score, n_authors from clusters order by score desc limit 5"
        )
        await _broadcast(bot, fmt.fmt_digest(stats, new_signals, new_msgs, list(top)))


async def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    guard = AdminOnly(settings.admin_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(router)

    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    log.info("бот @%s запущен, админов: %d", me.username, len(settings.admin_ids))

    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await db.close_pool()
        await bot.session.close()
