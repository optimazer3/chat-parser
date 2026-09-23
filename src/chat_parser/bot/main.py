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
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, User

from .. import db
from ..config import settings
from ..net import aiogram_proxy, network_hint
from . import format as fmt
from . import jobs
from .auth import AdminOnly
from .handlers import router

log = logging.getLogger("chat_parser.bot")

COMMANDS = [
    BotCommand(command="status", description="что загружено и что выполняется"),
    BotCommand(command="extract", description="разобрать обсуждения"),
    BotCommand(command="cluster", description="пересчитать боли"),
    BotCommand(command="top", description="топ болей"),
    BotCommand(command="pain", description="карточка боли по номеру"),
    BotCommand(command="signals", description="последние сигналы"),
    BotCommand(command="report", description="отчёт файлом"),
    BotCommand(command="redo", description="разобрать всё заново"),
    BotCommand(command="retry", description="повторить неудачные обсуждения"),
    BotCommand(command="run", description="полный цикл одной кнопкой"),
    BotCommand(command="chats", description="список чатов"),
    BotCommand(command="help", description="справка"),
]


async def _broadcast(bot: Bot, text: str) -> None:
    for admin in settings.admin_ids:
        try:
            for part in fmt.chunks(text):
                await bot.send_message(admin, part)
        except Exception as e:  # noqa: BLE001 — один недоступный админ не валит рассылку
            log.warning("не доставлено %s: %s", admin, e)


async def connect_saved(bot: Bot) -> None:
    """Ключи Telegram появились — подключить чаты, сохранённые кнопкой
    «Добавить чат», и предложить загрузить их историю. Токенов модели не
    тратит: это только подключение, разбор — по кнопке."""
    try:
        res = await jobs.run_job(jobs.connect_saved_chats())
    except (jobs.Busy, jobs.Cancelled):
        return  # подключатся по кнопке в /chats или при следующем запуске
    except Exception as e:  # noqa: BLE001
        await _broadcast(bot, f"❌ Не удалось подключить сохранённые чаты: {fmt.esc(e)}")
        return
    text, markup = fmt.fmt_connect_result(res)
    if not text:
        return
    for admin in settings.admin_ids:
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.warning("не доставлено %s: %s", admin, e)


def build_bot() -> Bot:
    """Бот с прокси из TG_PROXY, если он задан."""
    proxy = aiogram_proxy(settings.tg_proxy)
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    return Bot(
        token=settings.tg_bot_token,
        session=session,
        # Превью ссылок выключено: под цитатами ссылки на сообщения, и Telegram
        # иначе прилеплял бы к карточке боли большую плашку первой из них.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def check_bot(bot: Bot) -> User:
    """get_me с понятными ошибками вместо трейсбека aiogram."""
    try:
        return await bot.get_me()
    except TelegramUnauthorizedError:
        raise SystemExit(
            "TG_BOT_TOKEN не подходит — Telegram его не принял. "
            "Возьми токен заново у @BotFather (/mybots -> API Token)."
        ) from None
    except TelegramNetworkError as e:
        raise SystemExit(network_hint(e.message, settings.tg_proxy)) from None


async def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        bot = build_bot()
    except ValueError as e:  # неправильный TG_PROXY
        raise SystemExit(str(e)) from None
    task: asyncio.Task | None = None
    try:
        # Связь проверяем до сборки диспетчера: router — модульный синглтон,
        # и его можно подключить только к одному диспетчеру за процесс.
        me = await check_bot(bot)
        # Схема идемпотентна: так новые таблицы появляются после обновления
        # кода без ручного init-db.
        await db.apply_schema()
        dp = Dispatcher()
        guard = AdminOnly(settings.admin_ids)
        dp.message.outer_middleware(guard)
        dp.callback_query.outer_middleware(guard)
        dp.include_router(router)
        await bot.set_my_commands(COMMANDS)
        via = f", через прокси {settings.tg_proxy}" if settings.tg_proxy else ""
        log.info("бот @%s запущен%s, админов: %d", me.username, via, len(settings.admin_ids))
        # Расписания нет: всё, что тратит токены, запускается только вручную.
        if settings.telegram_ready and await jobs.pending_requests():
            task = asyncio.create_task(connect_saved(bot))
        await dp.start_polling(bot)
    finally:
        if task is not None:
            task.cancel()
        await db.close_pool()
        await bot.session.close()
